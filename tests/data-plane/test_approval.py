"""Signed approval tokens, nonce replay, and the broker's cross-pod path.

Every key and adapter here is in memory or a temporary SQLite file. Nothing
touches the network.
"""

from __future__ import annotations

import tempfile
import threading
import unittest

from data_plane.approval import (
    ApprovalClaims,
    SignedApprovalVerifier,
    UsedApprovalStore,
    claims_for,
    sign_approval,
    verify_approval,
)
from data_plane.broker import Broker, Intent, WriteRequest
from data_plane.confirmation import FieldChange, NormalizedOperation
from data_plane.errors import (
    AuthorizationError,
    ConfirmationError,
    ConfirmationRequired,
)
from data_plane.identity import LOCAL_PROFILE, IdentityIssuer
from data_plane.model import (
    Authority,
    Capability,
    EffectClass,
    FieldAuthority,
    IntentState,
    SourceContract,
    Visibility,
)

NOW = 1_000_000
KEY = b"approval-test-key"


def operation(**overrides) -> NormalizedOperation:
    values = dict(
        intent_id="intent-1",
        source_id="notes",
        capability="delete",
        targets=("note-1",),
        base_revision="rev-1",
        field_diff=(FieldChange(field="title", before="Old", after="New"),),
        affected_count=1,
        epoch=3,
        recurrence=None,
        attendee_scope=(),
        destination=None,
    )
    values.update(overrides)
    return NormalizedOperation(**values)


class ApprovalTokenTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = NOW
        self.claims = claims_for(operation(), "operator", issued_at=self.now, ttl=60)

    def test_sign_and_verify_roundtrip(self) -> None:
        token = sign_approval(self.claims, KEY)
        verified = verify_approval(token, KEY, now=self.now)
        self.assertEqual(verified, self.claims)

    def test_signature_tamper_is_rejected(self) -> None:
        token = sign_approval(self.claims, KEY)
        payload, signature = token.split(".", 1)
        flipped = ("A" if signature[0] != "A" else "B") + signature[1:]
        with self.assertRaises(ConfirmationError):
            verify_approval(f"{payload}.{flipped}", KEY, now=self.now)

    def test_payload_tamper_is_rejected(self) -> None:
        token = sign_approval(self.claims, KEY)
        _, signature = token.split(".", 1)
        other = sign_approval(
            claims_for(operation(targets=("note-9",)), "operator", issued_at=self.now, ttl=60),
            KEY,
        )
        other_payload = other.split(".", 1)[0]
        with self.assertRaises(ConfirmationError):
            verify_approval(f"{other_payload}.{signature}", KEY, now=self.now)

    def test_wrong_key_is_rejected(self) -> None:
        token = sign_approval(self.claims, KEY)
        with self.assertRaises(ConfirmationError):
            verify_approval(token, b"another-key", now=self.now)

    def test_expired_token_is_rejected(self) -> None:
        token = sign_approval(self.claims, KEY)
        with self.assertRaises(ConfirmationError):
            verify_approval(token, KEY, now=self.now + 60)

    def test_future_issue_is_rejected(self) -> None:
        future = claims_for(operation(), "operator", issued_at=self.now + 1, ttl=60)
        token = sign_approval(future, KEY)
        with self.assertRaises(ConfirmationError):
            verify_approval(token, KEY, now=self.now)

    def test_ttl_beyond_maximum_is_rejected(self) -> None:
        long_lived = claims_for(operation(), "operator", issued_at=self.now, ttl=901)
        token = sign_approval(long_lived, KEY)
        with self.assertRaises(ConfirmationError):
            verify_approval(token, KEY, now=self.now, max_ttl=900)

    def test_empty_key_is_rejected(self) -> None:
        with self.assertRaises(ConfirmationError):
            sign_approval(self.claims, b"")


class UsedApprovalStoreTests(unittest.TestCase):
    def test_nonce_is_single_use(self) -> None:
        store = UsedApprovalStore()
        self.addCleanup(store.close)
        store.consume("a" * 32, intent_id="intent-1", operation_hash="f" * 64)
        self.assertTrue(store.used("a" * 32))
        with self.assertRaises(ConfirmationError):
            store.consume("a" * 32, intent_id="intent-1", operation_hash="f" * 64)

    def test_reused_nonce_with_another_binding_is_rejected(self) -> None:
        store = UsedApprovalStore()
        self.addCleanup(store.close)
        store.consume("b" * 32, intent_id="intent-1", operation_hash="f" * 64)
        with self.assertRaises(ConfirmationError):
            store.consume("b" * 32, intent_id="intent-2", operation_hash="e" * 64)

    def test_concurrent_consumption_has_one_winner(self) -> None:
        store = UsedApprovalStore()
        self.addCleanup(store.close)
        winners: list[int] = []
        losers: list[int] = []

        def attempt() -> None:
            try:
                store.consume("c" * 32)
                winners.append(1)
            except ConfirmationError:
                losers.append(1)

        threads = [threading.Thread(target=attempt) for _ in range(12)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(len(winners), 1)
        self.assertEqual(len(losers), 11)

    def test_nonce_survives_reopen(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = f"{tmp}/approvals.sqlite"
            first = UsedApprovalStore(path)
            first.consume("d" * 32, intent_id="intent-1", operation_hash="f" * 64)
            first.close()

            second = UsedApprovalStore(path)
            try:
                with self.assertRaises(ConfirmationError):
                    second.consume("d" * 32)
            finally:
                second.close()


class VerifierTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = NOW
        self.store = UsedApprovalStore()
        self.addCleanup(self.store.close)
        self.verifier = SignedApprovalVerifier(
            KEY, self.store, clock=lambda: self.now, max_ttl=900
        )
        self.claims, self.token = self.verifier.issue(operation(), "operator")
        self.operation = operation()

    def test_verify_matches_and_consumes(self) -> None:
        verified = self.verifier.verify(self.token, self.operation)
        self.assertEqual(verified.nonce, self.claims.nonce)
        self.assertTrue(self.store.used(self.claims.nonce))

    def test_replay_is_rejected(self) -> None:
        self.verifier.verify(self.token, self.operation)
        with self.assertRaises(ConfirmationError):
            self.verifier.verify(self.token, self.operation)

    def test_wrong_intent_does_not_consume_the_nonce(self) -> None:
        with self.assertRaises(ConfirmationError):
            self.verifier.verify(
                self.token, operation(intent_id="intent-2")
            )
        self.assertFalse(self.store.used(self.claims.nonce))

    def test_wrong_operation_hash_is_rejected(self) -> None:
        with self.assertRaises(ConfirmationError):
            self.verifier.verify(self.token, operation(targets=("note-9",)))
        self.assertFalse(self.store.used(self.claims.nonce))

    def test_wrong_epoch_is_rejected(self) -> None:
        with self.assertRaises(ConfirmationError):
            self.verifier.verify(self.token, operation(epoch=4))
        self.assertFalse(self.store.used(self.claims.nonce))

    def test_claimed_operation_hash_is_bound_to_the_operation(self) -> None:
        forged = ApprovalClaims(
            intent_id=self.operation.intent_id,
            operation_hash="f" * 64,
            epoch=self.operation.epoch,
            approved_by="operator",
            issued_at=self.now,
            expires_at=self.now + 60,
            nonce="f" * 32,
        )
        with self.assertRaises(ConfirmationError):
            self.verifier.verify(sign_approval(forged, KEY), self.operation)


def contract(source_id: str, visibility: Visibility) -> SourceContract:
    return SourceContract(
        source_id=source_id,
        visibility=visibility,
        version=1,
        recovery_group=source_id,
        stable_id_fields=("id",),
        capabilities=(
            Capability(name="read", effect=EffectClass.READ),
            Capability(name="create", effect=EffectClass.CREATE, reversible=True),
            Capability(
                name="delete",
                effect=EffectClass.DELETE,
                reversible=False,
                requires_confirmation=True,
            ),
        ),
        authority=(FieldAuthority(field="title", authority=Authority.PORTABLE_FILE),),
        revision_field="rev",
        tombstone_field="deleted_at",
        epoch=1,
    )


class FakeAdapter:
    def __init__(self, records: dict[str, dict] | None = None) -> None:
        self.records = {key: dict(value) for key, value in (records or {}).items()}
        self.applies: list[tuple[str, str]] = []

    def revision(self, target: str) -> str | None:
        return self.records.get(target, {}).get("rev")

    def read(self, target: str) -> dict:
        return dict(self.records.get(target, {}))

    def search(self, query: str) -> list[dict]:
        return [dict(record) for record in self.records.values()]

    def apply(self, operation: NormalizedOperation, idempotency_key: str) -> dict:
        self.applies.append((operation.intent_id, idempotency_key))
        for target in operation.targets:
            if operation.capability == "delete":
                self.records.pop(target, None)
                continue
            record = self.records.setdefault(target, {"id": target})
            for change in operation.field_diff:
                record[change.field] = change.after
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


class BrokerCrossPodApprovalTests(unittest.TestCase):
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
        self.intents = FakeIntentStore()
        self.recorder = FakeRecorder()
        self.store = UsedApprovalStore()
        self.addCleanup(self.store.close)
        self.verifier = SignedApprovalVerifier(
            KEY, self.store, clock=lambda: NOW, max_ttl=900
        )
        # No gate at all: the signed verifier is the only approval path.
        self.broker = Broker(
            self.sources,
            self.adapters,
            self.issuer,
            None,
            self.intents,
            self.recorder,
            clock=lambda: NOW,
            approval_verifier=self.verifier,
        )

    def claims(self, *scopes: tuple[str, str]):
        return self.issuer.verify(
            self.issuer.issue(LOCAL_PROFILE, "agent", scopes)
        )

    def delete_request(self, key: str = "del-1") -> WriteRequest:
        return WriteRequest(
            source_id="notes",
            capability="delete",
            targets=("note-1",),
            idempotency_key=key,
            base_revision="rev-1",
        )

    def test_signed_approval_applies_without_an_in_process_gate(self) -> None:
        claims = self.claims(("notes", "delete"))
        write = self.delete_request()
        _, operation = self.broker.propose(claims, write)
        approval, token = self.verifier.issue(operation, "operator")

        effect = self.broker.apply(claims, write, approval_token=token)

        self.assertEqual(effect, {"applied": 1})
        self.assertNotIn("note-1", self.adapters["notes"].records)
        self.assertTrue(self.store.used(approval.nonce))
        self.assertEqual(self.recorder.events[0]["approval"]["approved_by"], "operator")

    def test_delete_without_an_approval_is_refused(self) -> None:
        claims = self.claims(("notes", "delete"))
        write = self.delete_request("del-2")
        self.broker.propose(claims, write)
        with self.assertRaises(ConfirmationRequired):
            self.broker.apply(claims, write)
        self.assertIn("note-1", self.adapters["notes"].records)

    def test_token_for_another_operation_is_rejected(self) -> None:
        claims = self.claims(("notes", "delete"))
        write = self.delete_request("del-3")
        _, operation = self.broker.propose(claims, write)
        other = operation.__class__(
            intent_id=operation.intent_id,
            source_id=operation.source_id,
            capability=operation.capability,
            targets=("note-9",),
            base_revision=operation.base_revision,
            field_diff=(),
            affected_count=1,
            epoch=operation.epoch,
        )
        _, token = self.verifier.issue(other, "operator")
        with self.assertRaises(ConfirmationError):
            self.broker.apply(claims, write, approval_token=token)

    def test_a_second_pod_can_spend_an_operator_approval(self) -> None:
        claims = self.claims(("notes", "delete"))
        write = self.delete_request("del-4")
        _, operation = self.broker.propose(claims, write)
        _, token = self.verifier.issue(operation, "operator")

        other_pod = Broker(
            self.sources,
            self.adapters,
            self.issuer,
            None,
            self.intents,
            self.recorder,
            clock=lambda: NOW,
            approval_verifier=SignedApprovalVerifier(
                KEY, self.store, clock=lambda: NOW, max_ttl=900
            ),
        )
        transferred = other_pod.apply(claims, write, approval_token=token)
        self.assertEqual(transferred, {"applied": 1})

        with self.assertRaises(ConfirmationError):
            self.verifier.verify(token, operation)

    def test_confirm_spends_the_nonce_and_leaves_apply_tokenless(self) -> None:
        claims = self.claims(("notes", "delete"))
        write = self.delete_request("del-5")
        _, operation = self.broker.propose(claims, write)
        approval, token = self.verifier.issue(operation, "operator")

        approved = self.broker.confirm(claims, write, token)
        self.assertIs(approved.state, IntentState.APPROVED)
        self.assertTrue(self.store.used(approval.nonce))
        self.broker.apply(claims, write)
        self.assertIs(self.intents.find("del-5").state, IntentState.APPLIED)

    def test_local_to_general_create_still_needs_an_approval(self) -> None:
        claims = self.claims(("books", "create"))
        write = WriteRequest(
            source_id="books",
            capability="create",
            targets=("book-2",),
            idempotency_key="create-1",
            field_diff=(FieldChange(field="title", before=None, after="New"),),
        )
        intent, operation = self.broker.propose(claims, write)
        self.assertIs(intent.state, IntentState.AWAITING_CONFIRMATION)

        with self.assertRaises(ConfirmationRequired):
            self.broker.apply(claims, write)
        self.assertNotIn("book-2", self.adapters["books"].records)

        _, token = self.verifier.issue(operation, "operator")
        self.broker.apply(claims, write, approval_token=token)
        self.assertEqual(self.adapters["books"].records["book-2"]["title"], "New")

    def test_the_applying_workload_must_still_be_authorized(self) -> None:
        claims = self.claims(("notes", "delete"))
        write = self.delete_request("del-6")
        _, operation = self.broker.propose(claims, write)
        _, token = self.verifier.issue(operation, "operator")

        unauthorized = self.claims(("books", "read"))
        with self.assertRaises(AuthorizationError):
            self.broker.apply(unauthorized, write, approval_token=token)


if __name__ == "__main__":
    unittest.main()
