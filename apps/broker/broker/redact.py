"""Redaction of known credential values from headers, bodies and log text.

Only exact known values and their common encodings are replaced. This is a
damage limiter, not a guarantee: a provider that transforms, compresses or
encrypts a secret can return it in a shape this cannot recognize (spec 04, 6.7).
"""

from __future__ import annotations

import base64
import html
import json
from urllib.parse import quote, quote_plus

# Headers that can carry a URL or a reflected credential.
SENSITIVE_HEADERS = frozenset(
    {
        "location",
        "content-location",
        "link",
        "refresh",
        "set-cookie",
        "www-authenticate",
        "proxy-authenticate",
    }
)


def secret_variants(secret: str) -> set[str]:
    """The shapes a secret takes when it passes through a URL, JSON, HTML or base64."""
    if not secret:
        return set()
    raw = secret.encode()
    return {
        secret,
        quote(secret, safe=""),
        quote_plus(secret),
        json.dumps(secret)[1:-1],
        secret.replace("/", "\\/"),
        html.escape(secret, quote=True),
        base64.b64encode(raw).decode(),
        base64.urlsafe_b64encode(raw).decode().rstrip("="),
    }


def redact(text: str, secrets: dict[str, str], *, label: str) -> str:
    """Replace every known credential (and encoding) with `[REDACTED:<label>]`."""
    if not text or not secrets:
        return text
    marker = f"[REDACTED:{label}]"
    variants: set[str] = set()
    for value in secrets.values():
        variants |= secret_variants(value)
    # Longest first: a variant of a longer secret must not be half-replaced.
    for variant in sorted(variants, key=len, reverse=True):
        if variant:
            text = text.replace(variant, marker)
    return text


def redact_header(name: str, value: str, secrets: dict[str, str], *, label: str) -> str:
    if name.lower() in SENSITIVE_HEADERS:
        return redact(value, secrets, label=label)
    return value
