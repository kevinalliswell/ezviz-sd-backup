from __future__ import annotations

from datetime import datetime, timedelta, timezone
from io import BytesIO
import json
from pathlib import Path
import signal
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from ezviz_sd_backup.errors import MediaError
from ezviz_sd_backup.ezviz import PlaybackAddress
from ezviz_sd_backup.recorder import MediaRecorder, _HTTPSOnlyRedirectHandler
from ezviz_sd_backup.redaction import Redactor


class FakeResponse:
    def __init__(self, data: bytes, content_type: str = "video/x-flv") -> None:
        self.stream = BytesIO(data)
        self.headers = {"Content-Type": content_type}

    def __enter__(self):
        return self

    def __exit__(self, *args) -> None:
        self.stream.close()

    def read(self, size: int) -> bytes:
        return self.stream.read(size)

    def read1(self, size: int) -> bytes:
        return self.stream.read(size)


class FakeStdin:
    def __init__(self) -> None:
        self.buffer = BytesIO()
        self.file = tempfile.TemporaryFile()

    def write(self, data) -> int:
        return self.buffer.write(data)

    def fileno(self) -> int:
        return self.file.fileno()

    def close(self) -> None:
        self.buffer.close()
        self.file.close()


class FakeProcess:
    def __init__(
        self, command: list[str], *, early_exit: bool = False, returncode: int = 0,
        wait_timeout: bool = False,
    ) -> None:
        self.command = command
        self.early_exit = early_exit
        self.requested_returncode = returncode
        self.wait_timeout = wait_timeout
        self.returncode = 0 if early_exit else None
        self.stdin = FakeStdin()
        self.pid = 12345
        self.terminated = False

    def poll(self):
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        if self.returncode is None and self.wait_timeout:
            raise subprocess.TimeoutExpired("ffmpeg", timeout)
        if self.returncode is None:
            self.returncode = self.requested_returncode
            if self.returncode == 0:
                Path(self.command[-1]).write_bytes(b"media")
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15

    def kill(self) -> None:
        self.returncode = -9


class RecorderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        self.playback = PlaybackAddress(
            "https://media.invalid/history.flv?signature=private",
            self.start,
            self.start + timedelta(seconds=60),
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def recorder(
        self,
        responses: list[FakeResponse],
        processes: list[FakeProcess],
        *,
        duration: float = 60,
        streams: list[dict] | None = None,
        max_bytes: int = 1024 * 1024,
        early_exit: bool = False,
        returncode: int = 0,
        wait_timeout: bool = False,
    ) -> tuple[MediaRecorder, list[SimpleNamespace]]:
        commands: list[SimpleNamespace] = []

        def popen(command, **kwargs):
            process = FakeProcess(
                command, early_exit=early_exit, returncode=returncode, wait_timeout=wait_timeout
            )
            processes.append(process)
            commands.append(SimpleNamespace(command=command, options=kwargs))
            return process

        def runner(command, **kwargs):
            commands.append(SimpleNamespace(command=command, options=kwargs))
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps({
                    "streams": streams if streams is not None else [
                        {"codec_type": "video", "codec_name": "h264"}
                    ],
                    "format": {"duration": str(duration), "size": "5"},
                }),
                stderr="",
            )

        recorder = MediaRecorder(
            self.directory / "ffmpeg",
            self.directory / "ffprobe",
            Redactor(),
            max_bytes=max_bytes,
            min_free_bytes=0,
            opener=lambda *args, **kwargs: responses.pop(0),
            popen=popen,
            runner=runner,
        )
        return recorder, commands

    def test_stream_copy_uses_stdin_and_never_exposes_signed_url_in_argv(self) -> None:
        processes: list[FakeProcess] = []
        recorder, commands = self.recorder([FakeResponse(b"FLV" + b"x" * 100)], processes)
        output = self.directory / "event.mp4"
        info = recorder.save(self.playback, output)
        self.assertEqual(output.read_bytes(), b"media")
        self.assertEqual(info.video_codecs, ("h264",))
        ffmpeg_command = commands[0].command
        self.assertIn("pipe:0", ffmpeg_command)
        self.assertEqual(ffmpeg_command[ffmpeg_command.index("-c") + 1], "copy")
        for command in commands:
            self.assertFalse(any("signature=private" in argument for argument in command.command))

    def test_http_200_error_page_is_rejected_and_process_is_stopped(self) -> None:
        processes: list[FakeProcess] = []
        recorder, _ = self.recorder(
            [FakeResponse(b"<html>denied</html>", "text/html")], processes
        )
        with self.assertRaises(MediaError) as caught:
            recorder.save(self.playback, self.directory / "event.mp4")
        self.assertEqual(caught.exception.error_code, "MEDIA_RESPONSE_INVALID")
        self.assertTrue(processes[0].terminated)

    def test_non_flv_http_200_is_rejected(self) -> None:
        processes: list[FakeProcess] = []
        recorder, _ = self.recorder([FakeResponse(b"{\"code\": 200}")], processes)
        with self.assertRaises(MediaError) as caught:
            recorder.save(self.playback, self.directory / "event.mp4")
        self.assertEqual(caught.exception.error_code, "MEDIA_RESPONSE_INVALID")

    def test_early_ffmpeg_exit_is_not_success(self) -> None:
        recorder, _ = self.recorder([FakeResponse(b"FLV-data")], [], early_exit=True)
        with self.assertRaises(MediaError) as caught:
            recorder.save(self.playback, self.directory / "event.mp4")
        self.assertEqual(caught.exception.error_code, "FFMPEG_EARLY_EXIT")

    def test_nonzero_ffmpeg_exit_is_not_success(self) -> None:
        recorder, _ = self.recorder([FakeResponse(b"FLV-data")], [], returncode=1)
        with self.assertRaises(MediaError) as caught:
            recorder.save(self.playback, self.directory / "event.mp4")
        self.assertEqual(caught.exception.error_code, "FFMPEG_FAILED")

    def test_missing_video_stream_is_rejected(self) -> None:
        recorder, _ = self.recorder([FakeResponse(b"FLV-data")], [], streams=[])
        with self.assertRaises(MediaError) as caught:
            recorder.save(self.playback, self.directory / "event.mp4")
        self.assertEqual(caught.exception.error_code, "MEDIA_VALIDATION_FAILED")

    def test_truncated_duration_is_rejected(self) -> None:
        recorder, _ = self.recorder([FakeResponse(b"FLV-data")], [], duration=12)
        with self.assertRaises(MediaError) as caught:
            recorder.save(self.playback, self.directory / "event.mp4")
        self.assertEqual(caught.exception.error_code, "MEDIA_DURATION_MISMATCH")

    def test_spool_limit_is_enforced_before_writing(self) -> None:
        recorder, _ = self.recorder([FakeResponse(b"FLV-data")], [], max_bytes=4)
        with self.assertRaises(MediaError) as caught:
            recorder.save(self.playback, self.directory / "event.mp4")
        self.assertEqual(caught.exception.error_code, "SPOOL_BUDGET_EXHAUSTED")

    def test_existing_output_is_never_overwritten(self) -> None:
        output = self.directory / "event.mp4"
        output.write_bytes(b"original")
        recorder, _ = self.recorder([], [])
        with self.assertRaises(MediaError) as caught:
            recorder.save(self.playback, output)
        self.assertEqual(caught.exception.error_code, "OUTPUT_EXISTS")
        self.assertEqual(output.read_bytes(), b"original")

    def test_keyboard_interrupt_stops_independent_ffmpeg_process(self) -> None:
        class InterruptResponse(FakeResponse):
            def read1(self, size: int) -> bytes:
                raise KeyboardInterrupt

        processes: list[FakeProcess] = []
        recorder, _ = self.recorder([InterruptResponse(b"FLV-data")], processes)
        with self.assertRaises(KeyboardInterrupt):
            recorder.save(self.playback, self.directory / "event.mp4")
        self.assertTrue(processes[0].terminated)

    def test_socket_read_timeout_has_explicit_media_timeout(self) -> None:
        class TimeoutResponse(FakeResponse):
            def read1(self, size: int) -> bytes:
                raise TimeoutError

        processes: list[FakeProcess] = []
        recorder, _ = self.recorder([TimeoutResponse(b"FLV-data")], processes)
        with self.assertRaises(MediaError) as caught:
            recorder.save(self.playback, self.directory / "event.mp4")
        self.assertEqual(caught.exception.error_code, "MEDIA_TIMEOUT")
        self.assertTrue(processes[0].terminated)

    def test_ffmpeg_wait_timeout_is_terminated(self) -> None:
        processes: list[FakeProcess] = []
        recorder, _ = self.recorder(
            [FakeResponse(b"FLV-data")], processes, wait_timeout=True
        )
        with self.assertRaises(MediaError) as caught:
            recorder.save(self.playback, self.directory / "event.mp4")
        self.assertEqual(caught.exception.error_code, "MEDIA_TIMEOUT")
        self.assertTrue(processes[0].terminated)

    def test_sigterm_is_converted_to_cleanup_error_and_handler_is_restored(self) -> None:
        recorder, _ = self.recorder([], [])
        with (
            patch("ezviz_sd_backup.recorder.os.name", "posix"),
            patch("ezviz_sd_backup.recorder.signal.getsignal", return_value=signal.SIG_DFL),
            patch("ezviz_sd_backup.recorder.signal.signal") as signal_mock,
        ):
            previous = recorder._convert_sigterm_to_error()
            handler = signal_mock.call_args.args[1]
            with self.assertRaises(MediaError) as caught:
                handler(signal.SIGTERM, None)
            self.assertEqual(caught.exception.error_code, "INTERRUPTED")
            recorder._restore_sigterm(previous)
            self.assertEqual(signal_mock.call_args.args, (signal.SIGTERM, signal.SIG_DFL))

    def test_https_to_http_redirect_is_rejected_before_following(self) -> None:
        handler = _HTTPSOnlyRedirectHandler()
        with self.assertRaises(MediaError) as caught:
            handler.redirect_request(
                None, None, 302, "Found", {}, "http://media.invalid/video?signature=private-target"
            )
        self.assertEqual(caught.exception.error_code, "PLAYBACK_EXPORT_UNSUPPORTED")
        self.assertNotIn("private-target", str(caught.exception))

    def test_final_response_url_cannot_be_downgraded(self) -> None:
        class DowngradedResponse(FakeResponse):
            def geturl(self) -> str:
                return "http://media.invalid/video?signature=private-target"

        processes: list[FakeProcess] = []
        recorder, _ = self.recorder([DowngradedResponse(b"FLV-data")], processes)
        with self.assertRaises(MediaError) as caught:
            recorder.save(self.playback, self.directory / "event.mp4")
        self.assertEqual(caught.exception.error_code, "PLAYBACK_EXPORT_UNSUPPORTED")
        self.assertNotIn("private-target", str(caught.exception))
        self.assertTrue(processes[0].terminated)

    def test_ffmpeg_stderr_is_redacted(self) -> None:
        recorder, _ = self.recorder([], [])
        recorder.redactor.add_many(["SERIAL123", "token-value"])
        with tempfile.TemporaryFile() as output:
            output.write(
                b"https://media.invalid/a.flv?signature=private "
                b"accessToken=token-value deviceSerial=SERIAL123"
            )
            message = recorder._ffmpeg_error(output, 1)
        self.assertNotIn("private", message)
        self.assertNotIn("token-value", message)
        self.assertNotIn("SERIAL123", message)


if __name__ == "__main__":
    unittest.main()
