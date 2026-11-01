from __future__ import annotations

import unittest

from ezviz_sd_backup.redaction import Redactor


class RedactionTests(unittest.TestCase):
    def test_known_secrets_fields_and_signed_urls_are_removed(self) -> None:
        signed = "https://example.invalid/video.flv?token=private-signature&id=123"
        redact = Redactor(["literal-secret", "SERIAL123", signed])
        result = redact(
            f"accessToken=literal-secret deviceSerial=SERIAL123 url={signed} "
            "appSecret: another-secret"
        )
        self.assertNotIn("literal-secret", result)
        self.assertNotIn("SERIAL123", result)
        self.assertNotIn("private-signature", result)
        self.assertNotIn("another-secret", result)
        self.assertNotIn("https://", result)

    def test_empty_secrets_do_not_destroy_message(self) -> None:
        self.assertEqual(Redactor([""])("ordinary error"), "ordinary error")


if __name__ == "__main__":
    unittest.main()
