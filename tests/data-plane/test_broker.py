"""Write broker tests: scoped access, confirmation, revisions, and idempotency.

Every adapter here is in memory. Nothing touches the network, a provider, or
live data.
"""

from __future__ import annotations

import unittest

from data_plane.broker import (
    Broker,
    Intent,
    IntentStore,
    SourceAdapter,
    WriteRequest,
)
from data_plane.confirmation import ConfirmationGate, FieldChange, NormalizedOperation
from data_plane.errors import (
    AuthorizationError,
    ConfirmationError,
    ConfirmationRequired,
    ContractError,
    EpochError,
    IdempotencyConflict,
    OutcomeUnknown,
    StaleError,
    ValidationError,
)
from data_plane.identity import (
    EXTERNAL_PROFILE,
    LOCAL_PROFILE,
    IdentityIssuer,
    Scope,
    WorkloadClaims,
)
from data_plane.model import (
    Authority,
    Capability,
    EffectClass,
    FieldAuthority,
    IntentState,
    SourceContract,
    Visibility,
)

KEY = b"test-key-not-a-secret"
NOW = 1_000_000


def read() -> Capability:
    return Capability(name="read", effect=EffectClass.READ)


def search() -> Capability:
    return Capability(name="search", effect=EffectClass.SEARCH)


def create() -> Capability:
    return Capability(name="create", effect=EffectClass.CREATE, reversible=True)


def update() -> Capability:
    return Capability(name="update", effect=EffectClass.UPDATE, reversible=True)


def delete() -> Capability:
    return Capability(
        name="delete",
        effect=EffectClass.DELETE,
        reversible=False,
        requires_confirmation=True,
    )


def contract(
    source_id: str, visibility: Visibility, epoch: int = 1
) -> SourceContract:
    return SourceContract(
        source_id=source_id,
        visibility=visibility,
        version=1,
        recovery_group=source_id,
        stable_id_fields=("id",),
        capabilities=(read(), search(), create(), update(), delete()),
        authority=(FieldAuthority(field="title", authority=Authority.PORTABLE_FILE),),
        revision_field="rev",
        tombstone_field="deleted_at",
        epoch=epoch,
    )


class FakeAdapter:
    """In-memory adapter with the four operations the broker calls."""

    def __init__(self, records: dict[str, dict] | None = None) -> None:
        self.records = {key: dict(value) for key, value in (records or {}).items()}
        self.applies: list[tuple[str, str]] = []
        self.fail_apply = False
        self.ignore_writes = False

    def revision(self, target: str) -> str | None:
        return self.records.get(target, {}).get("rev")

    def read(self, target: str) -> dict:
        return dict(self.records.get(target, {}))

    def search(self, query: str) -> list[dict]:
        needle = query.lower()
        return [
            dict(record)
            for record in self.records.values()
            if needle in str(record.get("title", "")).lower()
        ]

    def apply(self, operation: NormalizedOperation, idempotency_key: str) -> dict:
        if self.fail_apply:
            raise ConnectionError("transport lost while the write was in flight")
        self.applies.append((operation.intent_id, idempotency_key))
        if self.ignore_writes:
            return {}
        for target in operation.targets:
            record = self.records.setdefault(target, {"id": target})
            for change in operation.field_diff:
                record[change.field] = change.after
            if operation.capability == "delete":
                self.records.pop(target, None)
            else:
                record["rev"] = f"rev-{len(self.applies) + 1}"
        return {"applied": len(operation.targets)}


class FakeIntentStore:
    def __init__(self) -> None:
        self.by_id: dict[str, Intent] = {}
        self.by_key: dict[str, Intent] = {}

    def get(self, intent_id: str) -> Intent | None:
        return self.by_id.get(intent_id)

    def find(self, idempotency_key: str) -> Intent | None:
        return self.by_key.get(idempotency_key)

    def save(self, intent: Intent) -> None:
        self.by_id[intent.intent_id] = intent
        self.by_key[intent.idempotency_key] = intent


class FakeRecorder:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def record(self, event: dict) -> None:
        self.events.append(dict(event))


def request(**overrides) -> WriteRequest:
    values = dict(
        source_id="notes",
        capability="update",
        targets=("note-1",),
        idempotency_key="key-1",
        field_diff=(FieldChange(field="title", before="Old", after="New"),),
        base_revision="rev-1",
    )
    values.update(overrides)
    return WriteRequest(**values)


class BrokerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sources = {
            "notes": contract("notes", Visibility.PERSONAL),
            "books": contract("books", Visibility.GENERAL),
        }
        self.adapters = {
            "notes": FakeAdapter(
                {"note-1": {"id": "note-1", "title": "Old", "rev": "rev-1"}}
            ),
            "books": FakeAdapter(
                {"book-1": {"id": "book-1", "title": "Old", "rev": "rev-1"}}
            ),
        }
        self.issuer = IdentityIssuer(self.sources, KEY, clock=lambda: NOW)
        self.gate = ConfirmationGate(ttl_seconds=60, clock=lambda: NOW)
        self.store = FakeIntentStore()
        self.recorder = FakeRecorder()
        self.broker = Broker(
            self.sources,
            self.adapters,
            self.issuer,
            self.gate,
            self.store,
            self.recorder,
            clock=lambda: NOW,
        )

    def claims(self, profile: str, *scopes: tuple[str, str]) -> WorkloadClaims:
        token = self.issuer.issue(profile, "agent", scopes)
        return self.issuer.verify(token)

    def local(self, *scopes: tuple[str, str]) -> WorkloadClaims:
        return self.claims(LOCAL_PROFILE, *scopes)

    def external(self, *scopes: tuple[str, str]) -> WorkloadClaims:
        return self.claims(EXTERNAL_PROFILE, *scopes)

    def test_scoped_read_and_search(self) -> None:
        claims = self.local(("notes", "read"), ("books", "search"))
        self.assertEqual(self.broker.read(claims, "notes", "note-1")["title"], "Old")
        found = self.broker.search(claims, "books", "old")
        self.assertEqual([record["id"] for record in found], ["book-1"])

    def test_read_outside_scope_is_denied(self) -> None:
        claims = self.local(("notes", "read"))
        with self.assertRaises(AuthorizationError):
            self.broker.read(claims, "books", "book-1")

    def test_external_profile_cannot_reach_personal(self) -> None:
        forged = WorkloadClaims(
            profile=EXTERNAL_PROFILE,
            workload="agent",
            scopes=(Scope("notes", "read"),),
            issued_at=NOW,
            expires_at=NOW + 60,
            nonce="deadbeef",
        )
        with self.assertRaises(AuthorizationError):
            self.broker.read(forged, "notes", "note-1")
        with self.assertRaises(AuthorizationError):
            self.broker.propose(
                forged,
                request(capability="create", targets=("note-2",), base_revision=None),
            )

    def test_create_on_personal_source_is_approved_and_applied(self) -> None:
        claims = self.local(("notes", "create"))
        write = request(
            capability="create",
            targets=("note-2",),
            base_revision=None,
            field_diff=(FieldChange(field="title", before=None, after="Draft"),),
            idempotency_key="create-1",
        )
        intent, operation = self.broker.propose(claims, write)
        self.assertIs(intent.state, IntentState.APPROVED)
        self.assertEqual(operation.epoch, 1)
        self.assertEqual(operation.affected_count, 1)

        effect = self.broker.apply(claims, write)

        self.assertEqual(effect["applied"], 1)
        self.assertEqual(self.adapters["notes"].records["note-2"]["title"], "Draft")
        self.assertIs(self.store.find("create-1").state, IntentState.APPLIED)
        self.assertEqual(len(self.recorder.events), 1)
        self.assertEqual(self.recorder.events[0]["intent_id"], intent.intent_id)

    def test_update_checks_the_base_revision(self) -> None:
        claims = self.local(("notes", "update"))
        write = request(idempotency_key="update-1")

        intent, operation = self.broker.propose(claims, write)
        self.assertIs(intent.state, IntentState.APPROVED)

        self.adapters["notes"].records["note-1"]["rev"] = "rev-2"
        with self.assertRaises(StaleError):
            self.broker.apply(claims, write)

        self.assertIs(self.store.find("update-1").state, IntentState.FAILED)
        self.assertEqual(self.adapters["notes"].records["note-1"]["title"], "Old")
        self.assertEqual(operation.base_revision, "rev-1")

    def test_epoch_change_rejects_the_write(self) -> None:
        claims = self.local(("books", "update"))
        write = request(source_id="books", targets=("book-1",), idempotency_key="epoch-1")
        self.broker.propose(claims, write)

        moved = Broker(
            {"books": contract("books", Visibility.GENERAL, epoch=2)},
            self.adapters,
            self.issuer,
            self.gate,
            self.store,
            self.recorder,
            clock=lambda: NOW,
        )
        with self.assertRaises(EpochError):
            moved.apply(claims, write)

    def test_delete_requires_confirmation(self) -> None:
        claims = self.local(("notes", "delete"))
        write = request(capability="delete", field_diff=(), idempotency_key="del-1")

        intent, operation = self.broker.propose(claims, write)
        self.assertIs(intent.state, IntentState.AWAITING_CONFIRMATION)
        with self.assertRaises(ConfirmationRequired):
            self.broker.apply(claims, write)

        _, token = self.gate.issue(operation, "smores")
        confirmed = self.broker.confirm(claims, write, token)
        self.assertIs(confirmed.state, IntentState.APPROVED)

        self.broker.apply(claims, write)

        self.assertNotIn("note-1", self.adapters["notes"].records)
        self.assertIs(self.store.find("del-1").state, IntentState.APPLIED)

    def test_wrong_token_does_not_approve(self) -> None:
        claims = self.local(("notes", "delete"))
        write = request(capability="delete", field_diff=(), idempotency_key="del-2")
        _, operation = self.broker.propose(claims, write)

        other = NormalizedOperation(
            intent_id=operation.intent_id,
            source_id=operation.source_id,
            capability=operation.capability,
            targets=("note-9",),
            base_revision=operation.base_revision,
            field_diff=(),
            affected_count=1,
            epoch=operation.epoch,
        )
        _, token = self.gate.issue(other, "smores")
        with self.assertRaises(ConfirmationError):
            self.broker.confirm(claims, write, token)
        with self.assertRaises(ConfirmationError):
            self.broker.confirm(claims, write, "not-a-token")

    def test_duplicate_apply_is_idempotent(self) -> None:
        claims = self.local(("notes", "update"))
        write = request(idempotency_key="dup-1")

        first, _ = self.broker.propose(claims, write)
        second, _ = self.broker.propose(claims, write)
        self.assertEqual(first.intent_id, second.intent_id)

        first_effect = self.broker.apply(claims, write)
        second_effect = self.broker.apply(claims, write)

        self.assertEqual(first_effect, second_effect)
        self.assertEqual(len(self.adapters["notes"].applies), 1)
        self.assertEqual(len(self.recorder.events), 1)

    def test_same_key_with_a_different_operation_conflicts(self) -> None:
        claims = self.local(("notes", "update"))
        self.broker.propose(claims, request(idempotency_key="conflict-1"))
        with self.assertRaises(IdempotencyConflict):
            self.broker.propose(
                claims,
                request(
                    idempotency_key="conflict-1",
                    field_diff=(FieldChange(field="title", before="Old", after="Other"),),
                ),
            )

    def test_outcome_unknown_is_never_replayed_and_reconciles(self) -> None:
        claims = self.local(("notes", "update"))
        write = request(idempotency_key="unknown-1")
        intent, _ = self.broker.propose(claims, write)

        self.adapters["notes"].fail_apply = True
        with self.assertRaises(OutcomeUnknown):
            self.broker.apply(claims, write)
        self.assertIs(self.store.find("unknown-1").state, IntentState.OUTCOME_UNKNOWN)

        with self.assertRaises(OutcomeUnknown):
            self.broker.apply(claims, write)
        self.assertEqual(len(self.adapters["notes"].applies), 0)

        self.adapters["notes"].fail_apply = False
        self.adapters["notes"].records["note-1"]["title"] = "New"
        reconciled = self.broker.reconcile(intent.intent_id)

        self.assertIs(reconciled.state, IntentState.APPLIED)
        self.assertEqual(reconciled.effect["title"], "New")
        self.assertEqual(len(self.recorder.events), 1)
        self.assertEqual(self.broker.apply(claims, write), reconciled.effect)
        self.assertEqual(len(self.adapters["notes"].applies), 0)

    def test_unconfirmed_read_back_marks_the_outcome_unknown(self) -> None:
        claims = self.local(("notes", "update"))
        write = request(idempotency_key="mismatch-1")
        self.adapters["notes"].ignore_writes = True
        self.broker.propose(claims, write)

        with self.assertRaises(OutcomeUnknown):
            self.broker.apply(claims, write)

        self.assertIs(self.store.find("mismatch-1").state, IntentState.OUTCOME_UNKNOWN)

    def test_local_write_to_general_requires_confirmation(self) -> None:
        claims = self.local(("books", "create"))
        write = request(
            source_id="books",
            capability="create",
            targets=("book-2",),
            base_revision=None,
            field_diff=(FieldChange(field="title", before=None, after="New"),),
            idempotency_key="down-1",
        )
        intent, _ = self.broker.propose(claims, write)
        self.assertIs(intent.state, IntentState.AWAITING_CONFIRMATION)
        with self.assertRaises(ConfirmationRequired):
            self.broker.apply(claims, write)

    def test_external_write_to_general_needs_no_confirmation(self) -> None:
        claims = self.external(("books", "create"))
        write = request(
            source_id="books",
            capability="create",
            targets=("book-3",),
            base_revision=None,
            field_diff=(FieldChange(field="title", before=None, after="New"),),
            idempotency_key="ext-1",
        )
        intent, _ = self.broker.propose(claims, write)
        self.assertIs(intent.state, IntentState.APPROVED)
        self.broker.apply(claims, write)
        self.assertEqual(self.adapters["books"].records["book-3"]["title"], "New")

    def test_empty_targets_and_read_capability_are_rejected(self) -> None:
        claims = self.local(("notes", "update"), ("notes", "read"))
        with self.assertRaises(ValidationError):
            self.broker.propose(
                claims, request(targets=(), idempotency_key="empty-1")
            )
        with self.assertRaises(ContractError):
            self.broker.propose(
                claims, request(capability="read", idempotency_key="cap-1")
            )
        with self.assertRaises(AuthorizationError):
            self.broker.propose(
                claims, request(capability="reclassify", idempotency_key="cap-2")
            )

    def test_apply_before_propose_is_rejected(self) -> None:
        claims = self.local(("notes", "update"))
        with self.assertRaises(ValidationError):
            self.broker.apply(claims, request(idempotency_key="never-proposed"))

    def test_adapters_satisfy_the_protocol(self) -> None:
        self.assertIsInstance(self.adapters["notes"], SourceAdapter)
        self.assertIsInstance(self.store, IntentStore)


if __name__ == "__main__":
    unittest.main()
