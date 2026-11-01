"""Command-line entry point for the P1 capability probe."""

from __future__ import annotations

import argparse
from datetime import date
import os
from pathlib import Path
import platform
import sys

from ezviz_sd_backup import __version__
from ezviz_sd_backup.config import Config, load_config
from ezviz_sd_backup.credentials import KeychainStore, TokenProvider, set_credentials_interactively
from ezviz_sd_backup.errors import AppError, ConfigError, ProbeError
from ezviz_sd_backup.ezviz import EzvizClient, Event
from ezviz_sd_backup.http_client import JsonHttpClient
from ezviz_sd_backup.recorder import MediaRecorder
from ezviz_sd_backup.redaction import Redactor


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ezviz-sd-backup")
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)

    credentials = commands.add_parser("set-credentials", help="通过隐藏输入写入 macOS Keychain")
    credentials.add_argument("--config", required=True, help="用户 Library 中的 config.toml")

    probe = commands.add_parser("probe", help="查询一天的历史事件，可选导出首个指定事件的 60 秒")
    probe.add_argument("--config", required=True, help="用户 Library 中的 config.toml")
    probe.add_argument("--date", required=True, type=date.fromisoformat, help="摄像机当地日期 YYYY-MM-DD")
    probe.add_argument("--export-key", help="仅导出稳定事件键匹配此前缀的历史事件")
    probe.add_argument("--seconds", type=int, default=60, help="导出时长 1-60 秒，默认 60")
    probe.add_argument("--output", type=Path, help="可选 MP4 输出路径；默认位于用户数据目录")
    return parser


def _client(config: Config, redactor: Redactor) -> EzvizClient:
    store = KeychainStore(config.ezviz.keychain_service, redactor=redactor)
    http = JsonHttpClient(redactor=redactor)
    tokens = TokenProvider(
        http, store, config.storage.data_dir / "token-cache.json", redactor
    )
    video_code = store.get("ezviz.video_code", required=False)
    return EzvizClient(
        http,
        tokens,
        config.camera,
        config.backup.timezone,
        redactor,
        video_code=video_code,
    )


def _select_event(events: tuple[Event, ...], prefix: str) -> Event:
    if len(prefix) < 8:
        raise ConfigError("--export-key 至少提供稳定事件键的前 8 位。")
    matches = [event for event in events if event.key.startswith(prefix)]
    if not matches:
        raise ProbeError("当天没有匹配该前缀的事件。", error_code="EVENT_NOT_FOUND")
    if len(matches) != 1:
        raise ProbeError("事件键前缀不唯一，请增加长度。", error_code="EVENT_KEY_AMBIGUOUS")
    return matches[0]


def _probe(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    redactor = Redactor([config.camera.device_serial])
    client = _client(config, redactor)
    result = client.query_day(args.date)
    local_zone = client.timezone
    print(
        f"环境: {platform.system()} {platform.release()} / {platform.machine()} / "
        f"Python {platform.python_version()}"
    )
    print(f"查询日期: {args.date.isoformat()}；接口: {result.api}；事件数: {len(result.events)}")
    for event in result.events:
        start = event.start_utc.astimezone(local_zone)
        end = event.end_utc.astimezone(local_zone)
        duration = (event.end_utc - event.start_utc).total_seconds()
        print(
            f"{event.key[:16]}  {start.isoformat()} -> {end.isoformat()}  "
            f"{event.record_type}  {duration:.0f}s"
        )
    if not args.export_key:
        return 0
    if not 1 <= args.seconds <= 60:
        raise ConfigError("--seconds 必须在 1 到 60 之间。")
    event = _select_event(result.events, args.export_key)
    playback = client.playback_address(event, args.seconds)
    output = args.output
    if output is None:
        start = event.start_utc.astimezone(local_zone)
        output = (
            config.storage.data_dir / "spool" / "probe"
            / f"{start:%Y%m%d-%H%M%S}_{event.key[:12]}.mp4"
        )
    output = output.expanduser()
    recorder = MediaRecorder(
        config.media.ffmpeg_path,
        config.media.ffprobe_path,
        redactor,
        max_bytes=config.storage.max_spool_bytes,
        min_free_bytes=config.storage.min_free_bytes,
    )
    info = recorder.save(playback, output)
    print(
        f"已保存并验证: {output}；{info.duration_seconds:.3f}s；"
        f"{info.size_bytes} bytes；视频: {', '.join(info.video_codecs)}"
    )
    print("离线/单项验证不替代 C6C 历史画面、首尾帧及 macOS 实机验收。")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "set-credentials":
            config = load_config(args.config)
            store = KeychainStore(config.ezviz.keychain_service)
            set_credentials_interactively(store)
            print("凭据已写入 macOS Keychain；未显示或记录密钥。")
            return 0
        if args.command == "probe":
            return _probe(args)
        raise ConfigError("未知命令。")
    except AppError as exc:
        print(f"错误 [{exc.error_code}]: {exc}", file=sys.stderr)
        return exc.exit_code
    except KeyboardInterrupt:
        print("错误 [INTERRUPTED]: 操作已中断。", file=sys.stderr)
        return 5
    except Exception as exc:
        print(f"错误 [INTERNAL_ERROR]: 未预期失败（{type(exc).__name__}）。", file=sys.stderr)
        return 5
