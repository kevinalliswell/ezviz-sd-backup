"""Strict parsing for the version 1 TOML configuration contract."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
import tomllib
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ezviz_sd_backup.errors import ConfigError


@dataclass(frozen=True)
class CameraConfig:
    name: str
    device_serial: str
    channel: int


@dataclass(frozen=True)
class EzvizConfig:
    keychain_service: str


@dataclass(frozen=True)
class BackupConfig:
    timezone: str
    lookback_days: int
    max_segment_seconds: int
    max_run_seconds: int
    download_concurrency: int
    upload_concurrency: int


@dataclass(frozen=True)
class StorageConfig:
    data_dir: Path
    max_spool_bytes: int
    min_free_bytes: int


@dataclass(frozen=True)
class MediaConfig:
    ffmpeg_path: Path
    ffprobe_path: Path


@dataclass(frozen=True)
class AlipanConfig:
    binary_path: Path
    config_dir: Path
    remote_root: str


@dataclass(frozen=True)
class CleanupConfig:
    enabled: bool
    keep_days: int
    require_verification: str


@dataclass(frozen=True)
class LoggingConfig:
    level: str
    directory: Path


@dataclass(frozen=True)
class Config:
    camera: CameraConfig
    ezviz: EzvizConfig
    backup: BackupConfig
    storage: StorageConfig
    media: MediaConfig
    alipan: AlipanConfig
    cleanup: CleanupConfig
    logging: LoggingConfig


_FIELDS = {
    "camera": {"name", "device_serial", "channel"},
    "ezviz": {"keychain_service"},
    "backup": {
        "timezone", "lookback_days", "max_segment_seconds", "max_run_seconds",
        "download_concurrency", "upload_concurrency",
    },
    "storage": {"data_dir", "max_spool_bytes", "min_free_bytes"},
    "media": {"ffmpeg_path", "ffprobe_path"},
    "alipan": {"binary_path", "config_dir", "remote_root"},
    "cleanup": {"enabled", "keep_days", "require_verification"},
    "logging": {"level", "directory"},
}


def _section(data: dict[str, Any], name: str) -> dict[str, Any]:
    value = data.get(name)
    if not isinstance(value, dict) or set(value) != _FIELDS[name]:
        raise ConfigError(f"配置段 {name} 缺失或包含未知字段。")
    return value


def _string(section: dict[str, Any], key: str, label: str) -> str:
    value = section.get(key)
    if not isinstance(value, str) or not value.strip() or any(ord(c) < 32 for c in value):
        raise ConfigError(f"{label}.{key} 必须是非空字符串。")
    return value


def _integer(section: dict[str, Any], key: str, label: str, minimum: int = 1) -> int:
    value = section.get(key)
    if type(value) is not int or value < minimum:
        raise ConfigError(f"{label}.{key} 必须是大于等于 {minimum} 的整数。")
    return value


def _path(section: dict[str, Any], key: str, label: str) -> Path:
    return Path(_string(section, key, label)).expanduser()


def _absolute_tool_path(section: dict[str, Any], key: str, label: str) -> Path:
    raw = _string(section, key, label)
    if not PurePosixPath(raw).is_absolute():
        raise ConfigError(f"{label}.{key} 必须使用绝对路径。")
    return Path(raw)


def load_config(path: str | Path) -> Config:
    config_path = Path(path).expanduser()
    try:
        with config_path.open("rb") as stream:
            data = tomllib.load(stream)
    except FileNotFoundError as exc:
        raise ConfigError("配置文件不存在；请从公开示例复制到用户 Library 后填写。") from exc
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError("无法读取或解析 TOML 配置。") from exc

    if set(data) != {"config_version", *_FIELDS} or type(data.get("config_version")) is not int:
        raise ConfigError("配置顶层字段不符合版本 1 契约。")
    if data["config_version"] != 1:
        raise ConfigError("仅支持 config_version = 1。")

    camera = _section(data, "camera")
    ezviz = _section(data, "ezviz")
    backup = _section(data, "backup")
    storage = _section(data, "storage")
    media = _section(data, "media")
    alipan = _section(data, "alipan")
    cleanup = _section(data, "cleanup")
    logging = _section(data, "logging")

    name = _string(camera, "name", "camera")
    serial = _string(camera, "device_serial", "camera")
    if name in {".", ".."} or "/" in name or "\\" in name:
        raise ConfigError("camera.name 不能包含路径分隔符或相对路径。")
    if serial == "<DEVICE_SERIAL>" or any(c.isspace() for c in serial):
        raise ConfigError("camera.device_serial 必须填写真实设备序列号，且不能包含空白。")
    timezone = _string(backup, "timezone", "backup")
    try:
        ZoneInfo(timezone)
    except ZoneInfoNotFoundError as exc:
        raise ConfigError("backup.timezone 必须是有效的 IANA 时区。") from exc
    if _integer(backup, "download_concurrency", "backup") != 1:
        raise ConfigError("P1 仅支持下载并发 1。")
    if _integer(backup, "upload_concurrency", "backup") != 1:
        raise ConfigError("P1 仅支持上传并发 1。")
    remote_root = _string(alipan, "remote_root", "alipan")
    if not remote_root.startswith("/") or ".." in PurePosixPath(remote_root).parts:
        raise ConfigError("alipan.remote_root 必须是不含 .. 的绝对远端路径。")
    if type(cleanup.get("enabled")) is not bool:
        raise ConfigError("cleanup.enabled 必须是布尔值。")
    if cleanup["enabled"]:
        raise ConfigError("本地自动清理尚未实现，cleanup.enabled 必须保持 false。")
    if _string(cleanup, "require_verification", "cleanup") != "sha1":
        raise ConfigError("cleanup.require_verification 仅接受 sha1。")
    level = _string(logging, "level", "logging")
    if level not in {"INFO", "WARNING", "ERROR"}:
        raise ConfigError("logging.level 仅接受 INFO、WARNING 或 ERROR。")

    return Config(
        camera=CameraConfig(name, serial, _integer(camera, "channel", "camera")),
        ezviz=EzvizConfig(_string(ezviz, "keychain_service", "ezviz")),
        backup=BackupConfig(
            timezone,
            _integer(backup, "lookback_days", "backup"),
            _integer(backup, "max_segment_seconds", "backup"),
            _integer(backup, "max_run_seconds", "backup"),
            backup["download_concurrency"],
            backup["upload_concurrency"],
        ),
        storage=StorageConfig(
            _path(storage, "data_dir", "storage"),
            _integer(storage, "max_spool_bytes", "storage"),
            _integer(storage, "min_free_bytes", "storage"),
        ),
        media=MediaConfig(
            _absolute_tool_path(media, "ffmpeg_path", "media"),
            _absolute_tool_path(media, "ffprobe_path", "media"),
        ),
        alipan=AlipanConfig(
            _absolute_tool_path(alipan, "binary_path", "alipan"),
            _path(alipan, "config_dir", "alipan"),
            remote_root,
        ),
        cleanup=CleanupConfig(False, _integer(cleanup, "keep_days", "cleanup", 0), "sha1"),
        logging=LoggingConfig(level, _path(logging, "directory", "logging")),
    )
