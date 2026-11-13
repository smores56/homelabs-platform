"""GitHub App token minting: JWT shape, scoped requests, cache and failures."""

from __future__ import annotations

import base64
import json
import unittest
from datetime import datetime, timezone

from broker.github_app import CredentialUnavailable, GithubAppTokens

APP_ID = 4242
INSTALLATION = 99
KEY = b"-----BEGIN PRIVATE KEY-----\nfake\n-----END PRIVATE KEY-----\n"


def b64decode(part: str) -> bytes:
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


class Clock:
    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class Transport:
    def __init__(self, expires_in: int = 3600, status: int = 201, token: str = "ghs_minted") -> None:
        self.calls: list[tuple[str, str, dict]] = []
        self.expires_in = expires_in
        self.status = status
        self.token = token
        self.clock: Clock | None = None

    def __call__(self, url: str, jwt: str, body: dict) -> tuple[int, object]:
        self.calls.append((url, jwt, dict(body)))
        expires = datetime.fromtimestamp(self.clock.now + self.expires_in, tz=timezone.utc)
        return self.status, {"token": self.token, "expires_at": expires.strftime("%Y-%m-%dT%H:%M:%SZ")}


def build(clock: Clock | None = None, transport: Transport | None = None):
    clock = clock or Clock(1_800_000_000)
    transport = transport or Transport()
    transport.clock = clock
    signer = lambda message: b"signature"  # noqa: E731
    tokens = GithubAppTokens(APP_ID, KEY, signer=signer, transport=transport, clock=clock)
    return tokens, transport, clock


class JwtTests(unittest.TestCase):
    def test_the_jwt_is_rs256_with_bounded_claims(self):
        tokens, _, clock = build()
        header_raw, claims_raw, signature = tokens.jwt().split(".")
        header = json.loads(b64decode(header_raw))
        claims = json.loads(b64decode(claims_raw))
        self.assertEqual(header, {"alg": "RS256", "typ": "JWT"})
        self.assertEqual(claims["iss"], APP_ID)
        self.assertEqual(claims["iat"], int(clock.now) - 60)
        self.assertEqual(claims["exp"], int(clock.now) + 540)
        self.assertLessEqual(claims["exp"] - claims["iat"], 600)
        self.assertEqual(b64decode(signature), b"signature")

    def test_a_second_jwt_is_minted_for_a_second_request(self):
        tokens, transport, clock = build()
        tokens.token(INSTALLATION, ("repo",), {"contents": "write"})
        clock.now += 3400
        tokens.token(INSTALLATION, ("repo",), {"contents": "write"})
        first = json.loads(b64decode(transport.calls[0][1].split(".")[1]))
        second = json.loads(b64decode(transport.calls[1][1].split(".")[1]))
        self.assertNotEqual(first["iat"], second["iat"])


class ScopeTests(unittest.TestCase):
    def test_the_request_names_the_installation_and_the_exact_scope(self):
        tokens, transport, _ = build()
        self.assertEqual(tokens.token(INSTALLATION, ("homelabs-apps",), {"contents": "write"}), "ghs_minted")
        (url, _, body), = transport.calls
        self.assertEqual(url, f"https://api.github.com/app/installations/{INSTALLATION}/access_tokens")
        self.assertEqual(body, {"permissions": {"contents": "write"}, "repositories": ["homelabs-apps"]})

    def test_different_scopes_are_different_tokens(self):
        tokens, transport, _ = build()
        tokens.token(INSTALLATION, ("homelabs-apps",), {"contents": "write"})
        tokens.token(INSTALLATION, ("homelabs-platform",), {"contents": "read"})
        self.assertEqual(len(transport.calls), 2)

    def test_a_token_is_reused_inside_its_lifetime(self):
        tokens, transport, clock = build()
        tokens.token(INSTALLATION, ("repo",), {"contents": "write"})
        clock.now += 600
        tokens.token(INSTALLATION, ("repo",), {"contents": "write"})
        self.assertEqual(len(transport.calls), 1)

    def test_a_token_is_refreshed_before_it_expires(self):
        tokens, transport, clock = build()
        tokens.token(INSTALLATION, ("repo",), {"contents": "write"})
        # 3600 s lifetime, 300 s margin: the second call is inside the margin.
        clock.now += 3301
        tokens.token(INSTALLATION, ("repo",), {"contents": "write"})
        self.assertEqual(len(transport.calls), 2)

    def test_active_tokens_are_visible_for_redaction(self):
        tokens, _, _ = build()
        self.assertEqual(tokens.active_tokens(), [])
        tokens.token(INSTALLATION, ("repo",), {"contents": "write"})
        self.assertEqual(tokens.active_tokens(), ["ghs_minted"])


class FailureTests(unittest.TestCase):
    def test_a_refused_request_is_fatal(self):
        tokens, _, _ = build(transport=Transport(status=403))
        with self.assertRaises(CredentialUnavailable):
            tokens.token(INSTALLATION, ("repo",), {"contents": "write"})

    def test_a_response_without_a_token_is_fatal(self):
        tokens, _, _ = build(transport=Transport(token=""))
        with self.assertRaises(CredentialUnavailable):
            tokens.token(INSTALLATION, ("repo",), {"contents": "write"})

    def test_a_failure_is_not_cached(self):
        failing = Transport(status=500)
        tokens, _, clock = build(transport=failing)
        with self.assertRaises(CredentialUnavailable):
            tokens.token(INSTALLATION, ("repo",), {"contents": "write"})
        failing.status = 201
        self.assertEqual(tokens.token(INSTALLATION, ("repo",), {"contents": "write"}), "ghs_minted")


if __name__ == "__main__":
    unittest.main()
