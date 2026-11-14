"""Human confirmation boundary for destructive and cross-domain operations.

The confirmation surface is intentionally outside agent control. The gate binds
a single-use token to a hash of the normalized operation, its intent id, and the
source epoch. Any change to the operation, reuse, expiry, or epoch change
invalidates the approval.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from .errors import ConfirmationError
from .identity import canonical_json


@dataclass(frozen=True)
class FieldChange:
    field: str
    before: Any
    after: Any

    def as_list(self) -> list[Any]:
        return [self.field, self.before, self.after]


@dataclass(frozen=True)
class NormalizedOperation:
    intent_id: str
    source_id: str
    capability: str
    targets: tuple[str, ...]
    base_revision: str | None
    field_diff: tuple[FieldChange, ...]
    affected_count: int
    epoch: int
    recurrence: str | None = None
    attendee_scope: tuple[str, ...] = ()
    destination: str | None = None

    def normalized(self) -> dict[str, Any]:
        return {
            "intent_id": self.intent_id,
            "source_id": self.source_id,
            "capability": self.capability,
            "targets": list(self.targets),
            "base_revision": self.base_revision,
            "field_diff": [
                change.as_list()
                for change in sorted(self.field_diff, key=lambda item: item.field)
            ],
            "affected_count": self.affected_count,
            "epoch": self.epoch,
            "recurrence": self.recurrence,
            "attendee_scope": list(self.attendee_scope),
            "destination": self.destination,
        }

    @classmethod
    def from_normalized(cls, value: Mapping[str, Any]) -> "NormalizedOperation":
        """Rebuild an operation from its canonical JSON form."""

        try:
            return cls(
                intent_id=str(value["intent_id"]),
                source_id=str(value["source_id"]),
                capability=str(value["capability"]),
                targets=tuple(str(item) for item in value.get("targets", ())),
                base_revision=value.get("base_revision"),
                field_diff=tuple(
                    FieldChange(
                        field=str(item[0]), before=item[1], after=item[2]
                    )
                    for item in value.get("field_diff", ())
                ),
                affected_count=int(value.get("affected_count", 0)),
                epoch=int(value.get("epoch", 1)),
                recurrence=value.get("recurrence"),
                attendee_scope=tuple(
                    str(item) for item in value.get("attendee_scope", ())
                ),
                destination=value.get("destination"),
            )
        except (KeyError, TypeError, ValueError, IndexError) as exc:
            raise ConfirmationError(
                f"stored operation is malformed: {exc}"
            ) from exc


def operation_hash(operation: NormalizedOperation) -> str:
    return hashlib.sha256(canonical_json(operation.normalized())).hexdigest()


def _quote(value: Any) -> str:
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=True)
    return json.dumps(value, ensure_ascii=True, sort_keys=True)


def render(operation: NormalizedOperation) -> str:
    """Render a plain-text confirmation card.

    No HTML or remote resources are emitted, so a terminal or mail client cannot
    activate content from a source-controlled field.
    """

    lines = [
        "Confirmation required",
        f"intent: {operation.intent_id}",
        f"source: {operation.source_id}",
        f"capability: {operation.capability}",
        f"epoch: {operation.epoch}",
        f"base revision: {operation.base_revision or '(none)'}",
        f"affected count: {operation.affected_count}",
        f"targets: {', '.join(_quote(item) for item in operation.targets)}",
        f"destination: {_quote(operation.destination) if operation.destination else '(none)'}",
    ]
    if operation.recurrence:
        lines.append(f"recurrence: {_quote(operation.recurrence)}")
    if operation.attendee_scope:
        lines.append(
            "attendees: " + ", ".join(_quote(item) for item in operation.attendee_scope)
        )
    if operation.field_diff:
        lines.append("field diff:")
        for change in sorted(operation.field_diff, key=lambda item: item.field):
            lines.append(
                f"  {change.field}: {_quote(change.before)} -> {_quote(change.after)}"
            )
    else:
        lines.append("field diff: (none)")
    return "\n".join(lines)


@dataclass(frozen=True)
class Approval:
    operation_hash: str
    intent_id: str
    epoch: int
    approved_by: str
    approved_at: int
    expires_at: int
    nonce: str


@dataclass
class _Entry:
    approval: Approval
    used: bool = False
    invalidated: bool = False


@dataclass
class ConfirmationGate:
    ttl_seconds: int = 900
    clock: Any = field(default=time.time)

    def __post_init__(self) -> None:
        self._entries: dict[str, _Entry] = {}

    def issue(
        self, operation: NormalizedOperation, approved_by: str
    ) -> tuple[Approval, str]:
        if not approved_by.strip():
            raise ConfirmationError("approved_by is required")
        now = int(self.clock())
        approval = Approval(
            operation_hash=operation_hash(operation),
            intent_id=operation.intent_id,
            epoch=operation.epoch,
            approved_by=approved_by,
            approved_at=now,
            expires_at=now + self.ttl_seconds,
            nonce=secrets.token_hex(16),
        )
        token = secrets.token_urlsafe(32)
        self._entries[token] = _Entry(approval=approval)
        return approval, token

    def verify(self, token: str, operation: NormalizedOperation) -> Approval:
        entry = self._entries.get(token)
        if entry is None:
            raise ConfirmationError("unknown confirmation token")
        if entry.invalidated:
            raise ConfirmationError("confirmation was invalidated")
        if entry.used:
            raise ConfirmationError("confirmation token was already used")
        approval = entry.approval
        now = int(self.clock())
        if now >= approval.expires_at:
            raise ConfirmationError("confirmation expired")
        if approval.operation_hash != operation_hash(operation):
            raise ConfirmationError("confirmation does not match the operation")
        if approval.intent_id != operation.intent_id:
            raise ConfirmationError("confirmation was issued for another intent")
        if approval.epoch != operation.epoch:
            raise ConfirmationError("source epoch changed; re-confirm the operation")
        entry.used = True
        return approval

    def invalidate(self, token: str) -> None:
        entry = self._entries.get(token)
        if entry is None:
            raise ConfirmationError("unknown confirmation token")
        entry.invalidated = True

    def pending(self) -> Iterable[tuple[str, Approval]]:
        for token, entry in self._entries.items():
            if not entry.used and not entry.invalidated:
                yield token, entry.approval
