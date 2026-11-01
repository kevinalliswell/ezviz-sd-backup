"""EZVIZ historical event discovery and playback-address adapter."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
import hashlib
from typing import Any, Callable
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from ezviz_sd_backup.config import CameraConfig
from ezviz_sd_backup.credentials import TokenProvider
from ezviz_sd_backup.errors import AuthorizationError, CapabilityError, PaginationError, ProbeError
from ezviz_sd_backup.http_client import JsonHttpClient
from ezviz_sd_backup.redaction import Redactor


@dataclass(frozen=True)
class Event:
    source: str
    device_serial: str
    channel: int
    start_utc: datetime
    end_utc: datetime
    record_type: str

    @property
    def key(self) -> str:
        value = "|".join(
            (
                self.source,
                self.device_serial,
                str(self.channel),
                self.start_utc.isoformat(timespec="milliseconds"),
                self.end_utc.isoformat(timespec="milliseconds"),
                self.record_type,
            )
        )
        return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class DayQuery:
    day: date
    api: str
    events: tuple[Event, ...]


@dataclass(frozen=True)
class PlaybackAddress:
    url: str
    start_utc: datetime
    stop_utc: datetime


class EzvizClient:
    def __init__(
        self,
        http: JsonHttpClient,
        tokens: TokenProvider,
        camera: CameraConfig,
        timezone_name: str,
        redactor: Redactor,
        *,
        video_code: str | None = None,
        page_size: int = 500,
    ) -> None:
        self.http = http
        self.tokens = tokens
        self.camera = camera
        self.timezone = ZoneInfo(timezone_name)
        self.redactor = redactor
        self.video_code = video_code
        self.page_size = page_size
        self.redactor.add(camera.device_serial)
        self.redactor.add(video_code)

    def query_day(self, day: date) -> DayQuery:
        try:
            return DayQuery(day, "unify", self._query_unify(day))
        except CapabilityError as exc:
            if exc.error_code != "ENDPOINT_UNSUPPORTED":
                raise
            return DayQuery(day, "legacy", self._query_legacy(day))

    def _query_unify(self, day: date) -> tuple[Event, ...]:
        start = int(datetime.combine(day, time.min, self.timezone).timestamp())
        end = int(datetime.combine(day + timedelta(days=1), time.min, self.timezone).timestamp()) - 1
        cursor = start
        events: dict[str, Event] = {}
        for _ in range(1000):
            payload = self._request_with_token(
                lambda token: self.http.request(
                    "GET",
                    "/api/v3/device/local/video/unify/query",
                    headers={
                        "accessToken": token,
                        "deviceSerial": self.camera.device_serial,
                        "localIndex": str(self.camera.channel),
                    },
                    query={
                        "recordType": 2,
                        "startTime": str(cursor),
                        "endTime": str(end),
                        "pageSize": self.page_size,
                    },
                )
            )
            data = payload.get("data")
            if not isinstance(data, dict) or not isinstance(data.get("records"), list):
                raise ProbeError("统一录像查询响应缺少 records。", error_code="RESPONSE_INVALID")
            for record in data["records"]:
                event = self._event_from_record(record, unit="seconds", source="sd-unify")
                if event.record_type.upper() != "TIMING":
                    events[event.key] = event
            if not data.get("hasMore", False):
                return tuple(sorted(events.values(), key=lambda item: item.start_utc))
            next_cursor = data.get("nextFileTime")
            if type(next_cursor) is not int or next_cursor <= cursor or next_cursor > end:
                raise PaginationError("统一录像查询游标未前进或越界，已停止该日探测。")
            cursor = next_cursor
        raise PaginationError("统一录像查询超过 1000 页安全上限。", error_code="PAGINATION_LIMIT_EXCEEDED")

    def _query_legacy(self, day: date) -> tuple[Event, ...]:
        start = int(datetime.combine(day, time.min, self.timezone).timestamp() * 1000)
        end = int(datetime.combine(day + timedelta(days=1), time.min, self.timezone).timestamp() * 1000) - 1
        cursor = start
        events: dict[str, Event] = {}
        saw_non_event = False
        for _ in range(1000):
            payload = self._request_with_token(
                lambda token: self.http.request(
                    "POST",
                    "/api/lapp/video/by/time",
                    form={
                        "accessToken": token,
                        "deviceSerial": self.camera.device_serial,
                        "channelNo": self.camera.channel,
                        "startTime": cursor,
                        "endTime": end,
                        "recType": 2,
                        "version": "2.0",
                        "pageSize": self.page_size,
                    },
                )
            )
            data = payload.get("data")
            if not isinstance(data, dict) or not isinstance(data.get("files"), list):
                raise ProbeError("兼容录像查询响应缺少分页 files。", error_code="RESPONSE_INVALID")
            for record in data["files"]:
                event = self._event_from_record(record, unit="milliseconds", source="sd-legacy")
                record_type = event.record_type.upper()
                if record_type in {"ALARM", "ALLEVENT", "IO"}:
                    events[event.key] = event
                elif record_type == "TIMING":
                    saw_non_event = True
                else:
                    raise CapabilityError(
                        "兼容接口返回无法确认的录像类型，不能保证事件索引完整。",
                        error_code="EVENT_INDEX_UNAVAILABLE",
                    )
            if data.get("isAll") is True:
                if saw_non_event and not events:
                    raise CapabilityError(
                        "兼容接口只返回定时或无法确认的事件类型。",
                        error_code="EVENT_INDEX_UNAVAILABLE",
                    )
                return tuple(sorted(events.values(), key=lambda item: item.start_utc))
            next_cursor = data.get("nextFileTime")
            if type(next_cursor) is not int or next_cursor <= cursor or next_cursor > end:
                raise PaginationError("兼容录像查询游标未前进或越界，已停止该日探测。")
            cursor = next_cursor
        raise PaginationError("兼容录像查询超过 1000 页安全上限。", error_code="PAGINATION_LIMIT_EXCEEDED")

    def _event_from_record(self, record: object, *, unit: str, source: str) -> Event:
        if not isinstance(record, dict):
            raise ProbeError("录像记录不是对象。", error_code="RESPONSE_INVALID")
        start = record.get("startTime")
        end = record.get("endTime")
        if type(start) not in {int, float} or type(end) not in {int, float}:
            raise ProbeError("录像记录缺少有效时间戳。", error_code="RESPONSE_INVALID")
        divisor = 1000 if unit == "milliseconds" else 1
        start_utc = datetime.fromtimestamp(start / divisor, timezone.utc)
        end_utc = datetime.fromtimestamp(end / divisor, timezone.utc)
        if end_utc <= start_utc:
            raise ProbeError("录像记录结束时间未晚于开始时间。", error_code="RESPONSE_INVALID")
        record_type = record.get("type") if unit == "seconds" else record.get("localType")
        if not isinstance(record_type, str) or not record_type:
            raise ProbeError("录像记录缺少类型。", error_code="RESPONSE_INVALID")
        return Event(
            source, self.camera.device_serial, self.camera.channel,
            start_utc, end_utc, record_type,
        )

    def _request_with_token(self, request: Callable[[str], dict[str, Any]]) -> dict[str, Any]:
        for refreshed in (False, True):
            payload = request(self.tokens.get_token(force_refresh=refreshed))
            code = self._response_code(payload)
            if code == 200:
                return payload
            if code == 10002 and not refreshed:
                continue
            self._raise_api_error(code)
        raise AssertionError("unreachable")

    @staticmethod
    def _response_code(payload: dict[str, Any]) -> int:
        meta = payload.get("meta")
        raw = meta.get("code") if isinstance(meta, dict) else payload.get("code")
        try:
            return int(raw)
        except (TypeError, ValueError) as exc:
            raise ProbeError("接口响应缺少有效状态码。", error_code="RESPONSE_INVALID") from exc

    @staticmethod
    def _raise_api_error(code: int) -> None:
        if code in {10002, 10005, 10013, 10030, 10031, 401, 403}:
            raise AuthorizationError(f"平台授权或设备权限校验失败（代码 {code}）。")
        if code == 20015:
            raise CapabilityError(
                "设备不支持统一事件录像查询。", error_code="ENDPOINT_UNSUPPORTED"
            )
        if code == 20007:
            raise ProbeError("摄像机不在线，无法完成历史探测。", error_code="DEVICE_OFFLINE")
        raise ProbeError(f"平台接口返回错误（代码 {code}）。", error_code="API_REQUEST_FAILED")

    def playback_address(self, event: Event, seconds: int) -> PlaybackAddress:
        if not 1 <= seconds <= 60:
            raise ValueError("P1 playback probe duration must be between 1 and 60 seconds.")
        stop = min(event.end_utc, event.start_utc + timedelta(seconds=seconds))
        if stop <= event.start_utc:
            raise ProbeError("事件没有可导出的时长。", error_code="RESPONSE_INVALID")

        def request(token: str) -> dict[str, Any]:
            form: dict[str, object] = {
                "accessToken": token,
                "deviceSerial": self.camera.device_serial,
                "channelNo": self.camera.channel,
                "protocol": 4,
                "type": "2",
                "startTime": event.start_utc.astimezone(self.timezone).strftime("%Y-%m-%d %H:%M:%S"),
                "stopTime": stop.astimezone(self.timezone).strftime("%Y-%m-%d %H:%M:%S"),
                "expireTime": 300,
                "diserr": 1,
            }
            if self.video_code:
                form["code"] = self.video_code
            return self.http.request("POST", "/api/lapp/v2/live/address/get", form=form)

        payload = self._request_with_token(request)
        data = payload.get("data")
        url = data.get("url") if isinstance(data, dict) else None
        if not isinstance(url, str):
            raise ProbeError("回放地址响应缺少 url。", error_code="RESPONSE_INVALID")
        self.redactor.add(url)
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise CapabilityError(
                "平台未返回可安全读取的 HTTPS HTTP-FLV 地址。",
                error_code="PLAYBACK_EXPORT_UNSUPPORTED",
            )
        return PlaybackAddress(url, event.start_utc, stop)
