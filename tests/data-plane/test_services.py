"""Wire tests for the thin data-plane HTTP services.

Each service runs on port 0 in a thread over a temporary database or directory.
Every request is localhost; nothing reaches the network, a provider, a cluster,
or a restic repository. The fixtures are plain dicts and in-memory adapters.
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for candidate in (str(ROOT), str(ROOT / "libs")):
    if candidate not in sys.path:
        sys.path.insert(0, candidate)

from apps.data_plane import (  # noqa: E402 - path setup above
    backup_service,
    broker_service,
    common,
    event_service,
    relay_service,
)
from data_plane.backup.controller import FanoutController  # noqa: E402
from data_plane.backup.destinations import InMemoryDestination  # noqa: E402
from data_plane.broker import Broker, Intent  # noqa: E402
from data_plane.collectors.relay import RecordSchema, Relay  # noqa: E402
from data_plane.confirmation import ConfirmationGate  # noqa: E402
from data_plane.event_log import EventLog  # noqa: E402
from data_plane.identity import (  # noqa: E402
    LOCAL_PROFILE,
    IdentityIssuer,
)
from data_plane.model import (  # noqa: E402
    Authority,
    Capability,
    EffectClass,
    FieldAuthority,
    SourceContract,
    Visibility,
)

NOW = 1_000_000


class Harness:
    """One service on an ephemeral localhost port."""

    def __init__(self, service: object) -> None:
        self.server = common.build_server(
            "test", common.handler_for(service), port=0
        )
        self.thread = threading.Thread(
            target=lambda: self.server.serve_forever(poll_interval=0.02),
            daemon=True,
        )

    def __enter__(self) -> "Harness":
        self.thread.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}{path}"

    def request(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, object]:
        request = urllib.request.Request(
            self.url(path), data=body, method=method, headers=dict(headers or {})
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                raw = response.read()
                return response.status, json.loads(raw) if raw else None
        except urllib.error.HTTPError as exc:
            try:
                raw = exc.read()
            finally:
                exc.close()
            return exc.code, json.loads(raw) if raw else None


def event_headers(secret: bytes, method: str, path: str, body: bytes = b"") -> dict[str, str]:
    return {"X-Event-Token": common.request_token(secret, method, path, body)}


class EventServiceTests(unittest.TestCase):
    SECRET = b"event-test-secret"

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.log = EventLog(Path(self.tmp.name) / "events.db")
        self.addCleanup(self.log.close)
        self.harness = Harness(
            event_service.EventService(self.log, self.SECRET)
        )
        self.harness.__enter__()
        self.addCleanup(self.harness.__exit__, None, None, None)

    def post_event(self, payload: dict, path: str = "/events") -> tuple[int, object]:
        body = json.dumps(payload).encode()
        return self.harness.request(
            "POST", path, body=body, headers=event_headers(self.SECRET, "POST", path, body)
        )

    def get(self, path: str) -> tuple[int, object]:
        return self.harness.request(
            "GET", path, headers=event_headers(self.SECRET, "GET", path)
        )

    def test_append_read_and_ack(self) -> None:
        status, event = self.post_event(
            {"kind": "book.updated", "entity": "book-1", "payload": {"title": "Dune"}}
        )
        self.assertEqual(status, 201)
        self.assertEqual(event["position"], 1)
        self.assertEqual(event["kind"], "book.updated")
        self.assertEqual(event["payload"], {"title": "Dune"})

        status, listing = self.get("/events?after=0&limit=10")
        self.assertEqual(status, 200)
        self.assertEqual(listing["epoch"], 1)
        self.assertEqual([item["position"] for item in listing["events"]], [1])

        ack_body = json.dumps({"consumer": "worker", "position": 1}).encode()
        status, acked = self.harness.request(
            "POST",
            "/events/ack",
            body=ack_body,
            headers=event_headers(self.SECRET, "POST", "/events/ack", ack_body),
        )
        self.assertEqual(status, 200)
        self.assertEqual(acked, {"consumer": "worker", "position": 1, "epoch": 1})
        self.assertEqual(self.log.cursor("worker"), (1, 1))

    def test_tail_long_polls_until_an_event_arrives(self) -> None:
        def append_later() -> None:
            time.sleep(0.2)
            self.log.append("book.created", "book-1", {"title": "Dune"})

        threading.Thread(target=append_later, daemon=True).start()
        path = "/events/tail?consumer=worker&timeout=2"
        status, payload = self.get(path)
        self.assertEqual(status, 200)
        self.assertEqual(payload["event"]["position"], 1)
        self.assertEqual(payload["event"]["kind"], "book.created")
        # The tail must not acknowledge; the consumer owns the cursor.
        self.assertEqual(self.log.cursor("worker"), (0, 1))

    def test_tail_times_out_without_an_event(self) -> None:
        status, payload = self.get("/events/tail?consumer=idle&timeout=0.05")
        self.assertEqual(status, 204)
        self.assertIsNone(payload)

    def test_missing_and_wrong_tokens_are_rejected(self) -> None:
        body = json.dumps({"kind": "k", "entity": "e"}).encode()
        status, payload = self.harness.request("POST", "/events", body=body)
        self.assertEqual(status, 401)
        self.assertEqual(payload["error"]["code"], "unauthorized")

        wrong = {"X-Event-Token": "sha256=" + "0" * 64}
        status, payload = self.harness.request(
            "POST", "/events", body=body, headers=wrong
        )
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"]["code"], "forbidden")

    def test_oversized_body_is_refused_before_parsing(self) -> None:
        small_log = EventLog(Path(self.tmp.name) / "small.db")
        self.addCleanup(small_log.close)
        service = event_service.EventService(
            small_log, self.SECRET, max_body_bytes=32
        )
        with Harness(service) as harness:
            body = json.dumps({"kind": "k", "entity": "e" * 64}).encode()
            status, payload = harness.request(
                "POST",
                "/events",
                body=body,
                headers=event_headers(self.SECRET, "POST", "/events", body),
            )
        self.assertEqual(status, 413)
        self.assertEqual(payload["error"]["code"], "payload_too_large")

    def test_missing_field_is_a_validation_error(self) -> None:
        status, payload = self.post_event({"entity": "book-1"})
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "validation")

    def test_healthz_needs_no_token(self) -> None:
        status, payload = self.harness.request("GET", "/healthz")
        self.assertEqual(status, 200)
        self.assertEqual(payload["service"], "event")


def read_capability() -> Capability:
    return Capability(name="read", effect=EffectClass.READ)


def create_capability() -> Capability:
    return Capability(name="create", effect=EffectClass.CREATE, reversible=True)


def update_capability() -> Capability:
    return Capability(name="update", effect=EffectClass.UPDATE, reversible=True)


def delete_capability() -> Capability:
    return Capability(
        name="delete",
        effect=EffectClass.DELETE,
        reversible=False,
        requires_confirmation=True,
    )


def contract(source_id: str = "notes") -> SourceContract:
    return SourceContract(
        source_id=source_id,
        visibility=Visibility.PERSONAL,
        version=1,
        recovery_group=source_id,
        stable_id_fields=("id",),
        capabilities=(
            read_capability(),
            create_capability(),
            update_capability(),
            delete_capability(),
        ),
        authority=(FieldAuthority(field="title", authority=Authority.PORTABLE_FILE),),
        revision_field="rev",
        tombstone_field="deleted_at",
        epoch=1,
    )


class FakeAdapter:
    """In-memory adapter with the four operations the broker calls."""

    def __init__(self, records: dict[str, dict] | None = None) -> None:
        self.records = {key: dict(value) for key, value in (records or {}).items()}
        self.applies: list[tuple[str, str]] = []

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

    def apply(self, operation, idempotency_key: str) -> dict:
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


class BrokerServiceTests(unittest.TestCase):
    KEY = b"broker-test-key"

    def setUp(self) -> None:
        self.sources = {"notes": contract()}
        self.adapter = FakeAdapter(
            {"note-1": {"id": "note-1", "title": "Old", "rev": "rev-1"}}
        )
        self.issuer = IdentityIssuer(self.sources, self.KEY, clock=lambda: NOW)
        self.gate = ConfirmationGate(ttl_seconds=60, clock=lambda: NOW)
        self.store = FakeIntentStore()
        self.recorder = FakeRecorder()
        self.broker = Broker(
            self.sources,
            {"notes": self.adapter},
            self.issuer,
            self.gate,
            self.store,
            self.recorder,
            clock=lambda: NOW,
        )
        self.service = broker_service.BrokerService(self.broker, self.issuer)
        self.harness = Harness(self.service)
        self.harness.__enter__()
        self.addCleanup(self.harness.__exit__, None, None, None)
        token = self.issuer.issue(
            LOCAL_PROFILE,
            "agent",
            (("notes", "delete"), ("notes", "update"), ("notes", "create")),
        )
        self.headers = {"X-Workload-Token": token}

    def write(self, **overrides) -> dict:
        values = {
            "source_id": "notes",
            "capability": "delete",
            "targets": ["note-1"],
            "idempotency_key": "del-1",
            "field_diff": [],
            "base_revision": "rev-1",
        }
        values.update(overrides)
        return values

    def post(self, path: str, payload: dict) -> tuple[int, object]:
        return self.harness.request(
            "POST", path, body=json.dumps(payload).encode(), headers=self.headers
        )

    def test_propose_confirm_and_apply_with_the_external_operator_token(self) -> None:
        status, proposed = self.post("/propose", self.write())
        self.assertEqual(status, 201)
        self.assertEqual(proposed["intent"]["state"], "awaiting_confirmation")
        self.assertTrue(proposed["intent"]["requires_confirmation"])
        self.assertEqual(proposed["operation"]["capability"], "delete")

        # No operator token yet: the service refuses rather than issuing one.
        status, refused = self.post("/apply", self.write())
        self.assertEqual(status, 403)
        self.assertEqual(refused["error"]["code"], "confirmation_required")

        # The operator surface is outside this service; the test stands in for it.
        stored = self.store.find("del-1")
        _, operator_token = self.gate.issue(stored.operation, "operator")

        status, confirmed = self.post(
            "/confirm", self.write(confirmation_token=operator_token)
        )
        self.assertEqual(status, 200)
        self.assertEqual(confirmed["intent"]["state"], "approved")

        status, applied = self.post("/apply", self.write())
        self.assertEqual(status, 200)
        self.assertEqual(applied["effect"], {"applied": 1})
        self.assertNotIn("note-1", self.adapter.records)
        self.assertEqual(len(self.recorder.events), 1)

    def test_update_is_proposed_and_applied(self) -> None:
        write = self.write(
            capability="update",
            idempotency_key="update-1",
            field_diff=[["title", "Old", "New"]],
            base_revision="rev-1",
        )
        self.assertEqual(self.post("/propose", write)[0], 201)
        status, applied = self.post("/apply", write)
        self.assertEqual(status, 200)
        self.assertEqual(self.adapter.records["note-1"]["title"], "New")

    def test_missing_workload_token_is_unauthorized(self) -> None:
        status, payload = self.harness.request(
            "POST", "/propose", body=json.dumps(self.write()).encode()
        )
        self.assertEqual(status, 401)
        self.assertEqual(payload["error"]["code"], "unauthorized")

    def test_missing_idempotency_key_is_a_validation_error(self) -> None:
        write = self.write()
        del write["idempotency_key"]
        status, payload = self.post("/propose", write)
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "validation")

    def test_reconcile_unknown_intent_is_a_validation_error(self) -> None:
        status, payload = self.post("/reconcile", {"intent_id": "missing"})
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "validation")


class RelayServiceTests(unittest.TestCase):
    SECRET = b"relay-test-secret"

    def setUp(self) -> None:
        self.sunk: list[dict] = []
        self.relay = Relay(self.SECRET, self.schema(), self.sunk.append)
        self.harness = Harness(relay_service.RelayService(self.relay))
        self.harness.__enter__()
        self.addCleanup(self.harness.__exit__, None, None, None)

    @staticmethod
    def schema(**overrides) -> RecordSchema:
        values = {
            "required_fields": ("source_id", "object_id", "tombstone"),
            "field_types": {
                "source_id": str,
                "object_id": str,
                "tombstone": bool,
            },
            "max_bytes": 1024,
            "max_records_per_second": 10,
        }
        values.update(overrides)
        return RecordSchema(**values)

    @staticmethod
    def record(**overrides) -> dict:
        values = {"source_id": "mail", "object_id": "message-1", "tombstone": False}
        values.update(overrides)
        return values

    def ingest(
        self,
        payload: dict,
        *,
        key: str = "key-1",
        cursor: int = 1,
        signature: str | None = None,
        raw: bytes | None = None,
    ) -> tuple[int, object]:
        body = raw if raw is not None else json.dumps(payload).encode()
        signed = self.relay.sign(body, key) if signature is None else signature
        headers = {
            "X-Idempotency-Key": key,
            "X-Relay-Signature": f"sha256={signed}",
        }
        return self.harness.request(
            "POST", f"/ingest?cursor={cursor}", body=body, headers=headers
        )

    def test_ingest_is_idempotent_for_the_same_key_and_body(self) -> None:
        status, first = self.ingest(self.record())
        self.assertEqual(status, 202)
        self.assertEqual(first["accepted"], True)
        self.assertEqual(first["cursor"], 1)
        self.assertEqual(len(self.sunk), 1)

        status, again = self.ingest(self.record())
        self.assertEqual(status, 202)
        self.assertEqual(again, first)
        self.assertEqual(len(self.sunk), 1)

        status, conflict = self.ingest(self.record(object_id="message-2"))
        self.assertEqual(status, 409)
        self.assertEqual(conflict["error"]["code"], "idempotency_conflict")
        self.assertEqual(len(self.sunk), 1)

    def test_bad_signature_is_rejected(self) -> None:
        status, payload = self.ingest(self.record(), signature="0" * 64)
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"]["code"], "forbidden")
        self.assertEqual(self.sunk, [])

    def test_schema_violation_is_a_validation_error(self) -> None:
        status, payload = self.ingest(self.record(tombstone="no"))
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "validation")

    def test_oversized_body_is_refused(self) -> None:
        service = relay_service.RelayService(self.relay, max_body_bytes=16)
        with Harness(service) as harness:
            body = json.dumps(self.record()).encode()
            status, payload = harness.request(
                "POST",
                "/ingest?cursor=1",
                body=body,
                headers={
                    "X-Idempotency-Key": "key-1",
                    "X-Relay-Signature": f"sha256={self.relay.sign(body, 'key-1')}",
                },
            )
        self.assertEqual(status, 413)
        self.assertEqual(payload["error"]["code"], "payload_too_large")

    def test_cursor_from_header(self) -> None:
        body = json.dumps(self.record()).encode()
        status, payload = self.harness.request(
            "POST",
            "/ingest",
            body=body,
            headers={
                "X-Idempotency-Key": "key-1",
                "X-Cursor": "1",
                "X-Relay-Signature": f"sha256={self.relay.sign(body, 'key-1')}",
            },
        )
        self.assertEqual(status, 202)
        self.assertEqual(payload["cursor"], 1)

    def test_healthz_needs_no_signature(self) -> None:
        status, payload = self.harness.request("GET", "/healthz")
        self.assertEqual(status, 200)
        self.assertEqual(payload["service"], "relay")


class FakeSnapshotter:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def fence(self, group_id: str) -> None:
        self.calls.append(("fence", group_id))

    def checkpoint(self, group_id: str) -> None:
        self.calls.append(("checkpoint", group_id))

    def snapshot(self, group_id: str) -> dict[str, str]:
        self.calls.append(("snapshot", group_id))
        return {f"fixture-{group_id}": f"snap-{group_id}-1"}

    def resume(self, group_id: str) -> None:
        self.calls.append(("resume", group_id))


class BackupServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.snapshotter = FakeSnapshotter()
        self.controller = FanoutController(
            self.snapshotter,
            [InMemoryDestination("fixture-a"), InMemoryDestination("fixture-b")],
            Path(self.tmp.name) / "manifests",
            clock=lambda: 1_000.0,
        )
        self.service = backup_service.BackupService(
            self.controller, ("fixture-a", "fixture-b"), ("books",)
        )
        self.harness = Harness(self.service)
        self.harness.__enter__()
        self.addCleanup(self.harness.__exit__, None, None, None)

    def test_run_publishes_a_complete_generation(self) -> None:
        status, manifest = self.harness.request(
            "POST", "/run", body=json.dumps({"group": "books"}).encode()
        )
        self.assertEqual(status, 200)
        self.assertTrue(manifest["complete"])
        self.assertEqual(manifest["group_id"], "books")
        self.assertEqual(
            [item["destination"] for item in manifest["destinations"]],
            ["fixture-a", "fixture-b"],
        )
        self.assertEqual(
            self.snapshotter.calls,
            [
                ("fence", "books"),
                ("checkpoint", "books"),
                ("snapshot", "books"),
                ("resume", "books"),
            ],
        )
        self.assertTrue(list(Path(self.tmp.name, "manifests").glob("*.json")))

    def test_unknown_group_is_a_validation_error(self) -> None:
        status, payload = self.harness.request(
            "POST", "/run", body=json.dumps({"group": "music"}).encode()
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "validation")

    def test_healthz_reports_the_fixture_destinations(self) -> None:
        status, payload = self.harness.request("GET", "/healthz")
        self.assertEqual(status, 200)
        self.assertEqual(payload["destinations"], ["fixture-a", "fixture-b"])


if __name__ == "__main__":
    unittest.main()
