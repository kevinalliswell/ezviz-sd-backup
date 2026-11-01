"""Bounded HTTPS HTTP-FLV recording through FFmpeg stdin."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import select
import shutil
import signal
import socket
import subprocess
import tempfile
import threading
import time
from typing import Any, Callable
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, build_opener

from ezviz_sd_backup.errors import MediaError
from ezviz_sd_backup.ezviz import PlaybackAddress
from ezviz_sd_backup.redaction import Redactor

_CHUNK_BYTES = 64 * 1024


class _HTTPSOnlyRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> Any:
        _require_https_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _require_https_url(url: str) -> None:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise MediaError(
            "回放连接或重定向目标不是安全的 HTTPS 地址。",
            error_code="PLAYBACK_EXPORT_UNSUPPORTED",
        )


def _open_https_url(url: str, *, timeout: float) -> Any:
    _require_https_url(url)
    return _HTTPS_OPENER.open(url, timeout=timeout)


_HTTPS_OPENER = build_opener(_HTTPSOnlyRedirectHandler)


@dataclass(frozen=True)
class MediaInfo:
    duration_seconds: float
    size_bytes: int
    video_codecs: tuple[str, ...]


class MediaRecorder:
    def __init__(
        self,
        ffmpeg_path: Path,
        ffprobe_path: Path,
        redactor: Redactor,
        *,
        max_bytes: int,
        min_free_bytes: int,
        opener: Callable[..., Any] = _open_https_url,
        popen: Callable[..., Any] = subprocess.Popen,
        runner: Callable[..., Any] = subprocess.run,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.ffmpeg_path = ffmpeg_path
        self.ffprobe_path = ffprobe_path
        self.redactor = redactor
        self.max_bytes = max_bytes
        self.min_free_bytes = min_free_bytes
        self.opener = opener
        self.popen = popen
        self.runner = runner
        self.clock = clock

    def save(self, playback: PlaybackAddress, output: Path) -> MediaInfo:
        requested = int((playback.stop_utc - playback.start_utc).total_seconds())
        if not 1 <= requested <= 60:
            raise MediaError("P1 单次媒体探测仅允许 1 到 60 秒。")
        if output.suffix.lower() != ".mp4":
            raise MediaError("媒体探测输出必须使用 .mp4 后缀。")
        if output.exists():
            raise MediaError("输出文件已存在；不会覆盖已有原片。", error_code="OUTPUT_EXISTS")
        output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        partial = output.with_name(output.name + ".partial")
        partial.unlink(missing_ok=True)
        deadline = self.clock() + requested * 2 + 60

        command = [
            str(self.ffmpeg_path), "-hide_banner", "-loglevel", "error", "-nostdin",
            "-f", "flv", "-i", "pipe:0", "-t", str(requested), "-map", "0",
            "-c", "copy", "-f", "mp4", str(partial),
        ]
        process: Any = None
        previous_sigterm = self._convert_sigterm_to_error()
        try:
            with tempfile.TemporaryFile() as error_output:
                process = self.popen(
                    command,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL,
                    stderr=error_output,
                    bufsize=0,
                    start_new_session=os.name == "posix",
                )
                if os.name == "posix":
                    os.set_blocking(process.stdin.fileno(), False)
                self.redactor.add(playback.url)
                connect_timeout = max(0.1, min(15.0, deadline - self.clock()))
                with self.opener(playback.url, timeout=connect_timeout) as response:
                    final_url = response.geturl() if hasattr(response, "geturl") else playback.url
                    self.redactor.add(final_url)
                    _require_https_url(final_url)
                    content_type = response.headers.get("Content-Type", "")
                    if "text/html" in content_type.lower():
                        raise MediaError(
                            "回放服务器返回 HTML 而非 FLV。", error_code="MEDIA_RESPONSE_INVALID"
                        )
                    first = self._read(response, deadline)
                    if not first.startswith(b"FLV"):
                        raise MediaError(
                            "HTTP 响应不是可识别的 FLV 媒体。", error_code="MEDIA_RESPONSE_INVALID"
                        )
                    received = len(first)
                    if received > self.max_bytes:
                        raise MediaError("媒体达到暂存字节上限。", error_code="SPOOL_BUDGET_EXHAUSTED")
                    self._write(process, first, deadline, output.parent)
                    while True:
                        chunk = self._read(response, deadline)
                        if not chunk:
                            break
                        received += len(chunk)
                        if received > self.max_bytes:
                            raise MediaError("媒体达到暂存字节上限。", error_code="SPOOL_BUDGET_EXHAUSTED")
                        self._write(process, chunk, deadline, output.parent)
                process.stdin.close()
                remaining = max(0.1, deadline - self.clock())
                return_code = process.wait(timeout=remaining)
                if return_code != 0:
                    raise MediaError(
                        self._ffmpeg_error(error_output, return_code),
                        error_code="FFMPEG_FAILED",
                    )
            info = self._inspect(partial)
            tolerance = max(2.0, requested * 0.1)
            if abs(info.duration_seconds - requested) > tolerance:
                raise MediaError(
                    f"媒体时长异常：目标 {requested} 秒，实际 {info.duration_seconds:.3f} 秒。",
                    error_code="MEDIA_DURATION_MISMATCH",
                )
            os.chmod(partial, 0o600)
            if output.exists():
                raise MediaError("输出文件在保存期间出现；不会覆盖。", error_code="OUTPUT_EXISTS")
            partial.rename(output)
            return info
        except MediaError:
            raise
        except subprocess.TimeoutExpired as exc:
            raise MediaError("FFmpeg 等待超过时间预算。", error_code="MEDIA_TIMEOUT") from exc
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            raise MediaError(
                f"媒体保存失败：{self.redactor(type(exc).__name__)}。",
                error_code="MEDIA_SAVE_FAILED",
            ) from exc
        finally:
            self._restore_sigterm(previous_sigterm)
            self._terminate(process)

    def _read(self, response: Any, deadline: float) -> bytes:
        remaining = deadline - self.clock()
        if remaining <= 0:
            raise MediaError("媒体读取超过时间预算。", error_code="MEDIA_TIMEOUT")
        self._set_socket_timeout(response, min(15.0, remaining))
        try:
            reader = getattr(response, "read1", None)
            chunk = reader(_CHUNK_BYTES) if callable(reader) else response.read(_CHUNK_BYTES)
        except (socket.timeout, TimeoutError) as exc:
            raise MediaError("媒体读取超时。", error_code="MEDIA_TIMEOUT") from exc
        if self.clock() > deadline:
            raise MediaError("媒体读取超过时间预算。", error_code="MEDIA_TIMEOUT")
        return chunk

    @staticmethod
    def _set_socket_timeout(response: Any, timeout: float) -> None:
        # CPython's HTTPResponse and addinfourl use these two socket layouts.
        for attributes in (("fp", "raw", "_sock"), ("fp", "fp", "raw", "_sock")):
            value = response
            for attribute in attributes:
                value = getattr(value, attribute, None)
            if value is not None and hasattr(value, "settimeout"):
                value.settimeout(max(0.1, timeout))
                return

    def _write(self, process: Any, data: bytes, deadline: float, directory: Path) -> None:
        if process.poll() is not None:
            raise MediaError("FFmpeg 在媒体传输期间提前退出。", error_code="FFMPEG_EARLY_EXIT")
        if shutil.disk_usage(directory).free < self.min_free_bytes:
            raise MediaError("磁盘可用空间低于安全下限。", error_code="DISK_BUDGET_EXHAUSTED")
        view = memoryview(data)
        while view:
            remaining = deadline - self.clock()
            if remaining <= 0:
                raise MediaError("FFmpeg 写入阻塞直至超时。", error_code="MEDIA_TIMEOUT")
            try:
                if os.name == "posix":
                    _, writable, _ = select.select([], [process.stdin], [], min(remaining, 1.0))
                    if not writable:
                        continue
                written = process.stdin.write(view)
            except BrokenPipeError as exc:
                raise MediaError(
                    "FFmpeg 在媒体传输期间关闭输入。", error_code="FFMPEG_EARLY_EXIT"
                ) from exc
            except BlockingIOError:
                continue
            if written is None:
                continue
            if written <= 0:
                raise MediaError("FFmpeg 写入没有进展。", error_code="FFMPEG_EARLY_EXIT")
            view = view[written:]

    def _inspect(self, path: Path) -> MediaInfo:
        command = [
            str(self.ffprobe_path), "-v", "error", "-show_entries",
            "stream=codec_type,codec_name:format=duration,size", "-of", "json", str(path),
        ]
        try:
            result = self.runner(command, capture_output=True, text=True, timeout=30, check=False)
        except subprocess.TimeoutExpired as exc:
            raise MediaError("ffprobe 执行超时。", error_code="MEDIA_TIMEOUT") from exc
        except (OSError, subprocess.SubprocessError) as exc:
            raise MediaError("ffprobe 执行失败。", error_code="MEDIA_VALIDATION_FAILED") from exc
        if result.returncode != 0:
            raise MediaError("ffprobe 无法读取保存的媒体。", error_code="MEDIA_VALIDATION_FAILED")
        try:
            payload = json.loads(result.stdout)
            streams = payload["streams"]
            container = payload["format"]
            duration = float(container["duration"])
            size = int(container["size"])
            codecs = tuple(
                stream["codec_name"] for stream in streams
                if stream.get("codec_type") == "video" and isinstance(stream.get("codec_name"), str)
            )
        except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            raise MediaError("ffprobe 返回无效媒体元数据。", error_code="MEDIA_VALIDATION_FAILED") from exc
        if not codecs or duration <= 0 or size <= 0 or path.stat().st_size != size:
            raise MediaError("媒体缺少视频流、有效时长或完整大小。", error_code="MEDIA_VALIDATION_FAILED")
        return MediaInfo(duration, size, codecs)

    @staticmethod
    def _convert_sigterm_to_error() -> Any:
        if os.name != "posix" or threading.current_thread() is not threading.main_thread():
            return None

        def handle_sigterm(signum: int, frame: Any) -> None:
            raise MediaError("收到 SIGTERM，正在终止媒体子进程。", error_code="INTERRUPTED")

        previous = signal.getsignal(signal.SIGTERM)
        signal.signal(signal.SIGTERM, handle_sigterm)
        return previous

    @staticmethod
    def _restore_sigterm(previous: Any) -> None:
        if previous is not None and threading.current_thread() is threading.main_thread():
            signal.signal(signal.SIGTERM, previous)

    def _ffmpeg_error(self, output: Any, return_code: int) -> str:
        try:
            output.seek(0)
            detail = output.read(4096).decode("utf-8", errors="replace").strip()
        except OSError:
            detail = ""
        suffix = f"：{self.redactor(detail)}" if detail else ""
        return f"FFmpeg 以状态 {return_code} 退出{suffix}"

    @staticmethod
    def _terminate(process: Any) -> None:
        if process is None:
            return
        try:
            if process.poll() is None:
                try:
                    if os.name == "posix":
                        os.killpg(process.pid, signal.SIGTERM)
                    else:
                        process.terminate()
                    process.wait(timeout=5)
                except (OSError, subprocess.SubprocessError):
                    try:
                        if os.name == "posix":
                            os.killpg(process.pid, signal.SIGKILL)
                        else:
                            process.kill()
                        process.wait(timeout=5)
                    except (OSError, subprocess.SubprocessError):
                        pass
        finally:
            try:
                process.stdin.close()
            except (AttributeError, OSError, ValueError):
                pass
