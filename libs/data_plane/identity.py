"""Signed workload identity for the local and external capability planes.

HMAC-SHA256 is used so the libraries stay stdlib-only. The verifier is pluggable:
a deployment can swap the key material for a hardware-backed signer without
changing the claim or scope model.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass
from typing import Any, Mapping

from .errors import AuthenticationError, AuthorizationError, ContractError
from .model import SourceContract, Visibility

LOCAL_PROFILE = "local"
EXTERNAL_PROFILE = "external"
_PROFILES = frozenset({LOCAL_PROFILE, EXTERNAL_PROFILE})
MAX_IDENTITY_TTL_SECONDS = 3600


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64decode(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


@dataclass(frozen=True, order=True)
class Scope:
    source_id: str
    capability: str

    def to_list(self) -> list[str]:
        return [self.source_id, self.capability]

    @classmethod
    def from_list(cls, value: list[str]) -> "Scope":
        if len(value) != 2:
            raise AuthorizationError(f"bad scope {value!r}")
        return cls(source_id=value[0], capability=value[1])


@dataclass(frozen=True)
class WorkloadClaims:
    profile: str
    workload: str
    scopes: tuple[Scope, ...]
    issued_at: int
    expires_at: int
    nonce: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile": self.profile,
            "workload": self.workload,
            "scopes": [scope.to_list() for scope in self.scopes],
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
            "nonce": self.nonce,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "WorkloadClaims":
        try:
            scopes = tuple(Scope.from_list(item) for item in value["scopes"])
            return cls(
                profile=str(value["profile"]),
                workload=str(value["workload"]),
                scopes=scopes,
                issued_at=int(value["issued_at"]),
                expires_at=int(value["expires_at"]),
                nonce=str(value["nonce"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise AuthorizationError(f"malformed workload claims: {exc}") from exc


def encode_claims(claims: WorkloadClaims, key: bytes) -> str:
    if not key:
        raise AuthorizationError("signing key is empty")
    payload = _b64encode(canonical_json(claims.to_dict()))
    signature = _b64encode(hmac.new(key, payload.encode("ascii"), hashlib.sha256).digest())
    return f"{payload}.{signature}"


def decode_claims(
    token: str, key: bytes, now: int | None = None, max_skew: int = 60
) -> WorkloadClaims:
    if not key:
        raise AuthenticationError("verification key is empty")
    try:
        payload, signature = token.split(".", 1)
    except (ValueError, AttributeError) as exc:
        raise AuthenticationError("malformed identity token") from exc
    if not payload or not signature:
        raise AuthenticationError("malformed identity token")
    try:
        payload_bytes = payload.encode("ascii")
        provided = _b64decode(signature)
    except (UnicodeEncodeError, binascii.Error, ValueError) as exc:
        raise AuthenticationError("malformed identity token") from exc
    expected = hmac.new(key, payload_bytes, hashlib.sha256).digest()
    if not hmac.compare_digest(expected, provided):
        raise AuthenticationError("identity signature does not match")
    try:
        claims = WorkloadClaims.from_dict(json.loads(_b64decode(payload)))
    except (ValueError, binascii.Error, json.JSONDecodeError, AuthorizationError) as exc:
        raise AuthenticationError(f"malformed identity payload: {exc}") from exc
    current = int(time.time()) if now is None else int(now)
    if claims.issued_at > current + max_skew:
        raise AuthenticationError("identity was issued in the future")
    if claims.expires_at <= current:
        raise AuthenticationError("identity has expired")
    return claims


class IdentityIssuer:
    """Issues and verifies scoped identities against the source catalog."""

    def __init__(
        self,
        sources: Mapping[str, SourceContract],
        key: bytes,
        clock=time.time,
        max_ttl: int = MAX_IDENTITY_TTL_SECONDS,
    ) -> None:
        self._sources = dict(sources)
        self._key = key
        self._clock = clock
        if max_ttl < 1:
            raise AuthorizationError("max_ttl must be positive")
        self._max_ttl = max_ttl

    def issue(
        self,
        profile: str,
        workload: str,
        scopes: tuple[tuple[str, str], ...],
        ttl: int = 300,
    ) -> str:
        if profile not in _PROFILES:
            raise AuthorizationError(f"unknown profile {profile!r}")
        if not workload.strip():
            raise AuthorizationError("workload name is empty")
        if ttl < 1:
            raise AuthorizationError("ttl must be positive")
        if ttl > self._max_ttl:
            raise AuthorizationError(
                f"ttl {ttl} exceeds the maximum identity lifetime {self._max_ttl}"
            )
        resolved = tuple(
            Scope(source_id=source_id, capability=capability)
            for source_id, capability in scopes
        )
        for scope in resolved:
            self._check_scope(profile, scope)
        now = int(self._clock())
        claims = WorkloadClaims(
            profile=profile,
            workload=workload,
            scopes=resolved,
            issued_at=now,
            expires_at=now + ttl,
            nonce=secrets.token_hex(16),
        )
        return encode_claims(claims, self._key)

    def verify(self, token: str) -> WorkloadClaims:
        claims = decode_claims(token, self._key, now=int(self._clock()))
        if claims.profile not in _PROFILES:
            raise AuthorizationError(f"unknown profile {claims.profile!r}")
        for scope in claims.scopes:
            self._check_scope(claims.profile, scope)
        return claims

    def authorize(self, claims: WorkloadClaims, source_id: str, capability: str) -> None:
        scope = Scope(source_id=source_id, capability=capability)
        if scope not in claims.scopes:
            raise AuthorizationError(
                f"workload {claims.workload!r} lacks {source_id}:{capability}"
            )
        self._check_scope(claims.profile, scope)

    def _check_scope(self, profile: str, scope: Scope) -> None:
        source = self._sources.get(scope.source_id)
        if source is None:
            raise AuthorizationError(f"unknown source {scope.source_id!r}")
        try:
            source.capability(scope.capability)
        except ContractError as exc:
            raise AuthorizationError(str(exc)) from exc
        if source.visibility is Visibility.INFRASTRUCTURE:
            raise AuthorizationError("infrastructure sources are human-only")
        if profile == EXTERNAL_PROFILE and source.visibility is Visibility.PERSONAL:
            raise AuthorizationError(
                f"external profile cannot reach personal source {scope.source_id!r}"
            )
        if profile == LOCAL_PROFILE and source.visibility is Visibility.PERSONAL:
            return
        if profile == LOCAL_PROFILE and source.visibility is Visibility.GENERAL:
            return
        if profile == EXTERNAL_PROFILE and source.visibility is Visibility.GENERAL:
            return
        raise AuthorizationError(
            f"profile {profile!r} cannot reach {source.visibility.value!r} source"
        )
