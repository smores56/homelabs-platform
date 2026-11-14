"""Operator-only confirmation surface for signed, cross-pod approvals.

The write broker runs in another pod and never holds the operator key. This
service is the only place an approval is minted:

* it authenticates with ``X-Operator-Token``, a secret distinct from the
  workload identity key and the event-service token;
* it accepts only a normalized operation that matches the source catalog, so a
  malformed, unknown, or epoch-stale operation is refused before it is signed;
* it signs an :class:`~data_plane.approval.ApprovalClaims` bound to the
  operation hash, intent id, and epoch. The broker consumes the nonce in a
  shared store, so a token works once and only for the operation it names.

``GET /healthz`` is the only unauthenticated route and names nothing else. There
is deliberately no route that accepts a workload token: a tenant cannot reach
this surface through the broker, and no request without the operator token can
mint an approval.
"""

from __future__ import annotations

import hmac
import secrets
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from data_plane.approval import (
    DEFAULT_APPROVAL_TTL_SECONDS,
    MAX_APPROVAL_TTL_SECONDS,
    ApprovalClaims,
    UsedApprovalStore,
    claims_for,
    sign_approval,
)
from data_plane.confirmation import NormalizedOperation, operation_hash
from data_plane.errors import DataPlaneError
from data_plane.model import EffectClass, SourceContract

from . import common

OPERATOR_TOKEN_HEADER = "x-operator-token"

_READ_EFFECTS = frozenset({EffectClass.READ, EffectClass.SEARCH})


def _secret_bytes(secret: bytes | str | None) -> bytes | None:
    if secret is None:
        return None
    raw = secret.encode("utf-8") if isinstance(secret, str) else bytes(secret)
    return raw or None


@dataclass(frozen=True)
class ConfirmationService:
    """Routes for one operator surface and its signing key."""

    key: bytes | str
    operator_secret: bytes | str | None
    sources: Mapping[str, SourceContract]
    store: UsedApprovalStore | None = None
    max_ttl: int = MAX_APPROVAL_TTL_SECONDS
    clock: Callable[[], float] = time.time

    def __post_init__(self) -> None:
        if isinstance(self.max_ttl, bool) or not isinstance(self.max_ttl, int):
            raise ValueError("max_ttl must be a positive integer")
        if self.max_ttl < 1 or self.max_ttl > MAX_APPROVAL_TTL_SECONDS:
            raise ValueError(
                f"max_ttl must be between 1 and {MAX_APPROVAL_TTL_SECONDS} seconds"
            )
        if not _secret_bytes(self.key):
            raise ValueError("operator signing key is empty")

    @property
    def routes(self) -> common.Routes:
        return {
            ("POST", "/approve"): self.approve,
            ("GET", "/healthz"): self.health,
        }

    def _require_operator(self, request: Any) -> None:
        secret = _secret_bytes(self.operator_secret)
        if secret is None:
            # Fail closed: a surface with no operator token can never mint one.
            raise common.HttpError(
                503, "unavailable", "confirmation has no operator token configured"
            )
        presented = request.headers.get(OPERATOR_TOKEN_HEADER, "").strip()
        if not presented:
            raise common.HttpError(401, "unauthorized", "missing operator token")
        if not hmac.compare_digest(
            presented.encode("utf-8", "ignore"), secret
        ):
            raise common.HttpError(403, "forbidden", "operator token does not match")

    def _operation(self, body: Mapping[str, Any]) -> NormalizedOperation:
        raw = body.get("operation")
        if not isinstance(raw, Mapping):
            raise common.HttpError(
                400, "validation", "operation must be a normalized JSON object"
            )
        try:
            operation = NormalizedOperation.from_normalized(raw)
        except DataPlaneError as exc:
            raise common.HttpError(400, "validation", str(exc)) from exc

        if not operation.intent_id.strip():
            raise common.HttpError(400, "validation", "operation has no intent id")
        if not operation.targets:
            raise common.HttpError(400, "validation", "operation has no targets")
        if operation.affected_count != len(operation.targets):
            raise common.HttpError(
                400, "validation", "operation affected_count does not match targets"
            )

        contract = self.sources.get(operation.source_id)
        if contract is None:
            raise common.HttpError(
                400, "validation", f"unknown source {operation.source_id!r}"
            )
        try:
            capability = contract.capability(operation.capability)
        except DataPlaneError as exc:
            raise common.HttpError(400, "validation", str(exc)) from exc
        if capability.effect in _READ_EFFECTS:
            raise common.HttpError(
                400,
                "validation",
                f"capability {capability.name!r} is {capability.effect.value}, not a write",
            )
        if operation.epoch != contract.epoch:
            raise common.HttpError(
                409,
                "epoch",
                f"source {operation.source_id!r} is at epoch {contract.epoch}, "
                f"not {operation.epoch}",
            )

        expected_hash = operation_hash(operation)
        supplied = body.get("operation_hash")
        if supplied is not None and supplied != expected_hash:
            raise common.HttpError(
                400, "validation", "operation_hash does not match the operation"
            )
        return operation

    def approve(self, request: Any, query: common.Query) -> tuple[int, Any]:
        # Authenticate before reading the body so an unauthenticated caller
        # cannot make the service parse or echo anything.
        self._require_operator(request)
        body = common.read_json_object(request)
        operation = self._operation(body)

        approved_by = common.field_str(body, "approved_by")
        if len(approved_by) > 128 or any(ord(ch) < 32 for ch in approved_by):
            raise common.HttpError(
                400, "validation", "approved_by must be printable and at most 128 chars"
            )
        ttl = common.field_optional_int(body, "ttl")
        if ttl is None:
            ttl = min(self.max_ttl, DEFAULT_APPROVAL_TTL_SECONDS)
        if ttl < 1 or ttl > self.max_ttl:
            raise common.HttpError(
                400,
                "validation",
                f"ttl must be between 1 and {self.max_ttl} seconds",
            )

        claims: ApprovalClaims = claims_for(
            operation,
            approved_by,
            issued_at=int(self.clock()),
            ttl=ttl,
            nonce=secrets.token_hex(16),
        )
        token = sign_approval(claims, self.key)
        return 200, {"approval_token": token, "claims": claims.to_dict()}

    def health(self, request: Any, query: common.Query) -> tuple[int, Any]:
        return 200, {"status": "ok", "service": "confirmation"}
