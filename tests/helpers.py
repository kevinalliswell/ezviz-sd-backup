from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def valid_config_text() -> str:
    return (ROOT / "examples" / "config.example.toml").read_text(encoding="utf-8").replace(
        "<DEVICE_SERIAL>", "SYNTHETIC_SERIAL"
    )


def write_config(directory: Path, text: str | None = None) -> Path:
    path = directory / "config.toml"
    path.write_text(text or valid_config_text(), encoding="utf-8")
    return path
