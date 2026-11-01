#!/usr/bin/env python3
"""Check documentation and example contracts; never contact external services."""

from __future__ import annotations

import os
from pathlib import Path, PurePosixPath
import plistlib
import re
import sys
from urllib.parse import unquote, urlsplit

if sys.version_info < (3, 12):
    raise SystemExit("Python 3.12+ is required.")

import tomllib


ROOT = Path(__file__).resolve().parents[1]
PLIST = "examples/launchd/io.github.kevinalliswell.ezviz-sd-backup.plist"
REQUIRED = (
    "README.md", "AGENTS.md", "LICENSE", "CONTRIBUTING.md", "SECURITY.md",
    "CODE_OF_CONDUCT.md", ".gitignore", ".gitattributes", ".editorconfig",
    ".python-version", ".github/workflows/checks.yml", ".github/dependabot.yml",
    ".github/ISSUE_TEMPLATE/bug_report.yml",
    ".github/ISSUE_TEMPLATE/feature_request.yml",
    ".github/ISSUE_TEMPLATE/config.yml", ".github/pull_request_template.md",
    "docs/README.md", "docs/requirements.md", "docs/architecture.md",
    "docs/integrations/ezviz.md", "docs/integrations/alipan.md",
    "docs/configuration.md", "docs/macos.md", "docs/operations.md",
    "docs/testing.md", "docs/roadmap.md", "docs/implementation-prompt.md",
    "docs/adr/README.md", "docs/adr/0001-scheduled-cli.md",
    "docs/adr/0002-historical-export-gate.md",
    "docs/adr/0003-incremental-verification.md",
    "examples/config.example.toml", PLIST, "scripts/check_repo.py",
    "pyproject.toml", "src/ezviz_sd_backup/__main__.py",
    "src/ezviz_sd_backup/cli.py", "src/ezviz_sd_backup/config.py",
    "src/ezviz_sd_backup/credentials.py", "src/ezviz_sd_backup/ezviz.py",
    "src/ezviz_sd_backup/recorder.py", "tests/test_ezviz.py", "tests/test_recorder.py",
)
SKIP_DIRS = {
    ".git", ".venv", "venv", "node_modules", "__pycache__", "dist", "build",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", "data", "spool", "runtime",
    "logs", "evidence", "test-results",
}
LINK = re.compile(r"!?\[[^\]\n]*\]\(\s*(<[^>\n]+>|[^\s)]+)(?:\s+[^)\n]*)?\)")
REFERENCE = re.compile(r"^\s{0,3}\[[^\]\n]+\]:\s*(<[^>\n]+>|\S+)", re.MULTILINE)
SECRET = re.compile(
    r"gh[pousr]_[A-Za-z0-9]{24,}|github_pat_[A-Za-z0-9_]{40,}|"
    r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"
)
errors: list[str] = []


def require(condition: bool, message: str) -> None:
    if not condition:
        errors.append(message)


def public_files():
    """Do not recurse into runtime, cache, dependency or Git directories."""
    for directory, subdirs, filenames in os.walk(ROOT):
        subdirs[:] = sorted(name for name in subdirs if name not in SKIP_DIRS)
        for filename in sorted(filenames):
            yield Path(directory) / filename


def without_code(markdown: str) -> str:
    """Remove fenced and inline code before checking Markdown destinations."""
    lines = []
    fence = ""
    for line in markdown.splitlines():
        opening = re.match(r"^\s{0,3}(`{3,}|~{3,})", line)
        if not fence and opening:
            fence = opening.group(1)
        elif fence:
            if re.fullmatch(r"\s{0,3}" + re.escape(fence[0]) + "{" + str(len(fence)) + r",}\s*", line):
                fence = ""
        else:
            lines.append(re.sub(r"(`+).*?\1", "", line))
    return "\n".join(lines)


def check_target(source: Path, destination: str) -> None:
    destination = destination.removeprefix("<").removesuffix(">")
    parsed = urlsplit(destination)
    if parsed.scheme or parsed.netloc or not parsed.path:
        return  # External URLs and anchors are outside this check's scope.
    label = source.relative_to(ROOT).as_posix()
    require(not parsed.path.startswith("/"), f"{label}: absolute local link: {destination}")
    target = (source.parent / unquote(parsed.path)).resolve()
    if not target.is_relative_to(ROOT):
        errors.append(f"{label}: link escapes repository: {destination}")
        return
    if not target.exists():
        errors.append(f"{label}: missing link target: {destination}")
        return
    # Check spelling even on case-insensitive filesystems used by local authors.
    current = ROOT
    for part in target.relative_to(ROOT).parts:
        if part not in {item.name for item in current.iterdir()}:
            errors.append(f"{label}: link case differs from target: {destination}")
            break
        current /= part


def check_documents(files: list[Path]) -> None:
    count = 0
    for source in files:
        if source.suffix.lower() != ".md":
            continue
        count += 1
        markdown = without_code(source.read_text(encoding="utf-8"))
        for pattern in (LINK, REFERENCE):
            for match in pattern.finditer(markdown):
                check_target(source, match.group(1))
    print(f"Checked local file links in {count} Markdown files (URL/anchor checks excluded).")


def check_config() -> None:
    with (ROOT / "examples/config.example.toml").open("rb") as stream:
        config = tomllib.load(stream)
    fields = {
        "camera": {"name", "device_serial", "channel"},
        "ezviz": {"keychain_service"},
        "backup": {"timezone", "lookback_days", "max_segment_seconds", "max_run_seconds",
                   "download_concurrency", "upload_concurrency"},
        "storage": {"data_dir", "max_spool_bytes", "min_free_bytes"},
        "media": {"ffmpeg_path", "ffprobe_path"},
        "alipan": {"binary_path", "config_dir", "remote_root"},
        "cleanup": {"enabled", "keep_days", "require_verification"},
        "logging": {"level", "directory"},
    }
    require(set(config) == {"config_version", *fields}, "Unexpected example configuration sections.")
    require(type(config.get("config_version")) is int and config["config_version"] == 1,
            "Example config_version must be integer 1.")
    for section, expected in fields.items():
        require(isinstance(config.get(section), dict) and set(config[section]) == expected,
                f"Unexpected fields in config section {section}; do not add credentials.")
    if errors:
        return
    require(config["camera"]["device_serial"] == "<DEVICE_SERIAL>",
            "Example must contain only the device serial placeholder.")
    require(type(config["camera"]["channel"]) is int and config["camera"]["channel"] == 1,
            "Example channel must be integer 1.")
    expected_defaults = {
        "backup": {"timezone": "Asia/Shanghai", "lookback_days": 3,
                   "max_segment_seconds": 1800, "max_run_seconds": 10800,
                   "download_concurrency": 1, "upload_concurrency": 1},
        "storage": {"max_spool_bytes": 2 * 1024**3, "min_free_bytes": 5 * 1024**3},
        "cleanup": {"enabled": False, "keep_days": 14, "require_verification": "sha1"},
        "logging": {"level": "INFO"},
    }
    for section, defaults in expected_defaults.items():
        for key, expected in defaults.items():
            value = config[section][key]
            require(type(value) is type(expected) and value == expected,
                    f"Example {section}.{key} must match documented default {expected!r}.")
    require(config["ezviz"]["keychain_service"] == "io.github.kevinalliswell.ezviz-sd-backup",
            "Keychain service must match documented contract.")
    library_root = "~/Library/Application Support/ezviz-sd-backup"
    require(config["storage"]["data_dir"] == library_root, "Example data must live under user Library.")
    require(config["alipan"]["config_dir"] == library_root + "/aliyunpan",
            "Third-party credentials must use a dedicated Library directory.")
    require(config["logging"]["directory"] == "~/Library/Logs/ezviz-sd-backup",
            "Example logs must live under user Library.")
    for section, key in (("media", "ffmpeg_path"), ("media", "ffprobe_path"), ("alipan", "binary_path")):
        value = config[section][key]
        require(isinstance(value, str) and PurePosixPath(value).is_absolute(),
                f"Example {section}.{key} must use an absolute macOS path.")
    require(config["alipan"]["remote_root"] == "/家庭监控", "Example remote path differs from docs.")
    print("Checked TOML fields, credential placeholders and conservative defaults.")


def check_plist() -> None:
    with (ROOT / PLIST).open("rb") as stream:
        config = plistlib.load(stream)
    expected = {
        "Label": "io.github.kevinalliswell.ezviz-sd-backup",
        "ProgramArguments": ["__PYTHON_BIN__", "-m", "ezviz_sd_backup", "run", "--config", "__CONFIG_PATH__"],
        "WorkingDirectory": "__APP_DIR__",
        "StartCalendarInterval": {"Hour": 3, "Minute": 15},
        "ProcessType": "Background", "Nice": 10, "LowPriorityIO": True, "Umask": 0o077,
        "StandardOutPath": "__LOG_DIR__/launchd.stdout.log",
        "StandardErrorPath": "__LOG_DIR__/launchd.stderr.log",
    }
    require(config == expected, "launchd template differs from documented schedule/paths/background settings.")
    require("KeepAlive" not in config and "RunAtLoad" not in config,
            "launchd template must not run continuously or launch at login.")
    require("EnvironmentVariables" not in config, "Do not put credentials into launchd environment.")
    print("Checked plist placeholders and one-shot daily scheduling.")


def main() -> int:
    for relative in REQUIRED:
        require((ROOT / relative).is_file(), f"Missing required file: {relative}")
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    files = list(public_files())
    for path in files:
        if path.suffix in {".md", ".py", ".toml", ".yml", ".plist"}:
            require(not SECRET.search(path.read_text(encoding="utf-8")),
                    f"Potential credential marker in {path.relative_to(ROOT).as_posix()}; inspect privately.")
    check_documents(files)
    for check in (check_config, check_plist):
        try:
            check()
        except (OSError, ValueError, KeyError, TypeError) as exc:
            errors.append(f"{check.__name__}: invalid example ({type(exc).__name__}).")
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print("Repository checks passed. Device, upload and macOS behavior remain untested.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
