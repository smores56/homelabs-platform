"""Signed workload identity and capability-plane separation tests."""

from __future__ import annotations

import unittest

from data_plane.errors import AuthorizationError
from data_plane.identity import (
    EXTERNAL_PROFILE,
    LOCAL_PROFILE,
    IdentityIssuer,
    Scope,
    decode_claims,
    encode_claims,
)
from data_plane.model import (
    Capability,
    EffectClass,
    SourceContract,
    Visibility,
)

KEY = b"test-key-not-a-secret"


def source(source_id: str, visibility: Visibility) -> SourceContract:
    return SourceContract(
        source_id=source_id,
        visibility=visibility,
        version=1,
        recovery_group=source_id,
        stable_id_fields=("id",),
        capabilities=(
            Capability(name="read", effect=EffectClass.READ),
            Capability(name="search", effect=EffectClass.SEARCH),
        ),
    )


class IdentityTests(unittest.TestCase):
    def setUp(self):
        self.sources = {
            "notes": source("notes", Visibility.PERSONAL),
            "books": source("books", Visibility.GENERAL),
            "infra": source("infra", Visibility.INFRASTRUCTURE),
        }
        self.now = 1_000_000
        self.issuer = IdentityIssuer(self.sources, KEY, clock=lambda: self.now)

    def test_external_profile_cannot_reach_personal(self):
        with self.assertRaises(AuthorizationError):
            self.issuer.issue(EXTERNAL_PROFILE, "agent", (("notes", "read"),))

    def test_external_profile_can_reach_general(self):
        token = self.issuer.issue(EXTERNAL_PROFILE, "agent", (("books", "read"),))
        claims = self.issuer.verify(token)
        self.assertEqual(claims.scopes, (Scope("books", "read"),))
        self.issuer.authorize(claims, "books", "read")

    def test_local_profile_can_reach_personal_and_general(self):
        token = self.issuer.issue(
            LOCAL_PROFILE, "agent", (("notes", "read"), ("books", "search"))
        )
        claims = self.issuer.verify(token)
        self.issuer.authorize(claims, "notes", "read")
        self.issuer.authorize(claims, "books", "search")

    def test_infrastructure_is_human_only(self):
        for profile in (LOCAL_PROFILE, EXTERNAL_PROFILE):
            with self.subTest(profile=profile):
                with self.assertRaises(AuthorizationError):
                    self.issuer.issue(profile, "agent", (("infra", "read"),))

    def test_unknown_capability_rejected(self):
        with self.assertRaises(AuthorizationError):
            self.issuer.issue(LOCAL_PROFILE, "agent", (("notes", "write"),))

    def test_signature_tamper_rejected(self):
        token = self.issuer.issue(LOCAL_PROFILE, "agent", (("notes", "read"),))
        payload, signature = token.split(".", 1)
        tampered = payload[:-1] + ("A" if payload[-1] != "A" else "B") + "." + signature
        with self.assertRaises(AuthorizationError):
            decode_claims(tampered, KEY, now=self.now)

    def test_expiry_rejected(self):
        token = self.issuer.issue(LOCAL_PROFILE, "agent", (("notes", "read"),), ttl=10)
        with self.assertRaises(AuthorizationError):
            decode_claims(token, KEY, now=self.now + 11)

    def test_scope_not_granted_rejected(self):
        token = self.issuer.issue(LOCAL_PROFILE, "agent", (("notes", "read"),))
        claims = self.issuer.verify(token)
        with self.assertRaises(AuthorizationError):
            self.issuer.authorize(claims, "books", "read")

    def test_claims_round_trip(self):
        token = self.issuer.issue(LOCAL_PROFILE, "agent", (("notes", "read"),))
        claims = decode_claims(token, KEY, now=self.now)
        self.assertEqual(decode_claims(encode_claims(claims, KEY), KEY, now=self.now), claims)


if __name__ == "__main__":
    unittest.main()
