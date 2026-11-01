from __future__ import annotations

from io import BytesIO
import json
import unittest
from urllib.error import HTTPError, URLError

from ezviz_sd_backup.errors import AuthorizationError, ProbeError
from ezviz_sd_backup.http_client import JsonHttpClient


class Response:
    def __init__(self, payload: object) -> None:
        self.stream = BytesIO(json.dumps(payload).encode("utf-8"))

    def __enter__(self):
        return self

    def __exit__(self, *args) -> None:
        self.stream.close()

    def read(self, size: int) -> bytes:
        return self.stream.read(size)


class HttpClientTests(unittest.TestCase):
    def test_network_failure_is_retried_with_bounded_attempts(self) -> None:
        calls = 0
        sleeps: list[float] = []

        def opener(request, timeout):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise URLError("private network detail")
            return Response({"code": "200"})

        client = JsonHttpClient(opener=opener, sleeper=sleeps.append)
        self.assertEqual(client.request("GET", "/test"), {"code": "200"})
        self.assertEqual(calls, 2)
        self.assertEqual(sleeps, [0.25])

    def test_http_401_has_authorization_category(self) -> None:
        def opener(request, timeout):
            raise HTTPError(request.full_url, 401, "unauthorized", {}, BytesIO(b"secret"))

        client = JsonHttpClient(opener=opener, attempts=1)
        with self.assertRaises(AuthorizationError):
            client.request("GET", "/test")

    def test_invalid_json_is_not_confused_with_empty_result(self) -> None:
        class InvalidResponse(Response):
            def __init__(self) -> None:
                self.stream = BytesIO(b"not-json")

        client = JsonHttpClient(opener=lambda *args, **kwargs: InvalidResponse(), attempts=1)
        with self.assertRaises(ProbeError) as caught:
            client.request("GET", "/test")
        self.assertEqual(caught.exception.error_code, "RESPONSE_INVALID")

    def test_absolute_external_api_path_is_rejected(self) -> None:
        client = JsonHttpClient()
        with self.assertRaises(ValueError):
            client.request("GET", "https://attacker.invalid/collect")


if __name__ == "__main__":
    unittest.main()
