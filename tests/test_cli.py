from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ezviz_sd_backup.cli import _select_event, main
from ezviz_sd_backup.errors import ConfigError, ProbeError
from ezviz_sd_backup.ezviz import Event
from tests.helpers import write_config


class CliTests(unittest.TestCase):
    def test_invalid_config_uses_documented_exit_code(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "missing.toml"
            self.assertEqual(main(["probe", "--config", str(path), "--date", "2024-01-01"]), 2)

    def test_event_prefix_must_be_unambiguous(self) -> None:
        event = Event(
            "sd-unify", "SERIAL", 1,
            datetime(2024, 1, 1, tzinfo=timezone.utc),
            datetime(2024, 1, 1, 0, 1, tzinfo=timezone.utc), "ALARM",
        )
        self.assertEqual(_select_event((event,), event.key[:8]), event)
        with self.assertRaises(ConfigError):
            _select_event((event,), "short")
        with self.assertRaises(ProbeError):
            _select_event((event,), "deadbeef")

    def test_valid_config_does_not_run_on_non_macos(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = write_config(Path(directory))
            with patch("ezviz_sd_backup.credentials.platform.system", return_value="Windows"):
                self.assertEqual(
                    main(["probe", "--config", str(config), "--date", "2024-01-01"]), 3
                )


if __name__ == "__main__":
    unittest.main()
