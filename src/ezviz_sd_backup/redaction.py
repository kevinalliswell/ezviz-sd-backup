"""Redact credentials and signed media URLs before displaying diagnostics."""

from __future__ import annotations

import re
from collections.abc import Iterable

_URL = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_SENSITIVE_FIELD = re.compile(
    r"(?i)\b(accessToken|access_token|appKey|appSecret|app_key|app_secret|"
    r"deviceSerial|device_serial|videoCode|video_code|code|token|password|secret)"
    r"(\s*[=:]\s*)([^\s&,;}\"']+)"
)


class Redactor:
    def __init__(self, secrets: Iterable[str] = ()) -> None:
        self._secrets: set[str] = set()
        self.add_many(secrets)

    def add(self, value: str | None) -> None:
        if value:
            self._secrets.add(str(value))

    def add_many(self, values: Iterable[str]) -> None:
        for value in values:
            self.add(value)

    def __call__(self, text: object) -> str:
        result = str(text)
        result = _URL.sub("<redacted-url>", result)
        result = _SENSITIVE_FIELD.sub(
            lambda match: f"{match.group(1)}{match.group(2)}<redacted>", result
        )
        for secret in sorted(self._secrets, key=len, reverse=True):
            result = result.replace(secret, "<redacted>")
        return result
