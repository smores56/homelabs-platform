"""Scoped read and write broker for the portable data plane.

The broker is the only component that turns a signed workload identity into an
adapter call. Reads are authorization plus a single adapter call; writes always
pass through an intent so a retry is idempotent, a stale revision is rejected
before the effect lands, and an ambiguous effect is never replayed blindly.

Storage is injected: ``IntentStore`` owns durable intent state and
``EffectRecorder`` owns the authoritative effect journal. Neither is implemented
here, so the same broker serves the local and external planes with different
journals.
"""

from __future__ import annotations

import hashlib
import re
import threading
import time
import uuid
from dataclasses import dataclass, replace
from typing import Any, Callable, Mapping, Protocol, runtime_checkable

from .confirmation import ConfirmationGate, FieldChange, NormalizedOperation
from .errors import (
    ConfirmationRequired,
    ContractError,
    EpochError,
    IdempotencyConflict,
    OutcomeUnknown,
    StaleError,
    ValidationError,
)
from .identity import LOCAL_PROFILE, IdentityIssuer, WorkloadClaims
from .model import Capability, EffectClass, IntentState, SourceContract, Visibility

Clock = Callable[[], float]

_READ_EFFECTS = frozenset({EffectClass.READ, EffectClass.SEARCH})
# States in which an effect may have landed without the broker observing it.
_RECONCILABLE = frozenset(
    {IntentState.APPLYING, IntentState.OUTCOME_UNKNOWN, IntentState.FAILED}
)
# States from which a write may proceed; anything else is terminal or ambiguous.
_APPLICABLE = frozenset(
    {IntentState.PROPOSED, IntentState.AWAITING_CONFIRMATION, IntentState.APPROVED}
)

_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_SECRET_PATTERN = re.compile(
    r"(?i)(token|secret|password|passwd|key)\s*[=:]\s*\S+"
)
_MAX_ERROR_TEXT = 160
_MAX_REMEMBERED_APPROVALS = 1024


def _describe_exception(exc: BaseException) -> str:
    """A bounded, secret-redacted, control-free description safe for the wire."""

    text = _SECRET_PATTERN.sub(r"\1=<redacted>", str(exc))
    text = _CONTROL_CHARACTERS.sub(" ", text)[:_MAX_ERROR_TEXT]
    return f"{type(exc).__name__}: {text}".strip()


@runtime_checkable
class SourceAdapter(Protocol):
    """The four operations one source exposes to the broker.

    An adapter is the source's only credential holder. It reports a revision per
    target for compare-and-swap, reads one record, searches within the source,
    and applies a normalized operation under the caller's idempotency key.
    """

    def revision(self, target: str) -> str | None:
        """Return the current revision of ``target``, or ``None`` if absent."""

    def read(self, target: str) -> dict[str, Any]:
        """Return one record; an absent target yields an empty mapping."""

    def search(self, query: str) -> list[dict[str, Any]]:
        """Return records in this source matching ``query``."""

    def apply(
        self, operation: NormalizedOperation, idempotency_key: str
    ) -> dict[str, Any]:
        """Apply the operation once for the key and return the source's result."""


@runtime_checkable
class IntentStore(Protocol):
    """Durable intent state, keyed by intent id and by idempotency key."""

    def get(self, intent_id: str) -> Intent | None:
        """Return the intent with ``intent_id`` or ``None``."""

    def find(self, idempotency_key: str) -> Intent | None:
        """Return the intent proposed under ``idempotency_key`` or ``None``."""

    def save(self, intent: Intent) -> None:
        """Persist the intent, replacing any prior revision of it."""


@runtime_checkable
class EffectRecorder(Protocol):
    """Authoritative effect journal written after a verified apply."""

    def record(self, event: Mapping[str, Any]) -> None:
        """Append one effect event."""


@dataclass(frozen=True)
class WriteRequest:
    """A caller's proposed write, before the broker normalizes it.

    ``epoch`` lets a caller pin the source epoch it observed; ``None`` accepts
    the contract's current epoch and is how a caller asks for replay.
    """

    source_id: str
    capability: str
    targets: tuple[str, ...]
    idempotency_key: str
    field_diff: tuple[FieldChange, ...] = ()
    base_revision: str | None = None
    recurrence: str | None = None
    attendee_scope: tuple[str, ...] = ()
    destination: str | None = None
    epoch: int | None = None


@dataclass(frozen=True)
class Intent:
    """One write's durable state and the exact operation it authorizes."""

    intent_id: str
    state: IntentState
    operation: NormalizedOperation
    idempotency_key: str
    requires_confirmation: bool
    created_at: int
    updated_at: int
    effect: dict[str, Any] | None = None
    error: str | None = None


class Broker:
    """Scoped read and confirmed, revision-checked write entry point."""

    def __init__(
        self,
        sources: Mapping[str, SourceContract],
        adapters: Mapping[str, SourceAdapter],
        issuer: IdentityIssuer,
        gate: ConfirmationGate,
        intents: IntentStore,
        events: EffectRecorder,
        clock: Clock = time.time,
    ) -> None:
        self._sources = dict(sources)
        self._adapters = dict(adapters)
        self._issuer = issuer
        self._gate = gate
        self._intents = intents
        self._events = events
        self._clock = clock
        # Single-writer serialization: the revision check, the adapter apply,
        # and the read-back must not interleave with another write.
        self._lock = threading.RLock()
        self._approvals: dict[str, dict[str, Any]] = {}

    # -- reads ---------------------------------------------------------

    def read(
        self, claims: WorkloadClaims | str, source_id: str, target: str
    ) -> dict[str, Any]:
        """Authorize ``read`` on ``source_id`` and return one record."""

        self._authorize(claims, source_id, "read")
        return self._adapter(source_id).read(target)

    def search(
        self, claims: WorkloadClaims | str, source_id: str, query: str
    ) -> list[dict[str, Any]]:
        """Authorize ``search`` on ``source_id`` and return matching records."""

        self._authorize(claims, source_id, "search")
        return self._adapter(source_id).search(query)

    # -- writes --------------------------------------------------------

    def propose(
        self, claims: WorkloadClaims | str, request: WriteRequest
    ) -> tuple[Intent, NormalizedOperation]:
        """Normalize and persist a write, returning its intent and operation.

        The same idempotency key with the same operation replays the prior
        intent; a different operation under that key is a conflict.
        """

        resolved = self._authorize(claims, request.source_id, request.capability)
        contract = self._contract(request.source_id)
        capability = contract.capability(request.capability)
        if capability.effect in _READ_EFFECTS:
            raise ContractError(
                f"capability {capability.name!r} is {capability.effect.value}, not a write"
            )
        self._validate_request(request, contract)

        existing = self._intents.find(request.idempotency_key)
        if existing is not None:
            self._require_epoch(existing.operation, contract)
            operation = self._build(request, contract, existing.intent_id)
            self._require_same_operation(existing.operation, operation)
            return existing, operation

        operation = self._build(request, contract, uuid.uuid4().hex)
        requires = self._needs_confirmation(resolved, contract, capability)
        now = int(self._clock())
        intent = Intent(
            intent_id=operation.intent_id,
            state=(
                IntentState.AWAITING_CONFIRMATION
                if requires
                else IntentState.APPROVED
            ),
            operation=operation,
            idempotency_key=request.idempotency_key,
            requires_confirmation=requires,
            created_at=now,
            updated_at=now,
        )
        self._intents.save(intent)
        return intent, operation

    def confirm(
        self, claims: WorkloadClaims | str, request: WriteRequest, token: str
    ) -> Intent:
        """Rebuild the operation exactly and mark the intent approved."""

        self._authorize(claims, request.source_id, request.capability)
        contract = self._contract(request.source_id)
        with self._lock:
            intent = self._intent_for(request)
            self._require_epoch(intent.operation, contract)
            operation = self._build(request, contract, intent.intent_id)
            self._require_same_operation(intent.operation, operation)
            if intent.state is not IntentState.AWAITING_CONFIRMATION:
                raise ValidationError(
                    f"intent {intent.intent_id} is {intent.state.value}, not awaiting confirmation"
                )

            approval = self._gate.verify(token, operation)
            self._remember_approval(intent.intent_id, approval, token)
            approved = self._transition(intent, IntentState.APPROVED)
            self._intents.save(approved)
            return approved

    def apply(
        self,
        claims: WorkloadClaims | str,
        request: WriteRequest,
        token: str | None = None,
    ) -> dict[str, Any]:
        """Apply a proposed write once, verifying confirmation and revision.

        An applied intent replays its stored effect without touching the
        adapter. An ``OUTCOME_UNKNOWN`` intent is refused until reconciled.
        The revision-check-through-apply window is serialized by the broker
        lock so two writers cannot both pass the same revision check.
        """

        self._authorize(claims, request.source_id, request.capability)
        with self._lock:
            return self._apply_locked(claims, request, token)

    def _apply_locked(
        self,
        claims: WorkloadClaims | str,
        request: WriteRequest,
        token: str | None,
    ) -> dict[str, Any]:
        contract = self._contract(request.source_id)
        intent = self._intent_for(request)
        self._require_epoch(intent.operation, contract)
        operation = self._build(request, contract, intent.intent_id)
        self._require_same_operation(intent.operation, operation)

        if intent.state is IntentState.APPLIED:
            return dict(intent.effect or {})
        if intent.state is IntentState.OUTCOME_UNKNOWN:
            raise OutcomeUnknown(
                f"intent {intent.intent_id} has an unknown outcome; reconcile it first"
            )
        if intent.state not in _APPLICABLE:
            raise ValidationError(
                f"intent {intent.intent_id} is {intent.state.value} and cannot be applied"
            )

        if intent.requires_confirmation and intent.state is not IntentState.APPROVED:
            if token is None:
                raise ConfirmationRequired(
                    f"intent {intent.intent_id} requires human confirmation"
                )
            approval = self._gate.verify(token, operation)
            self._remember_approval(intent.intent_id, approval, token)
            intent = self._transition(intent, IntentState.APPROVED)
            self._intents.save(intent)

        adapter = self._adapter(operation.source_id)
        for target in operation.targets:
            if adapter.revision(target) != operation.base_revision:
                failed = self._transition(
                    intent,
                    IntentState.FAILED,
                    error=f"stale base revision for {target!r}",
                )
                self._intents.save(failed)
                raise StaleError(
                    f"target {target!r} moved past base revision {operation.base_revision!r}"
                )

        intent = self._transition(intent, IntentState.APPLYING)
        self._intents.save(intent)

        try:
            result = adapter.apply(operation, intent.idempotency_key)
        except Exception as exc:
            self._mark_unknown(intent, f"apply raised {_describe_exception(exc)}")
            raise OutcomeUnknown(
                f"intent {intent.intent_id} may or may not have landed: "
                f"{_describe_exception(exc)}"
            ) from exc

        try:
            observed = [adapter.read(target) for target in operation.targets]
        except Exception as exc:
            self._mark_unknown(intent, f"read-back raised {_describe_exception(exc)}")
            raise OutcomeUnknown(
                f"intent {intent.intent_id} cannot be confirmed by read-back: "
                f"{_describe_exception(exc)}"
            ) from exc

        if not all(
            self._observed_matches(operation, contract, record) for record in observed
        ):
            self._mark_unknown(intent, "read-back did not show the expected fields")
            raise OutcomeUnknown(
                f"intent {intent.intent_id} read-back did not show the expected fields"
            )

        effect = dict(result or {})
        applied = self._transition(intent, IntentState.APPLIED, effect=effect)
        self._intents.save(applied)
        self._record(applied)
        return effect

    def reconcile(self, claims: WorkloadClaims | str, intent_id: str) -> Intent:
        """Observe an ambiguous intent through the adapter and settle it.

        The caller must be authorized for the intent's own source and
        capability before the broker reads anything: observing an outcome is
        still a read of that source. A settled intent is returned unchanged.
        """

        intent = self._intents.get(intent_id)
        if intent is None:
            raise ValidationError(f"unknown intent {intent_id!r}")
        self._authorize(
            claims, intent.operation.source_id, intent.operation.capability
        )
        with self._lock:
            intent = self._intents.get(intent_id)
            if intent is None:
                raise ValidationError(f"unknown intent {intent_id!r}")
            if intent.state not in _RECONCILABLE:
                return intent

            operation = intent.operation
            contract = self._contract(operation.source_id)
            try:
                observed = [
                    self._adapter(operation.source_id).read(target)
                    for target in operation.targets
                ]
                satisfied = all(
                    self._observed_matches(operation, contract, record)
                    for record in observed
                )
            except Exception:
                satisfied = False
                observed = []

            if satisfied:
                effect = observed[0] if len(observed) == 1 else {"targets": observed}
                settled = self._transition(
                    intent, IntentState.APPLIED, effect=dict(effect)
                )
                self._intents.save(settled)
                self._record(settled)
                return settled

            failed = self._transition(
                intent, IntentState.FAILED, error="reconcile did not observe the effect"
            )
            self._intents.save(failed)
            return failed

    # -- internals -----------------------------------------------------

    def _authorize(
        self, claims: WorkloadClaims | str, source_id: str, capability: str
    ) -> WorkloadClaims:
        resolved = self._issuer.verify(claims) if isinstance(claims, str) else claims
        self._issuer.authorize(resolved, source_id, capability)
        return resolved

    def _contract(self, source_id: str) -> SourceContract:
        contract = self._sources.get(source_id)
        if contract is None:
            raise ContractError(f"unknown source {source_id!r}")
        return contract

    def _adapter(self, source_id: str) -> SourceAdapter:
        adapter = self._adapters.get(source_id)
        if adapter is None:
            raise ContractError(f"no adapter for source {source_id!r}")
        return adapter

    def _intent_for(self, request: WriteRequest) -> Intent:
        intent = self._intents.find(request.idempotency_key)
        if intent is None:
            raise ValidationError(
                f"no intent for idempotency key {request.idempotency_key!r}; propose first"
            )
        return intent

    @staticmethod
    def _validate_request(request: WriteRequest, contract: SourceContract) -> None:
        if not request.idempotency_key.strip():
            raise ValidationError("idempotency key is empty")
        targets = tuple(request.targets)
        if not targets:
            raise ValidationError("a write needs at least one target")
        if len(targets) != len(set(targets)):
            raise ValidationError("a write cannot repeat a target")
        if request.epoch is not None and request.epoch != contract.epoch:
            raise EpochError(
                f"source {contract.source_id!r} is at epoch {contract.epoch}, "
                f"not {request.epoch}"
            )
        if request.base_revision and _CONTROL_CHARACTERS.search(request.base_revision):
            raise ValidationError("base_revision contains control characters")
        for change in request.field_diff:
            if _CONTROL_CHARACTERS.search(change.field):
                raise ValidationError(
                    f"field name {change.field!r} contains control characters"
                )

    @staticmethod
    def _build(
        request: WriteRequest, contract: SourceContract, intent_id: str
    ) -> NormalizedOperation:
        targets = tuple(request.targets)
        return NormalizedOperation(
            intent_id=intent_id,
            source_id=contract.source_id,
            capability=request.capability,
            targets=targets,
            base_revision=request.base_revision,
            field_diff=tuple(request.field_diff),
            affected_count=len(targets),
            epoch=contract.epoch,
            recurrence=request.recurrence,
            attendee_scope=tuple(request.attendee_scope),
            destination=request.destination,
        )

    @staticmethod
    def _needs_confirmation(
        claims: WorkloadClaims,
        contract: SourceContract,
        capability: Capability,
    ) -> bool:
        # A local session writing to a general source is a write-down across the
        # confidentiality boundary, so even a reversible create needs a human.
        if capability.requires_confirmation:
            return True
        return (
            claims.profile == LOCAL_PROFILE
            and contract.visibility is Visibility.GENERAL
        )

    @staticmethod
    def _require_epoch(operation: NormalizedOperation, contract: SourceContract) -> None:
        if operation.epoch != contract.epoch:
            raise EpochError(
                f"source {contract.source_id!r} moved from epoch {operation.epoch} "
                f"to {contract.epoch}; rebuild the intent"
            )

    @staticmethod
    def _require_same_operation(
        stored: NormalizedOperation, rebuilt: NormalizedOperation
    ) -> None:
        if stored.normalized() != rebuilt.normalized():
            raise IdempotencyConflict(
                f"intent {rebuilt.intent_id!r} was proposed for another operation"
            )

    @staticmethod
    def _observed_matches(
        operation: NormalizedOperation,
        contract: SourceContract,
        record: Mapping[str, Any],
    ) -> bool:
        """Whether the observed record proves the operation landed.

        An empty ``field_diff`` must not make a create or delete vacuously
        satisfy the check: a create needs the target to exist, and a delete
        needs the target gone or carrying its contract tombstone.
        """

        effect = contract.capability(operation.capability).effect
        if effect is EffectClass.CREATE and not record:
            return False
        if effect is EffectClass.DELETE:
            if not record:
                return True
            tombstone = contract.tombstone_field
            return bool(tombstone and record.get(tombstone))
        return all(
            record.get(change.field) == change.after for change in operation.field_diff
        )

    def _remember_approval(self, intent_id: str, approval: Any, token: str) -> None:
        # Durable intent rows have no approval columns yet (intents.py is owned
        # by another lane); keep the audit record in memory for the effect event.
        if len(self._approvals) >= _MAX_REMEMBERED_APPROVALS:
            self._approvals.pop(next(iter(self._approvals)))
        self._approvals[intent_id] = {
            "approved_by": approval.approved_by,
            "approved_at": approval.approved_at,
            "token_sha256": hashlib.sha256(token.encode("utf-8")).hexdigest(),
        }

    def _mark_unknown(self, intent: Intent, reason: str) -> None:
        self._intents.save(
            self._transition(intent, IntentState.OUTCOME_UNKNOWN, error=reason)
        )

    def _transition(
        self,
        intent: Intent,
        state: IntentState,
        *,
        effect: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> Intent:
        return replace(
            intent,
            state=state,
            updated_at=int(self._clock()),
            effect=effect if effect is not None else intent.effect,
            error=error,
        )

    def _record(self, intent: Intent) -> None:
        operation = intent.operation
        event = {
            "intent_id": intent.intent_id,
            "source_id": operation.source_id,
            "capability": operation.capability,
            "targets": list(operation.targets),
            "idempotency_key": intent.idempotency_key,
            "epoch": operation.epoch,
            "state": intent.state.value,
            "effect": dict(intent.effect or {}),
            "at": int(self._clock()),
        }
        approval = self._approvals.get(intent.intent_id)
        if approval is not None:
            event["approval"] = dict(approval)
        self._events.record(event)
