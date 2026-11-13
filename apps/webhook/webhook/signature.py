"""GitHub `X-Hub-Signature-256` verification.

The header is `<hex HMAC-SHA256>` prefixed with `sha256=`. The comparison is
constant-time; a malformed header is a refusal, never a fallback to "no secret
configured" (that decision belongs to the caller, which knows the source).
"""

from __future__ import annotations

import hashlib
import hmac

SIGNATURE_HEADER = "X-Hub-Signature-256"
PREFIX = "sha256="
_HEX = frozenset("0123456789abcdef")


def verify(secret: bytes, body: bytes, header: str | None) -> bool:
    """Return whether `header` carries the HMAC of `body` under `secret`."""
    if not header or not header.startswith(PREFIX):
        return False
    digest = header[len(PREFIX) :].strip().lower()
    if len(digest) != 64 or any(char not in _HEX for char in digest):
        return False
    expected = hmac.new(secret, body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, digest)
