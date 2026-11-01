from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import unittest
from zoneinfo import ZoneInfo

from ezviz_sd_backup.config import CameraConfig
from ezviz_sd_backup.errors import AuthorizationError, CapabilityError, PaginationError, ProbeError
from ezviz_sd_backup.ezviz import Event, EzvizClient
from ezviz_sd_backup.redaction import Redactor


class FakeTokens:
    def __init__(self) -> None:
        self.calls: list[bool] = []

    def get_token(self, *, force_refresh: bool = False) -> str:
        self.calls.append(force_refresh)
        return "fresh-token" if force_refresh else "cached-token"


class FakeHttp:
    def __init__(self, responses: list[dict | Exception]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, str, dict]] = []

    def request(self, method: str, path: str, **kwargs) -> dict:
        self.calls.append((method, path, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def unify(records: list[dict], *, has_more: bool = False, next_time: int | None = None, code: int = 200) -> dict:
    return {
        "meta": {"code": code},
        "data": {"records": records, "hasMore": has_more, "nextFileTime": next_time},
    }


def legacy(records: list[dict], *, is_all: bool = True, next_time: int = 0) -> dict:
    return {"code": "200", "data": {"files": records, "isAll": is_all, "nextFileTime": next_time}}


class EzvizTests(unittest.TestCase):
    def setUp(self) -> None:
        self.camera = CameraConfig("客厅", "SYNTHETIC_SERIAL", 1)
        self.day = date(2024, 3, 1)
        self.day_start = int(datetime(2024, 3, 1, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp())

    def client(self, responses: list[dict | Exception], timezone_name: str = "Asia/Shanghai", video_code: str | None = None):
        http = FakeHttp(responses)
        tokens = FakeTokens()
        redactor = Redactor()
        client = EzvizClient(
            http, tokens, self.camera, timezone_name, redactor, video_code=video_code
        )
        return client, http, tokens, redactor

    def test_unify_uses_seconds_and_normalizes_event(self) -> None:
        start = self.day_start + 3600
        client, http, _, _ = self.client([
            unify([{"startTime": start, "endTime": start + 30, "type": "ALARM"}])
        ])
        result = client.query_day(self.day)
        self.assertEqual(result.api, "unify")
        self.assertEqual(len(result.events), 1)
        self.assertEqual(result.events[0].start_utc, datetime.fromtimestamp(start, timezone.utc))
        method, path, options = http.calls[0]
        self.assertEqual((method, path), ("GET", "/api/v3/device/local/video/unify/query"))
        self.assertEqual(options["query"]["startTime"], str(self.day_start))
        self.assertLessEqual(len(options["query"]["startTime"]), 10)
        self.assertEqual(options["query"]["recordType"], 2)
        self.assertEqual(options["headers"]["accessToken"], "cached-token")

    def test_day_range_respects_dst_and_half_open_boundary(self) -> None:
        client, http, _, _ = self.client([unify([])], "America/New_York")
        client.query_day(date(2024, 11, 3))
        query = http.calls[0][2]["query"]
        self.assertEqual(int(query["endTime"]) - int(query["startTime"]), 25 * 3600 - 1)

    def test_pagination_deduplicates_repeated_boundary_event(self) -> None:
        first = {"startTime": self.day_start + 10, "endTime": self.day_start + 20, "type": "ALARM"}
        second = {"startTime": self.day_start + 30, "endTime": self.day_start + 40, "type": "ALARM"}
        client, _, _, _ = self.client([
            unify([first], has_more=True, next_time=self.day_start + 11),
            unify([first, second]),
        ])
        result = client.query_day(self.day)
        self.assertEqual(len(result.events), 2)
        self.assertLess(result.events[0].start_utc, result.events[1].start_utc)

    def test_non_advancing_cursor_is_not_an_empty_result(self) -> None:
        client, _, _, _ = self.client([
            unify([], has_more=True, next_time=self.day_start)
        ])
        with self.assertRaises(PaginationError):
            client.query_day(self.day)

    def test_empty_result_and_api_error_are_distinct(self) -> None:
        client, http, _, _ = self.client([unify([])])
        self.assertEqual(client.query_day(self.day).events, ())
        self.assertEqual(len(http.calls), 1)
        client, _, _, _ = self.client([unify([], code=50000)])
        with self.assertRaises(ProbeError):
            client.query_day(self.day)

    def test_expired_token_is_refreshed_exactly_once(self) -> None:
        client, http, tokens, _ = self.client([unify([], code=10002), unify([])])
        client.query_day(self.day)
        self.assertEqual(tokens.calls, [False, True])
        self.assertEqual(http.calls[1][2]["headers"]["accessToken"], "fresh-token")

    def test_permission_failure_does_not_fall_back(self) -> None:
        client, http, _, _ = self.client([unify([], code=10031)])
        with self.assertRaises(AuthorizationError):
            client.query_day(self.day)
        self.assertEqual(len(http.calls), 1)

    def test_empty_success_does_not_fall_back(self) -> None:
        client, http, _, _ = self.client([unify([])])
        self.assertEqual(client.query_day(self.day).api, "unify")
        self.assertEqual(len(http.calls), 1)

    def test_unsupported_unify_falls_back_to_millisecond_event_query(self) -> None:
        start_ms = (self.day_start + 60) * 1000 + 250
        client, http, _, _ = self.client([
            unify([], code=20015),
            legacy([{"startTime": start_ms, "endTime": start_ms + 30000, "localType": "ALARM"}]),
        ])
        result = client.query_day(self.day)
        self.assertEqual(result.api, "legacy")
        self.assertEqual(result.events[0].start_utc, datetime.fromtimestamp(start_ms / 1000, timezone.utc))
        self.assertEqual(http.calls[1][2]["form"]["startTime"], self.day_start * 1000)
        self.assertEqual(http.calls[1][2]["form"]["recType"], 2)

    def test_legacy_timing_records_do_not_become_event_index(self) -> None:
        start_ms = (self.day_start + 60) * 1000
        client, _, _, _ = self.client([
            unify([], code=20015),
            legacy([{"startTime": start_ms, "endTime": start_ms + 30000, "localType": "TIMING"}]),
        ])
        with self.assertRaises(CapabilityError) as caught:
            client.query_day(self.day)
        self.assertEqual(caught.exception.error_code, "EVENT_INDEX_UNAVAILABLE")

    def test_playback_uses_history_type_flv_and_bounded_duration(self) -> None:
        url = "https://media.invalid/history.flv?signature=private"
        client, http, _, redactor = self.client([
            {"code": "200", "data": {"url": url}}
        ], video_code="video-secret")
        event = Event(
            "sd-unify", self.camera.device_serial, 1,
            datetime(2024, 3, 1, 1, 0, tzinfo=timezone.utc),
            datetime(2024, 3, 1, 1, 10, tzinfo=timezone.utc), "ALARM",
        )
        playback = client.playback_address(event, 60)
        self.assertEqual(playback.stop_utc - playback.start_utc, timedelta(seconds=60))
        form = http.calls[0][2]["form"]
        self.assertEqual(form["type"], "2")
        self.assertEqual(form["protocol"], 4)
        self.assertEqual(form["code"], "video-secret")
        self.assertNotIn("private", redactor(url))

    def test_non_https_playback_is_blocked(self) -> None:
        client, _, _, _ = self.client([
            {"code": "200", "data": {"url": "ezopen://private"}}
        ])
        event = Event(
            "sd-unify", self.camera.device_serial, 1,
            datetime(2024, 3, 1, tzinfo=timezone.utc),
            datetime(2024, 3, 1, 0, 1, tzinfo=timezone.utc), "ALARM",
        )
        with self.assertRaises(CapabilityError):
            client.playback_address(event, 60)

    def test_refresh_still_failing_is_limited_to_two_requests(self) -> None:
        client, http, tokens, _ = self.client([unify([], code=10002), unify([], code=10002)])
        with self.assertRaises(AuthorizationError):
            client.query_day(self.day)
        self.assertEqual(tokens.calls, [False, True])
        self.assertEqual(len(http.calls), 2)

    def test_legacy_pagination_deduplicates_and_requires_progress(self) -> None:
        start_ms = (self.day_start + 60) * 1000
        record = {"startTime": start_ms, "endTime": start_ms + 30000, "localType": "ALARM"}
        client, _, _, _ = self.client([
            unify([], code=20015),
            legacy([record], is_all=False, next_time=start_ms + 1000),
            legacy([record]),
        ])
        self.assertEqual(len(client.query_day(self.day).events), 1)

        client, _, _, _ = self.client([
            unify([], code=20015),
            legacy([], is_all=False, next_time=self.day_start * 1000),
        ])
        with self.assertRaises(PaginationError):
            client.query_day(self.day)

    def test_pagination_midway_error_is_not_returned_as_complete(self) -> None:
        client, _, _, _ = self.client([
            unify([], has_more=True, next_time=self.day_start + 1),
            unify([], code=50000),
        ])
        with self.assertRaises(ProbeError):
            client.query_day(self.day)

    def test_unknown_legacy_type_is_not_silently_dropped(self) -> None:
        start_ms = (self.day_start + 60) * 1000
        client, _, _, _ = self.client([
            unify([], code=20015),
            legacy([
                {"startTime": start_ms, "endTime": start_ms + 1000, "localType": "ALARM"},
                {"startTime": start_ms + 2000, "endTime": start_ms + 3000, "localType": "UNKNOWN"},
            ]),
        ])
        with self.assertRaises(CapabilityError) as caught:
            client.query_day(self.day)
        self.assertEqual(caught.exception.error_code, "EVENT_INDEX_UNAVAILABLE")

    def test_playback_form_uses_refreshed_token(self) -> None:
        client, http, tokens, _ = self.client([
            {"code": "10002"},
            {"code": "200", "data": {"url": "https://media.invalid/history.flv"}},
        ])
        event = Event(
            "sd-unify", self.camera.device_serial, 1,
            datetime(2024, 3, 1, tzinfo=timezone.utc),
            datetime(2024, 3, 1, 0, 1, tzinfo=timezone.utc), "ALARM",
        )
        client.playback_address(event, 60)
        self.assertEqual(tokens.calls, [False, True])
        self.assertEqual(http.calls[1][2]["form"]["accessToken"], "fresh-token")


if __name__ == "__main__":
    unittest.main()
