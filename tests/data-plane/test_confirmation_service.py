"""Operator-only confirmation surface: authentication, validation, TTL, signing.

Every server binds 127.0.0.1 on an ephemeral port. The signing key and operator
token are test literals; no network or cluster is involved.
"""

from __future__ import annotations

import copy
import json
import sys
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for candidate in (str(ROOT), str(ROOT / "libs")):
    if candidate not in sys.path:
        sys.path.insert(0, candidate)

from apps.data_plane import common, confirmation_service  # noqa: E402
from data_plane.approval import (  # noqa: E402
    SignedApprovalVerifier,
    UsedApprovalStore,
)
from data_plane.confirmation import NormalizedOperation  # noqa: E402
from data_plane.errors import ConfirmationError  # noqa: E402
from data_plane.model import (  # noqa: E402
    Authority,
    Capability,
    EffectClass,
    FieldAuthority,
    SourceContract,
    Visibility,
)

NOW = 1_000_000
OPERATOR_TOKEN = "separate-operator-token"
OPERATOR_KEY = b"separate-operator-signing-key"


class Harness:
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

    def request(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, object]:
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.server.server_address[1]}{path}",
            data=body,
            method=method,
            headers=dict(headers or {}),
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


def contract() -> SourceContract:
    return SourceContract(
        source_id="books",
        visibility=Visibility.GENERAL,
        version=1,
        recovery_group="books",
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


def operation(**overrides) -> dict:
    values = {
        "intent_id": "intent-1",
        "source_id": "books",
        "capability": "delete",
        "targets": ["book-1"],
        "base_revision": "rev-1",
        "field_diff": [["title", "Old", "New"]],
        "affected_count": 1,
        "epoch": 1,
        "recurrence": None,
        "attendee_scope": [],
        "destination": None,
    }
    values.update(overrides)
    return values


class ConfirmationServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = UsedApprovalStore()
        self.addCleanup(self.store.close)
        self.service = confirmation_service.ConfirmationService(
            OPERATOR_KEY,
            OPERATOR_TOKEN,
            {"books": contract()},
            store=self.store,
            max_ttl=900,
            clock=lambda: NOW,
        )
        self.harness = Harness(self.service)
        self.harness.__enter__()
        self.addCleanup(self.harness.__exit__, None, None, None)

    def approve(
        self,
        payload: dict,
        *,
        token: str | None = OPERATOR_TOKEN,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, object]:
        merged = {} if headers is not None else (
            {} if token is None else {confirmation_service.OPERATOR_TOKEN_HEADER: token}
        )
        if headers is not None:
            merged = dict(headers)
        return self.harness.request(
            "POST",
            "/approve",
            body=json.dumps(payload).encode(),
            headers=merged,
        )

    def test_operator_token_is_required(self) -> None:
        status, payload = self.approve(
            {"operation": operation(), "approved_by": "smores"}, token=None
        )
        self.assertEqual(status, 401)
        self.assertEqual(payload["error"]["code"], "unauthorized")

        status, payload = self.approve(
            {"operation": operation(), "approved_by": "smores"}, token="wrong"
        )
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"]["code"], "forbidden")
        self.assertNotIn("approval_token", payload)

    def test_a_bearer_token_is_not_the_operator_header(self) -> None:
        status, payload = self.approve(
            {"operation": operation(), "approved_by": "smores"},
            headers={"Authorization": f"Bearer {OPERATOR_TOKEN}"},
        )
        self.assertEqual(status, 401)
        self.assertEqual(payload["error"]["code"], "unauthorized")

    def test_a_service_without_an_operator_token_fails_closed(self) -> None:
        unconfigured = confirmation_service.ConfirmationService(
            OPERATOR_KEY, None, {"books": contract()}, store=self.store
        )
        with Harness(unconfigured) as harness:
            status, payload = harness.request("POST", "/approve", body=b"{}")
        self.assertEqual(status, 503)
        self.assertEqual(payload["error"]["code"], "unavailable")

    def test_tampered_operation_is_rejected(self) -> None:
        unknown = operation(source_id="other")
        status, payload = self.approve(
            {"operation": unknown, "approved_by": "smores"}
        )
        self.assertEqual(status, 400)

        stale = operation(epoch=99)
        status, payload = self.approve(
            {"operation": stale, "approved_by": "smores"}
        )
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"]["code"], "epoch")

        inconsistent = operation(affected_count=5)
        status, _ = self.approve(
            {"operation": inconsistent, "approved_by": "smores"}
        )
        self.assertEqual(status, 400)

    def test_supplied_operation_hash_must_match(self) -> None:
        status, payload = self.approve(
            {
                "operation": operation(),
                "approved_by": "smores",
                "operation_hash": "f" * 64,
            }
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "validation")

    def test_ttl_is_capped(self) -> None:
        status, payload = self.approve(
            {"operation": operation(), "approved_by": "smores", "ttl": 901}
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "validation")

        status, payload = self.approve(
            {"operation": operation(), "approved_by": "smores", "ttl": 0}
        )
        self.assertEqual(status, 400)

        status, payload = self.approve(
            {"operation": operation(), "approved_by": "smores", "ttl": 60}
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            payload["claims"]["expires_at"] - payload["claims"]["issued_at"], 60
        )

    def test_signed_token_verifies_and_is_single_use(self) -> None:
        status, payload = self.approve(
            {"operation": operation(), "approved_by": "smores"}
        )
        self.assertEqual(status, 200)
        token = payload["approval_token"]
        self.assertEqual(payload["claims"]["approved_by"], "smores")

        verifier = SignedApprovalVerifier(
            OPERATOR_KEY, self.store, clock=lambda: NOW, max_ttl=900
        )
        verified = verifier.verify(
            token, NormalizedOperation.from_normalized(operation())
        )
        self.assertEqual(verified.approved_by, "smores")

        with self.assertRaises(ConfirmationError):
            verifier.verify(
                token, NormalizedOperation.from_normalized(operation())
            )

    def test_healthz_is_minimal_and_needs_no_token(self) -> None:
        status, payload = self.harness.request("GET", "/healthz")
        self.assertEqual(status, 200)
        self.assertEqual(payload, {"status": "ok", "service": "confirmation"})

    def test_only_the_operator_routes_exist(self) -> None:
        self.assertEqual(
            set(self.service.routes), {("POST", "/approve"), ("GET", "/healthz")}
        )
        status, payload = self.harness.request(
            "POST",
            "/confirm",
            body=json.dumps({"operation": operation()}).encode(),
            headers={confirmation_service.OPERATOR_TOKEN_HEADER: OPERATOR_TOKEN},
        )
        self.assertEqual(status, 404)

    def test_operation_copy_is_not_mutated(self) -> None:
        payload = {"operation": copy.deepcopy(operation()), "approved_by": "smores"}
        original = copy.deepcopy(payload)
        status, _ = self.approve(payload)
        self.assertEqual(status, 200)
        self.assertEqual(payload, original)


if __name__ == "__main__":
    unittest.main()
