"""Integration: broker against the single SQLite intent journal."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from data_plane.broker import Broker, WriteRequest
from data_plane.broker_journal import BrokerJournal
from data_plane.confirmation import ConfirmationGate, FieldChange
from data_plane.errors import OutcomeUnknown
from data_plane.identity import EXTERNAL_PROFILE, IdentityIssuer
from data_plane.intents import IntentStore
from data_plane.model import (
    Capability,
    EffectClass,
    IntentState,
    SourceContract,
    Visibility,
)

KEY = b"journal-test-key"


def contract() -> SourceContract:
    return SourceContract(
        source_id="books",
        visibility=Visibility.GENERAL,
        version=1,
        recovery_group="books",
        stable_id_fields=("id",),
        capabilities=(
            Capability(name="read", effect=EffectClass.READ),
            Capability(name="search", effect=EffectClass.SEARCH),
            Capability(name="create", effect=EffectClass.CREATE),
            Capability(
                name="delete",
                effect=EffectClass.DELETE,
                reversible=False,
                requires_confirmation=True,
            ),
        ),
        revision_field="rev",
        tombstone_field="deleted_at",
    )


class Adapter:
    def __init__(self) -> None:
        self.records: dict[str, dict] = {}
        self.calls = 0
        self.fail = False

    def seed(self, target: str) -> None:
        self.records[target] = {"id": target, "rev": "r1"}

    def revision(self, target: str) -> str | None:
        return self.records.get(target, {}).get("rev")

    def read(self, target: str) -> dict:
        return dict(self.records.get(target, {}))

    def search(self, query: str) -> list[dict]:
        return [record for record in self.records.values() if query in record.get("id", "")]

    def apply(self, operation, idempotency_key: str) -> dict:
        self.calls += 1
        if self.fail:
            raise RuntimeError("adapter failed")
        for target in operation.targets:
            record = self.records.setdefault(target, {"id": target, "rev": "r1"})
            for change in operation.field_diff:
                record[change.field] = change.after
            record["rev"] = "r2"
        return {"targets": list(operation.targets)}


class Recorder:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def record(self, event) -> None:
        self.events.append(dict(event))


class BrokerJournalTests(unittest.TestCase):
    def build(self, path: str):
        self.store = IntentStore(path)
        self.adapter = Adapter()
        self.adapter.seed("book-1")
        self.gate = ConfirmationGate(ttl_seconds=60, clock=lambda: 1000.0)
        self.issuer = IdentityIssuer({"books": contract()}, KEY, clock=lambda: 1000.0)
        self.broker = Broker(
            sources={"books": contract()},
            adapters={"books": self.adapter},
            issuer=self.issuer,
            gate=self.gate,
            intents=BrokerJournal(self.store),
            events=Recorder(),
            clock=lambda: 1000.0,
        )
        return self.issuer.issue(
            EXTERNAL_PROFILE, "agent", (("books", "create"), ("books", "delete"))
        )

    def tearDown(self) -> None:
        if getattr(self, "store", None) is not None:
            self.store.close()

    def test_create_persists_operation_and_applies(self):
        token = self.build(":memory:")
        request = WriteRequest(
            source_id="books",
            capability="create",
            targets=("book-2",),
            idempotency_key="key-create",
            field_diff=(FieldChange(field="title", before=None, after="New"),),
            base_revision=None,
        )
        intent, _ = self.broker.propose(token, request)
        self.assertIs(intent.state, IntentState.APPROVED)
        effect = self.broker.apply(token, request)
        self.assertEqual(effect["targets"], ["book-2"])
        self.assertEqual(self.adapter.calls, 1)

        row = self.store.find("key-create")
        self.assertIsNotNone(row)
        self.assertEqual(row.operation["targets"], ["book-2"])
        self.assertIs(row.state, IntentState.APPLIED)

        # Replaying the applied intent returns the stored effect untouched.
        self.assertEqual(self.broker.apply(token, request)["targets"], ["book-2"])
        self.assertEqual(self.adapter.calls, 1)

    def test_delete_requires_confirmation_and_survives_reopen(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "intents.sqlite")
            token = self.build(path)
            request = WriteRequest(
                source_id="books",
                capability="delete",
                targets=("book-1",),
                idempotency_key="key-delete",
                base_revision="r1",
            )
            intent, operation = self.broker.propose(token, request)
            self.assertIs(intent.state, IntentState.AWAITING_CONFIRMATION)
            _, approval_token = self.gate.issue(operation, "smores")
            approved = self.broker.confirm(token, request, approval_token)
            self.assertIs(approved.state, IntentState.APPROVED)
            self.broker.apply(token, request)
            self.store.close()

            reopened = IntentStore(path)
            try:
                row = reopened.find("key-delete")
                self.assertIsNotNone(row)
                self.assertIs(row.state, IntentState.APPLIED)
                self.assertEqual(row.operation["capability"], "delete")
            finally:
                reopened.close()

    def test_outcome_unknown_is_refused_then_reconciled(self):
        token = self.build(":memory:")
        self.adapter.fail = True
        request = WriteRequest(
            source_id="books",
            capability="create",
            targets=("book-3",),
            idempotency_key="key-unknown",
            field_diff=(FieldChange(field="title", before=None, after="Maybe"),),
            base_revision=None,
        )
        self.broker.propose(token, request)
        with self.assertRaises(OutcomeUnknown):
            self.broker.apply(token, request)
        calls = self.adapter.calls

        with self.assertRaises(OutcomeUnknown):
            self.broker.apply(token, request)
        self.assertEqual(self.adapter.calls, calls)

        self.adapter.fail = False
        intent = self.broker.reconcile(self.store.find("key-unknown").intent_id)
        self.assertIs(intent.state, IntentState.FAILED)


if __name__ == "__main__":
    unittest.main()
