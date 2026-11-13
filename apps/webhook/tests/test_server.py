"""Request handling: registration, signature, size limit, dedupe, enqueue.

The handler is a pure function over (method, path, headers, body) so the whole
contract runs without a socket; `make_handler` is the thin HTTP adapter.
"""

from __future__ import annotations

import hashlib
import hmac
import io
import json
import tempfile
import unittest
from pathlib import Path

from webhook.dedupe import DedupeCache
from webhook.server import (
    MAX_FORWARD_BYTES,
    Application,
    BodyError,
    read_body,
    render_message,
)
from webhook.store import SourceStore

ADMIN = "admin-token"
PUBLIC_URL = "https://agent-hooks.sammohr.dev"
HMAC_SECRET = "hunter2"
BODY = b'{"action":"opened","number":1}'


class Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []
        self.fail = False

    def __call__(self, text: str, delivery: str | None) -> None:
        if self.fail:
            raise RuntimeError("gateway down")
        self.calls.append((text, delivery))


def signed(body: bytes, secret: str = HMAC_SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


class Harness(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = SourceStore(Path(self.tmp.name) / "sources.json")
        self.store.load()
        self.recorder = Recorder()
        self.app = Application(
            store=self.store,
            dedupe=DedupeCache(),
            enqueue=self.recorder,
            admin_token=ADMIN,
            public_url=PUBLIC_URL,
        )

    def register(self, name: str = "github", secret: str | None = None) -> str:
        response = self.app.dispatch(
            "POST",
            "/endpoints",
            {"Authorization": f"Bearer {ADMIN}"},
            json.dumps({"name": name, "secret": secret}).encode(),
        )
        self.assertEqual(response.status, 201, response.payload)
        return str(response.payload["capability_id"])

    def hook(
        self,
        capability: str,
        body: bytes = BODY,
        *,
        delivery: str | None = "delivery-1",
        signature: str | None = None,
        event: str = "pull_request",
    ):
        headers = {"Content-Type": "application/json"}
        if delivery is not None:
            headers["X-GitHub-Delivery"] = delivery
        if signature is not None:
            headers["X-Hub-Signature-256"] = signature
        if event:
            headers["X-GitHub-Event"] = event
        return self.app.dispatch("POST", f"/hooks/{capability}", headers, body)


class HealthTests(Harness):
    def test_healthz_is_public_and_plain(self) -> None:
        response = self.app.dispatch("GET", "/healthz", {}, b"")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.payload, {"status": "ok"})

    def test_unknown_path_is_404(self) -> None:
        self.assertEqual(self.app.dispatch("GET", "/nope", {}, b"").status, 404)

    def test_healthz_rejects_post(self) -> None:
        response = self.app.dispatch("POST", "/healthz", {}, b"")
        self.assertEqual(response.status, 405)


class RegistrationTests(Harness):
    def test_registration_requires_the_admin_token(self) -> None:
        body = json.dumps({"name": "github"}).encode()
        self.assertEqual(self.app.dispatch("POST", "/endpoints", {}, body).status, 401)
        wrong = {"Authorization": "Bearer nope"}
        self.assertEqual(self.app.dispatch("POST", "/endpoints", wrong, body).status, 401)

    def test_registration_returns_an_unguessable_capability_url(self) -> None:
        response = self.app.dispatch(
            "POST",
            "/endpoints",
            {"Authorization": f"Bearer {ADMIN}"},
            json.dumps({"name": "github", "secret": HMAC_SECRET}).encode(),
        )
        self.assertEqual(response.status, 201)
        assert response.payload is not None
        capability = response.payload["capability_id"]
        self.assertEqual(len(str(capability)), 43)
        self.assertEqual(response.payload["capability_url"], f"{PUBLIC_URL}/hooks/{capability}")
        self.assertTrue(response.payload["hmac"])

    def test_registration_without_a_secret_reports_no_hmac(self) -> None:
        response = self.app.dispatch(
            "POST",
            "/endpoints",
            {"Authorization": f"Bearer {ADMIN}"},
            json.dumps({"name": "generic"}).encode(),
        )
        assert response.payload is not None
        self.assertFalse(response.payload["hmac"])

    def test_registration_rejects_an_invalid_name(self) -> None:
        response = self.app.dispatch(
            "POST",
            "/endpoints",
            {"Authorization": f"Bearer {ADMIN}"},
            json.dumps({"name": "Not A Name"}).encode(),
        )
        self.assertEqual(response.status, 400)

    def test_registration_rejects_a_non_json_body(self) -> None:
        response = self.app.dispatch(
            "POST", "/endpoints", {"Authorization": f"Bearer {ADMIN}"}, b"nope"
        )
        self.assertEqual(response.status, 400)

    def test_registration_rejects_an_empty_secret(self) -> None:
        response = self.app.dispatch(
            "POST",
            "/endpoints",
            {"Authorization": f"Bearer {ADMIN}"},
            json.dumps({"name": "github", "secret": ""}).encode(),
        )
        self.assertEqual(response.status, 400)

    def test_re_registering_a_name_rotates_the_capability(self) -> None:
        old = self.register("github")
        new = self.register("github")
        self.assertNotEqual(old, new)
        self.assertEqual(self.hook(old).status, 404)
        self.assertEqual(self.hook(new).status, 202)

    def test_delete_revokes_a_capability(self) -> None:
        capability = self.register("github")
        response = self.app.dispatch(
            "DELETE", f"/endpoints/{capability}", {"Authorization": f"Bearer {ADMIN}"}, b""
        )
        self.assertEqual(response.status, 204)
        self.assertEqual(self.hook(capability).status, 404)

    def test_delete_requires_the_admin_token(self) -> None:
        capability = self.register("github")
        self.assertEqual(self.app.dispatch("DELETE", f"/endpoints/{capability}", {}, b"").status, 401)

    def test_a_non_ascii_authorization_header_is_a_401_not_a_crash(self) -> None:
        response = self.app.dispatch(
            "POST", "/endpoints", {"Authorization": "Bearer ünïcode"}, b'{"name":"x"}'
        )
        self.assertEqual(response.status, 401)


class HookTests(Harness):
    def test_an_unknown_capability_is_404(self) -> None:
        self.assertEqual(self.hook("A" * 43).status, 404)

    def test_a_source_with_a_secret_requires_a_signature(self) -> None:
        capability = self.register("github", HMAC_SECRET)
        self.assertEqual(self.hook(capability).status, 401)
        self.assertEqual(len(self.recorder.calls), 0)

    def test_a_source_with_a_secret_rejects_a_bad_signature(self) -> None:
        capability = self.register("github", HMAC_SECRET)
        self.assertEqual(self.hook(capability, signature=signed(BODY, "other")).status, 401)
        self.assertEqual(len(self.recorder.calls), 0)

    def test_a_valid_signature_enqueues_the_body(self) -> None:
        capability = self.register("github", HMAC_SECRET)
        response = self.hook(capability, signature=signed(BODY))
        self.assertEqual(response.status, 202)
        self.assertEqual(len(self.recorder.calls), 1)
        text, delivery = self.recorder.calls[0]
        self.assertEqual(delivery, "delivery-1")
        self.assertIn("github", text)
        self.assertIn("pull_request", text)
        self.assertIn(BODY.decode(), text)

    def test_a_source_without_a_secret_accepts_an_unsigned_delivery(self) -> None:
        capability = self.register("generic")
        self.assertEqual(self.hook(capability).status, 202)

    def test_a_duplicate_delivery_enqueues_once(self) -> None:
        capability = self.register("github", HMAC_SECRET)
        self.assertEqual(self.hook(capability, signature=signed(BODY)).status, 202)
        duplicate = self.hook(capability, signature=signed(BODY))
        self.assertEqual(duplicate.status, 200)
        assert duplicate.payload is not None
        self.assertEqual(duplicate.payload["status"], "duplicate")
        self.assertEqual(len(self.recorder.calls), 1)

    def test_a_failed_enqueue_releases_the_delivery_for_retry(self) -> None:
        capability = self.register("github", HMAC_SECRET)
        self.recorder.fail = True
        self.assertEqual(self.hook(capability, signature=signed(BODY)).status, 502)
        self.recorder.fail = False
        self.assertEqual(self.hook(capability, signature=signed(BODY)).status, 202)
        self.assertEqual(len(self.recorder.calls), 1)

    def test_a_body_over_the_limit_is_413_and_never_reaches_the_gateway(self) -> None:
        capability = self.register("github", HMAC_SECRET)
        oversized = b"x" * 17
        app = Application(
            store=self.store,
            dedupe=DedupeCache(),
            enqueue=self.recorder,
            admin_token=ADMIN,
            public_url=PUBLIC_URL,
            max_body_bytes=16,
        )
        response = app.dispatch(
            "POST",
            f"/hooks/{capability}",
            {"X-GitHub-Delivery": "big", "X-Hub-Signature-256": signed(oversized)},
            oversized,
        )
        self.assertEqual(response.status, 413)
        self.assertEqual(len(self.recorder.calls), 0)

    def test_the_default_limit_is_one_mebibyte(self) -> None:
        self.assertEqual(Application.__init__.__kwdefaults__["max_body_bytes"], 1024 * 1024)

    def test_a_delivery_without_an_id_is_deduped_by_body_hash(self) -> None:
        capability = self.register("generic")
        self.assertEqual(self.hook(capability, delivery=None).status, 202)
        self.assertEqual(self.hook(capability, delivery=None).status, 200)
        self.assertEqual(len(self.recorder.calls), 1)

    def test_a_trailing_slash_is_not_the_same_route(self) -> None:
        capability = self.register("generic")
        response = self.app.dispatch("POST", f"/hooks/{capability}/", {}, BODY)
        self.assertEqual(response.status, 404)

    def test_a_get_on_a_hook_is_405(self) -> None:
        capability = self.register("generic")
        self.assertEqual(self.app.dispatch("GET", f"/hooks/{capability}", {}, b"").status, 405)


class BodyTests(unittest.TestCase):
    def test_a_missing_content_length_is_411(self) -> None:
        with self.assertRaises(BodyError) as raised:
            read_body({}, io.BytesIO(b""), 1024)
        self.assertEqual(raised.exception.status, 411)

    def test_an_invalid_content_length_is_400(self) -> None:
        with self.assertRaises(BodyError) as raised:
            read_body({"Content-Length": "nope"}, io.BytesIO(b""), 1024)
        self.assertEqual(raised.exception.status, 400)

    def test_a_declared_length_over_the_limit_is_413_before_reading(self) -> None:
        stream = io.BytesIO(b"x" * 5000)
        with self.assertRaises(BodyError) as raised:
            read_body({"Content-Length": "5000"}, stream, 1024)
        self.assertEqual(raised.exception.status, 413)
        self.assertEqual(stream.tell(), 0)

    def test_a_truncated_body_is_400(self) -> None:
        with self.assertRaises(BodyError) as raised:
            read_body({"Content-Length": "10"}, io.BytesIO(b"short"), 1024)
        self.assertEqual(raised.exception.status, 400)

    def test_a_body_at_the_limit_is_read(self) -> None:
        self.assertEqual(read_body({"Content-Length": "4"}, io.BytesIO(b"four"), 4), b"four")

    def test_content_length_lookup_is_case_insensitive(self) -> None:
        self.assertEqual(read_body({"content-length": "4"}, io.BytesIO(b"four"), 4), b"four")


class RenderTests(unittest.TestCase):
    def test_the_message_names_the_source_the_event_and_the_delivery(self) -> None:
        text = render_message("github", "d-1", {"X-GitHub-Event": "push"}, b'{"ref":"main"}')
        self.assertIn("github", text)
        self.assertIn("d-1", text)
        self.assertIn("push", text)
        self.assertIn('{"ref":"main"}', text)

    def test_a_large_body_is_truncated_for_the_agent(self) -> None:
        text = render_message("github", "d-1", {}, b"a" * (MAX_FORWARD_BYTES + 10))
        self.assertLess(len(text), MAX_FORWARD_BYTES + 200)
        self.assertIn("truncated", text)

    def test_a_binary_body_is_replaced_not_dropped(self) -> None:
        text = render_message("github", "d-1", {}, b"\xff\xfe")
        self.assertIn("\ufffd", text)


if __name__ == "__main__":
    unittest.main()
