from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from ezviz_sd_backup.config import load_config
from ezviz_sd_backup.errors import ConfigError
from tests.helpers import valid_config_text, write_config


class ConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def load(self, text: str | None = None):
        return load_config(write_config(self.directory, text))

    def test_valid_example_contract(self) -> None:
        config = self.load()
        self.assertEqual(config.camera.name, "客厅")
        self.assertEqual(config.backup.download_concurrency, 1)
        self.assertFalse(config.cleanup.enabled)
        self.assertEqual(config.backup.max_run_seconds, 10800)

    def test_placeholder_serial_is_rejected(self) -> None:
        with self.assertRaises(ConfigError):
            self.load(valid_config_text().replace("SYNTHETIC_SERIAL", "<DEVICE_SERIAL>"))

    def test_cleanup_cannot_be_enabled_before_implementation(self) -> None:
        with self.assertRaises(ConfigError):
            self.load(valid_config_text().replace("enabled = false", "enabled = true"))

    def test_concurrency_must_remain_one(self) -> None:
        with self.assertRaises(ConfigError):
            self.load(valid_config_text().replace("download_concurrency = 1", "download_concurrency = 2"))

    def test_unknown_field_is_rejected(self) -> None:
        with self.assertRaises(ConfigError):
            self.load(valid_config_text() + "\nunknown = true\n")

    def test_boolean_is_not_accepted_as_integer(self) -> None:
        with self.assertRaises(ConfigError):
            self.load(valid_config_text().replace("channel = 1", "channel = true"))

    def test_invalid_timezone_is_rejected(self) -> None:
        with self.assertRaises(ConfigError):
            self.load(valid_config_text().replace("Asia/Shanghai", "Mars/Olympus"))

    def test_remote_path_traversal_is_rejected(self) -> None:
        with self.assertRaises(ConfigError):
            self.load(valid_config_text().replace("/家庭监控", "/family/../other"))

    def test_relative_tool_path_is_rejected(self) -> None:
        with self.assertRaises(ConfigError):
            self.load(valid_config_text().replace("/opt/homebrew/bin/ffmpeg", "ffmpeg"))


if __name__ == "__main__":
    unittest.main()
