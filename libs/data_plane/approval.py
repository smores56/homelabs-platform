"""Signed, single-use approvals that work across pods.

The in-process :class:`data_plane.confirmation.ConfirmationGate` binds an
approval to a token held in one process. A confirmation surface and a write
broker run in different pods, so the broker must verify an approval it never
issued. This module carries the same binding as an HMAC-signed claim set:

* the normalized operation hash, its intent id, and the source epoch are in the
  signed payload, so any change to the operation invalidates the approval;
* ``issued_at``/``expires_at`` bound how long the claim is live, and the
  verifier refuses a claim issued in the future or with a lifetime beyond
  ``max_ttl``;
* the nonce is consumed exactly once through a shared SQLite table, so a second
  broker (or a second pod) cannot replay it.

Nothing here knows a source adapter or reaches the network. SQLite is the same
stdlib dependency the intent journal already uses.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, runtime_checkable

from .confirmation import NormalizedOperation, operation_hash
from .errors import ConfirmationError
from .identity import canonical_json

MAX_APPROVAL_TTL_SECONDS = 900
DEFAULT_APPROVAL_TTL_SECONDS = 900

_NONCE_PATTERN = re.compile(r"\A[0-9A-Za-z_-]{8,128}\Z")
_HASH_PATTERN = re.compile(r"\A[0-9a-f]{64}\Z")
_BUSY_TIMEOUT_MS = 5000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS used_approvals (
    nonce TEXT PRIMARY KEY,
    intent_id TEXT NOT NULL,
    operation_hash TEXT NOT NULL,
    consumed_at REAL NOT NULL
);
"""


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64decode(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def _key_bytes(key: bytes | str) -> bytes:
    raw = key.encode("utf-8") if isinstance(key, str) else bytes(key)
    if not raw:
        raise ConfirmationError("approval key is empty")
    return raw


@dataclass(frozen=True)
class ApprovalClaims:
    """The signed facts an operator surface issues and a broker verifies."""

    intent_id: str
    operation_hash: str
    epoch: int
    approved_by: str
    issued_at: int
    expires_at: int
    nonce: str

    def __post_init__(self) -> None:
        if not isinstance(self.intent_id, str) or not self.intent_id.strip():
            raise ConfirmationError("approval intent id is required")
        if not isinstance(self.operation_hash, str) or not _HASH_PATTERN.match(
            self.operation_hash
        ):
            raise ConfirmationError("approval operation hash is not a SHA-256")
        if isinstance(self.epoch, bool) or not isinstance(self.epoch, int) or self.epoch < 1:
            raise ConfirmationError("approval epoch must be a positive integer")
        if not isinstance(self.approved_by, str) or not self.approved_by.strip():
            raise ConfirmationError("approval must name an approver")
        for name in ("issued_at", "expires_at"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise ConfirmationError(f"approval {name} must be an integer")
        if self.expires_at <= self.issued_at:
            raise ConfirmationError("approval expiry must be after its issue time")
        if not isinstance(self.nonce, str) or not _NONCE_PATTERN.match(self.nonce):
            raise ConfirmationError("approval nonce is malformed")

    @property
    def approved_at(self) -> int:
        """Alias so the broker's audit record works for either approval type."""

        return self.issued_at

    @property
    def ttl(self) -> int:
        return self.expires_at - self.issued_at

    def to_dict(self) -> dict[str, Any]:
        return {
            "intent_id": self.intent_id,
            "operation_hash": self.operation_hash,
            "epoch": self.epoch,
            "approved_by": self.approved_by,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
            "nonce": self.nonce,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ApprovalClaims":
        try:
            return cls(
                intent_id=str(value["intent_id"]),
                operation_hash=str(value["operation_hash"]),
                epoch=int(value["epoch"]),
                approved_by=str(value["approved_by"]),
                issued_at=int(value["issued_at"]),
                expires_at=int(value["expires_at"]),
                nonce=str(value["nonce"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ConfirmationError(f"malformed approval claims: {exc}") from exc


def sign_approval(claims: ApprovalClaims, key: bytes | str) -> str:
    """Sign claims into ``payload.signature`` with HMAC-SHA256."""

    payload = _b64encode(canonical_json(claims.to_dict()))
    signature = _b64encode(
        hmac.new(_key_bytes(key), payload.encode("ascii"), hashlib.sha256).digest()
    )
    return f"{payload}.{signature}"


def verify_approval(
    token: str,
    key: bytes | str,
    now: int | None = None,
    max_ttl: int = MAX_APPROVAL_TTL_SECONDS,
) -> ApprovalClaims:
    """Verify a signed approval and return its claims.

    Rejects a tampered payload or signature, a token issued in the future, an
    expired token, and a lifetime longer than ``max_ttl``. Single use is the
    store's job; this function never mutates state.
    """

    if isinstance(max_ttl, bool) or not isinstance(max_ttl, int) or max_ttl < 1:
        raise ConfirmationError("approval max_ttl must be a positive integer")
    key_bytes = _key_bytes(key)
    try:
        payload, signature = token.split(".", 1)
    except (ValueError, AttributeError) as exc:
        raise ConfirmationError("malformed approval token") from exc
    if not payload or not signature:
        raise ConfirmationError("malformed approval token")
    try:
        payload_bytes = payload.encode("ascii")
        provided = _b64decode(signature)
        decoded = json.loads(_b64decode(payload))
    except (UnicodeEncodeError, binascii.Error, ValueError, json.JSONDecodeError) as exc:
        raise ConfirmationError("malformed approval token") from exc
    expected = hmac.new(key_bytes, payload_bytes, hashlib.sha256).digest()
    if not hmac.compare_digest(expected, provided):
        raise ConfirmationError("approval signature does not match")
    if not isinstance(decoded, dict):
        raise ConfirmationError("malformed approval payload")
    claims = ApprovalClaims.from_dict(decoded)

    current = int(time.time()) if now is None else int(now)
    if claims.issued_at > current:
        raise ConfirmationError("approval was issued in the future")
    if claims.expires_at <= current:
        raise ConfirmationError("approval has expired")
    if claims.expires_at - claims.issued_at > max_ttl:
        raise ConfirmationError(
            f"approval lifetime exceeds the {max_ttl}-second maximum"
        )
    return claims


def claims_for(
    operation: NormalizedOperation,
    approved_by: str,
    *,
    issued_at: int,
    ttl: int = DEFAULT_APPROVAL_TTL_SECONDS,
    nonce: str | None = None,
) -> ApprovalClaims:
    """Build claims bound to one normalized operation."""

    if isinstance(ttl, bool) or not isinstance(ttl, int) or ttl < 1:
        raise ConfirmationError("approval ttl must be a positive integer")
    return ApprovalClaims(
        intent_id=operation.intent_id,
        operation_hash=operation_hash(operation),
        epoch=operation.epoch,
        approved_by=approved_by,
        issued_at=int(issued_at),
        expires_at=int(issued_at) + ttl,
        nonce=nonce if nonce is not None else secrets.token_hex(16),
    )


class UsedApprovalStore:
    """A shared SQLite table of consumed nonces, keyed by nonce.

    The primary key makes the first insert the only successful one, so two
    processes (or two threads) cannot both consume the same approval even when
    they share one database file.
    """

    def __init__(self, path: str | Path = ":memory:") -> None:
        self._path = str(path)
        if self._path != ":memory:":
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(
            self._path, check_same_thread=False, isolation_level=None
        )
        self._conn.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
        self._conn.executescript(_SCHEMA)

    def consume(
        self,
        nonce: str,
        *,
        intent_id: str = "",
        operation_hash: str = "",
        now: float | None = None,
    ) -> None:
        """Mark ``nonce`` used; a second use raises ``ConfirmationError``.

        ``intent_id`` and ``operation_hash`` are stored beside the nonce so an
        audit can prove which operation the one-time approval paid for.
        """

        if not isinstance(nonce, str) or not _NONCE_PATTERN.match(nonce):
            raise ConfirmationError("approval nonce is malformed")
        consumed_at = float(time.time() if now is None else now)
        with self._lock:
            try:
                self._conn.execute(
                    "INSERT INTO used_approvals "
                    "(nonce, intent_id, operation_hash, consumed_at) "
                    "VALUES (?, ?, ?, ?)",
                    (nonce, str(intent_id), str(operation_hash), consumed_at),
                )
            except sqlite3.IntegrityError as exc:
                raise ConfirmationError(
                    "approval was already used; request a new confirmation"
                ) from exc

    def used(self, nonce: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM used_approvals WHERE nonce = ?", (nonce,)
            ).fetchone()
        return row is not None

    def close(self) -> None:
        with self._lock:
            self._conn.close()


@runtime_checkable
class ApprovalVerifier(Protocol):
    """What the broker needs to verify a signed approval cross-pod."""

    def verify(self, token: str, operation: NormalizedOperation) -> ApprovalClaims:
        """Return the claims when they match ``operation`` and consume the nonce."""


class SignedApprovalVerifier:
    """Verify signed approvals against one key and one shared nonce store."""

    def __init__(
        self,
        key: bytes | str,
        store: UsedApprovalStore,
        *,
        clock: Callable[[], float] = time.time,
        max_ttl: int = MAX_APPROVAL_TTL_SECONDS,
    ) -> None:
        if isinstance(max_ttl, bool) or not isinstance(max_ttl, int) or max_ttl < 1:
            raise ConfirmationError("approval max_ttl must be a positive integer")
        self._key = _key_bytes(key)
        self._store = store
        self._clock = clock
        self._max_ttl = max_ttl

    @property
    def max_ttl(self) -> int:
        return self._max_ttl

    def issue(
        self,
        operation: NormalizedOperation,
        approved_by: str,
        *,
        ttl: int | None = None,
    ) -> tuple[ApprovalClaims, str]:
        """Issue one approval for an operation; the caller is the operator."""

        lifetime = self._max_ttl if ttl is None else ttl
        if lifetime < 1 or lifetime > self._max_ttl:
            raise ConfirmationError(
                f"approval ttl must be between 1 and {self._max_ttl} seconds"
            )
        claims = claims_for(
            operation,
            approved_by,
            issued_at=int(self._clock()),
            ttl=lifetime,
        )
        return claims, sign_approval(claims, self._key)

    def verify(self, token: str, operation: NormalizedOperation) -> ApprovalClaims:
        claims = verify_approval(
            token, self._key, now=int(self._clock()), max_ttl=self._max_ttl
        )
        if claims.intent_id != operation.intent_id:
            raise ConfirmationError("approval was issued for another intent")
        if claims.operation_hash != operation_hash(operation):
            raise ConfirmationError("approval does not match the operation")
        if claims.epoch != operation.epoch:
            raise ConfirmationError("source epoch changed; re-confirm the operation")
        # Only a fully matched approval spends its nonce, so a wrong intent or
        # hash cannot burn a valid operator approval.
        self._store.consume(
            claims.nonce,
            intent_id=claims.intent_id,
            operation_hash=claims.operation_hash,
        )
        return claims
