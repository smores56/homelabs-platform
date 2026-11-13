"""GitHub webhook signature verification.

The header is the only thing that authenticates a delivery when a source has a
secret, so every rejection path is pinned here.
"""

from __future__ import annotations

import hashlib
import hmac
import unittest

from webhook.signature import verify

SECRET = b"hunter2"
BODY = b'{"zen":"Keep it logically awesome."}'


def sign(secret: bytes, body: bytes) -> str:
    return "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()


class VerifyTests(unittest.TestCase):
    def test_accepts_a_valid_signature(self) -> None:
        self.assertTrue(verify(SECRET, BODY, sign(SECRET, BODY)))

    def test_rejects_a_body_changed_after_signing(self) -> None:
        header = sign(SECRET, BODY)
        self.assertFalse(verify(SECRET, BODY + b" ", header))

    def test_rejects_a_signature_from_another_secret(self) -> None:
        self.assertFalse(verify(SECRET, BODY, sign(b"other", BODY)))

    def test_rejects_a_missing_header(self) -> None:
        self.assertFalse(verify(SECRET, BODY, None))

    def test_rejects_the_legacy_sha1_prefix(self) -> None:
        header = "sha1=" + hmac.new(SECRET, BODY, hashlib.sha1).hexdigest()
        self.assertFalse(verify(SECRET, BODY, header))

    def test_rejects_a_truncated_digest(self) -> None:
        self.assertFalse(verify(SECRET, BODY, sign(SECRET, BODY)[:-2]))

    def test_rejects_non_hex_digest(self) -> None:
        self.assertFalse(verify(SECRET, BODY, "sha256=" + "z" * 64))

    def test_accepts_uppercase_hex(self) -> None:
        self.assertTrue(verify(SECRET, BODY, sign(SECRET, BODY).upper().replace("SHA256=", "sha256=")))


if __name__ == "__main__":
    unittest.main()
