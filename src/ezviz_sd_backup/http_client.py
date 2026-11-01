"""Small, bounded JSON HTTP client with no third-party dependency."""

from __future__ import annotations

import json
import socket
import time
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from ezviz_sd_backup.errors import AuthorizationError, ProbeError
from ezviz_sd_backup.redaction import Redactor


class JsonHttpClient:
    def __init__(
        self,
        *,
        base_url: str = "https://open.ys7.com",
        timeout: float = 15.0,
        attempts: int = 3,
        redactor: Redactor | None = None,
        opener: Callable[..., Any] = urlopen,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.attempts = attempts
        self.redactor = redactor or Redactor()
        self.opener = opener
        self.sleeper = sleeper

    def request(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, object] | None = None,
        headers: dict[str, str] | None = None,
        form: dict[str, object] | None = None,
    ) -> dict[str, Any]:
        if not path.startswith("/") or "://" in path:
            raise ValueError("JSON API path must be relative to the fixed HTTPS origin.")
        url = self.base_url + path
        if query:
            url += "?" + urlencode(query)
        body = urlencode(form).encode("utf-8") if form is not None else None
        request_headers = {"Accept": "application/json", **(headers or {})}
        if form is not None:
            request_headers["Content-Type"] = "application/x-www-form-urlencoded"
        request = Request(url, data=body, headers=request_headers, method=method)

        for attempt in range(self.attempts):
            try:
                with self.opener(request, timeout=self.timeout) as response:
                    raw = response.read(2 * 1024 * 1024 + 1)
                if len(raw) > 2 * 1024 * 1024:
                    raise ProbeError("接口响应超过 2 MiB 安全上限。", error_code="RESPONSE_TOO_LARGE")
                payload = json.loads(raw.decode("utf-8"))
                if not isinstance(payload, dict):
                    raise ProbeError("接口未返回 JSON 对象。", error_code="RESPONSE_INVALID")
                return payload
            except HTTPError as exc:
                if exc.code in {401, 403}:
                    raise AuthorizationError("平台拒绝授权；请检查令牌、设备授权和权限范围。") from exc
                if exc.code < 500 or attempt + 1 == self.attempts:
                    raise ProbeError(
                        f"平台 HTTP 请求失败（状态 {exc.code}）。", error_code="HTTP_REQUEST_FAILED"
                    ) from exc
            except (URLError, TimeoutError, socket.timeout, OSError) as exc:
                if attempt + 1 == self.attempts:
                    raise ProbeError(
                        "平台网络请求失败或超时。", error_code="NETWORK_REQUEST_FAILED"
                    ) from exc
            except (UnicodeError, json.JSONDecodeError) as exc:
                raise ProbeError(
                    "平台响应不是有效的 UTF-8 JSON。", error_code="RESPONSE_INVALID"
                ) from exc
            self.sleeper(0.25 * (2**attempt))
        raise AssertionError("unreachable")
