"""Redaction tests: exact values and the encodings a secret takes in transit."""

from __future__ import annotations

import base64
import unittest
from urllib.parse import quote

from broker.redact import redact, redact_header, secret_variants

SECRET = "s3cr3t-token/value+more"


class RedactTest(unittest.TestCase):
    def test_plain_and_encoded_forms_are_replaced(self) -> None:
        secrets = {"token": SECRET}
        text = " ".join(
            [
                SECRET,
                quote(SECRET, safe=""),
                base64.b64encode(SECRET.encode()).decode(),
                SECRET.replace("/", "\\/"),
            ]
        )
        out = redact(text, secrets, label="neuralwatt")
        self.assertNotIn(SECRET, out)
        self.assertNotIn(quote(SECRET, safe=""), out)
        self.assertNotIn(base64.b64encode(SECRET.encode()).decode(), out)
        self.assertEqual(out.count("[REDACTED:neuralwatt]"), 4)

    def test_variants_cover_url_json_base64(self) -> None:
        variants = secret_variants(SECRET)
        self.assertIn(quote(SECRET, safe=""), variants)
        self.assertIn(base64.b64encode(SECRET.encode()).decode(), variants)

    def test_longest_variant_wins(self) -> None:
        secrets = {"a": "abcdef", "b": "abcdefgh"}
        self.assertEqual(redact("abcdefgh", secrets, label="x"), "[REDACTED:x]")

    def test_no_secrets_is_a_no_op(self) -> None:
        self.assertEqual(redact("plain text", {}, label="x"), "plain text")
        self.assertEqual(redact("", {"a": SECRET}, label="x"), "")

    def test_only_url_bearing_headers_are_redacted(self) -> None:
        secrets = {"token": SECRET}
        self.assertEqual(redact_header("Location", f"https://x/?t={SECRET}", secrets, label="h"), "https://x/?t=[REDACTED:h]")
        self.assertEqual(redact_header("Content-Type", SECRET, secrets, label="h"), SECRET)


if __name__ == "__main__":
    unittest.main()
