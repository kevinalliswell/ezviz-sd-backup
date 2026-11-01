from __future__ import annotations

import ctypes
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ezviz_sd_backup.credentials import KeychainStore, TokenProvider
from ezviz_sd_backup.errors import AuthorizationError, CredentialError
from ezviz_sd_backup.redaction import Redactor


class FakeStore:
    def __init__(self) -> None:
        self.values = {"ezviz.app_key": "key", "ezviz.app_secret": "secret"}

    def get(self, account: str, *, required: bool = True):
        return self.values.get(account)


class FakeHttp:
    def __init__(self, responses: list[dict]) -> None:
        self.responses = responses
        self.calls = 0

    def request(self, *args, **kwargs) -> dict:
        self.calls += 1
        return self.responses.pop(0)


class FakeFunction:
    def __init__(self, callback) -> None:
        self.callback = callback
        self.argtypes = None
        self.restype = None

    def __call__(self, *args):
        return self.callback(*args)


class FakeSecurity:
    def __init__(self, value: bytes = b"secret", add_status: int = 0) -> None:
        self.value = value
        self.buffer = None
        self.free_calls = 0
        self.modify_calls = 0
        self.SecKeychainFindGenericPassword = FakeFunction(self.find)
        self.SecKeychainAddGenericPassword = FakeFunction(lambda *args: add_status)
        self.SecKeychainItemFreeContent = FakeFunction(self.free)
        self.SecKeychainItemModifyContent = FakeFunction(self.modify)

    def find(self, *args) -> int:
        if args[7] is not None:
            item = ctypes.cast(args[7], ctypes.POINTER(ctypes.c_void_p))
            item.contents.value = 1234
            return 0
        self.buffer = ctypes.create_string_buffer(self.value)
        length = ctypes.cast(args[5], ctypes.POINTER(ctypes.c_uint32))
        length.contents.value = len(self.value)
        data = ctypes.cast(args[6], ctypes.POINTER(ctypes.c_void_p))
        data.contents.value = ctypes.addressof(self.buffer)
        return 0

    def free(self, *args) -> int:
        self.free_calls += 1
        return 0

    def modify(self, *args) -> int:
        self.modify_calls += 1
        return 0


class FakeCoreFoundation:
    def __init__(self) -> None:
        self.release_calls = 0
        self.CFRelease = FakeFunction(self.release)

    def release(self, item) -> None:
        self.release_calls += 1


class CredentialTests(unittest.TestCase):
    def test_keychain_is_required_on_non_macos(self) -> None:
        with patch("ezviz_sd_backup.credentials.platform.system", return_value="Windows"):
            with self.assertRaises(CredentialError):
                KeychainStore("service").get("ezviz.app_key")

    def test_keychain_read_uses_security_framework_and_releases_buffer(self) -> None:
        security = FakeSecurity(b"framework-secret")
        store = KeychainStore("service")
        store._security = security
        store._core_foundation = FakeCoreFoundation()
        with patch("ezviz_sd_backup.credentials.platform.system", return_value="Darwin"):
            self.assertEqual(store.get("ezviz.app_key"), "framework-secret")
        self.assertEqual(security.free_calls, 1)
        self.assertNotIn("framework-secret", store.redactor("framework-secret"))

    def test_duplicate_keychain_item_is_updated_and_released(self) -> None:
        security = FakeSecurity(add_status=-25299)
        core = FakeCoreFoundation()
        store = KeychainStore("service")
        store._security = security
        store._core_foundation = core
        with patch("ezviz_sd_backup.credentials.platform.system", return_value="Darwin"):
            store.set("ezviz.app_key", "new-secret")
        self.assertEqual(security.modify_calls, 1)
        self.assertEqual(core.release_calls, 1)

    def test_invalid_keychain_utf8_still_releases_buffer(self) -> None:
        security = FakeSecurity(b"\xff")
        store = KeychainStore("service")
        store._security = security
        store._core_foundation = FakeCoreFoundation()
        with patch("ezviz_sd_backup.credentials.platform.system", return_value="Darwin"):
            with self.assertRaises(CredentialError):
                store.get("ezviz.app_key")
        self.assertEqual(security.free_calls, 1)

    def test_token_is_cached_without_repeated_credential_request(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "private" / "token.json"
            http = FakeHttp([
                {"code": "200", "data": {"accessToken": "token-one", "expireTime": 2_000_000}}
            ])
            redactor = Redactor()
            provider = TokenProvider(http, FakeStore(), cache, redactor, now=lambda: 1000)
            self.assertEqual(provider.get_token(), "token-one")
            self.assertEqual(provider.get_token(), "token-one")
            self.assertEqual(http.calls, 1)
            self.assertNotIn("token-one", redactor("token-one"))

    def test_expiring_cache_is_refreshed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "token.json"
            cache.write_text('{"accessToken":"old","expireTime":1000000}', encoding="utf-8")
            http = FakeHttp([
                {"code": "200", "data": {"accessToken": "new", "expireTime": 2_000_000}}
            ])
            provider = TokenProvider(http, FakeStore(), cache, Redactor(), now=lambda: 1000)
            self.assertEqual(provider.get_token(), "new")

    def test_invalid_app_credentials_have_auth_exit_category(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            http = FakeHttp([{"code": "10030", "msg": "private details"}])
            provider = TokenProvider(
                http, FakeStore(), Path(directory) / "token.json", Redactor(), now=lambda: 1000
            )
            with self.assertRaises(AuthorizationError) as caught:
                provider.get_token()
            self.assertNotIn("private details", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
