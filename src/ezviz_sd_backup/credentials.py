"""macOS Keychain access without placing secrets in process arguments."""

from __future__ import annotations

import ctypes
from ctypes.util import find_library
import getpass
import json
import os
from pathlib import Path
import platform
import time
from typing import Any, Callable

from ezviz_sd_backup.errors import AuthorizationError, CredentialError, ProbeError
from ezviz_sd_backup.http_client import JsonHttpClient
from ezviz_sd_backup.redaction import Redactor

ACCOUNT_KEYS = ("ezviz.app_key", "ezviz.app_secret", "ezviz.video_code")


class KeychainStore:
    """Use the Security framework directly; command arguments never contain values."""

    def __init__(self, service: str, *, redactor: Redactor | None = None) -> None:
        self.service = service.encode("utf-8")
        self.redactor = redactor or Redactor()
        self._security: Any = None
        self._core_foundation: Any = None

    def _library(self) -> Any:
        if platform.system() != "Darwin":
            raise CredentialError("Keychain 只能在目标 macOS 用户会话中使用。")
        if self._security is None:
            path = find_library("Security")
            core_path = find_library("CoreFoundation")
            if not path or not core_path:
                raise CredentialError("无法加载 macOS Security/CoreFoundation framework。")
            self._security = ctypes.CDLL(path)
            self._core_foundation = ctypes.CDLL(core_path)
            self._core_foundation.CFRelease.argtypes = [ctypes.c_void_p]
            self._core_foundation.CFRelease.restype = None
            self._security.SecKeychainFindGenericPassword.argtypes = [
                ctypes.c_void_p, ctypes.c_uint32, ctypes.c_char_p,
                ctypes.c_uint32, ctypes.c_char_p, ctypes.c_void_p,
                ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p,
            ]
            self._security.SecKeychainFindGenericPassword.restype = ctypes.c_int32
            self._security.SecKeychainAddGenericPassword.argtypes = [
                ctypes.c_void_p, ctypes.c_uint32, ctypes.c_char_p,
                ctypes.c_uint32, ctypes.c_char_p, ctypes.c_uint32,
                ctypes.c_char_p, ctypes.c_void_p,
            ]
            self._security.SecKeychainAddGenericPassword.restype = ctypes.c_int32
            self._security.SecKeychainItemFreeContent.argtypes = [
                ctypes.c_void_p, ctypes.c_void_p,
            ]
            self._security.SecKeychainItemFreeContent.restype = ctypes.c_int32
        return self._security

    @staticmethod
    def _check(status: int, action: str) -> None:
        if status != 0:
            raise CredentialError(f"Keychain {action}失败（Security 状态 {status}）。")

    def get(self, account: str, *, required: bool = True) -> str | None:
        security = self._library()
        account_bytes = account.encode("utf-8")
        length = ctypes.c_uint32()
        data = ctypes.c_void_p()
        status = security.SecKeychainFindGenericPassword(
            None, len(self.service), self.service, len(account_bytes), account_bytes,
            ctypes.byref(length), ctypes.byref(data), None,
        )
        if status == -25300:
            if required:
                raise CredentialError(f"Keychain 缺少 {account}；请先交互式设置。")
            return None
        self._check(status, "读取")
        try:
            value = ctypes.string_at(data, length.value).decode("utf-8")
        except UnicodeError as exc:
            raise CredentialError(f"Keychain 中 {account} 不是有效 UTF-8。") from exc
        finally:
            security.SecKeychainItemFreeContent(None, data)
        self.redactor.add(value)
        return value

    def set(self, account: str, value: str) -> None:
        if not value:
            raise CredentialError("不能写入空密钥。")
        security = self._library()
        account_bytes = account.encode("utf-8")
        value_bytes = value.encode("utf-8")
        status = security.SecKeychainAddGenericPassword(
            None, len(self.service), self.service, len(account_bytes), account_bytes,
            len(value_bytes), value_bytes, None,
        )
        if status == -25299:
            status = self._update_existing(security, account_bytes, value_bytes)
        self._check(status, "写入")
        self.redactor.add(value)

    def _update_existing(self, security: Any, account_bytes: bytes, value_bytes: bytes) -> int:
        item = ctypes.c_void_p()
        status = security.SecKeychainFindGenericPassword(
            None, len(self.service), self.service, len(account_bytes), account_bytes,
            None, None, ctypes.byref(item),
        )
        if status != 0:
            return status
        try:
            security.SecKeychainItemModifyContent.argtypes = [
                ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_char_p,
            ]
            security.SecKeychainItemModifyContent.restype = ctypes.c_int32
            return security.SecKeychainItemModifyContent(item, None, len(value_bytes), value_bytes)
        finally:
            self._core_foundation.CFRelease(item)


def set_credentials_interactively(store: KeychainStore) -> None:
    for account in ACCOUNT_KEYS[:2]:
        value = getpass.getpass(f"请输入 {account}（输入不可见）：")
        store.set(account, value)
    video_code = getpass.getpass("可选：请输入 ezviz.video_code；无则直接回车：")
    if video_code:
        store.set("ezviz.video_code", video_code)


class TokenProvider:
    """Acquire and cache an access token with restrictive local file permissions."""

    def __init__(
        self,
        http: JsonHttpClient,
        store: KeychainStore,
        cache_path: Path,
        redactor: Redactor,
        *,
        now: Callable[[], float] = time.time,
    ) -> None:
        self.http = http
        self.store = store
        self.cache_path = cache_path
        self.redactor = redactor
        self.now = now

    def get_token(self, *, force_refresh: bool = False) -> str:
        if not force_refresh:
            cached = self._read_cache()
            if cached:
                return cached
        app_key = self.store.get("ezviz.app_key")
        app_secret = self.store.get("ezviz.app_secret")
        payload = self.http.request(
            "POST", "/api/lapp/token/get", form={"appKey": app_key, "appSecret": app_secret}
        )
        code = str(payload.get("code", ""))
        if code != "200":
            if code in {"10002", "10005", "10013", "10030"}:
                raise AuthorizationError("AppKey/AppSecret 无效或没有接口权限。")
            raise ProbeError("获取访问令牌失败。", error_code="TOKEN_REQUEST_FAILED")
        data = payload.get("data")
        if not isinstance(data, dict):
            raise ProbeError("令牌响应缺少 data。", error_code="RESPONSE_INVALID")
        token = data.get("accessToken")
        expire_time = data.get("expireTime")
        if not isinstance(token, str) or not token or type(expire_time) is not int:
            raise ProbeError("令牌响应字段不完整。", error_code="RESPONSE_INVALID")
        self.redactor.add(token)
        self._write_cache(token, expire_time)
        return token

    def _read_cache(self) -> str | None:
        try:
            with self.cache_path.open("r", encoding="utf-8") as stream:
                data = json.load(stream)
            token = data["accessToken"]
            expire_time = data["expireTime"]
            if not isinstance(token, str) or type(expire_time) is not int:
                return None
            if expire_time / 1000 <= self.now() + 300:
                return None
            self.redactor.add(token)
            return token
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def _write_cache(self, token: str, expire_time: int) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = self.cache_path.with_suffix(".tmp")
        try:
            with temporary.open("w", encoding="utf-8") as stream:
                json.dump({"accessToken": token, "expireTime": expire_time}, stream)
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.cache_path)
        except OSError as exc:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise CredentialError("无法安全写入访问令牌缓存。") from exc
