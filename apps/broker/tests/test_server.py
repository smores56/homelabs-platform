"""End-to-end broker tests against a fake upstream and an injected resolver.

The connector is replaced so the test traffic stays on loopback; the address
policy is still real (the resolver's answer is what gets checked), and the
credential injection, redirect, redaction and log assertions run the shipped
handler.
"""

from __future__ import annotations

import http.client
import io
import json
import logging
import re
import socket
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from broker.config import BodyGuard, Config, Enrollment, Injection
from broker.credentials import StaticCredentials
from broker.server import Broker, make_server

SECRET = "s3cr3t-broker-token"
AGENT_SECRET = "agent-supplied-value"
HOOK_PATH = "/repos/smores56/kanbei/hooks"
HOOK_URL = "https://agent-hooks.sammohr.dev/kanbei"


class UpstreamHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    requests: list[dict] = []

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        return

    def reply(self, status: int, body: bytes, headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        type(self).requests.append(
            {"path": self.path, "headers": {name.lower(): value for name, value in self.headers.items()}}
        )
        if self.path.startswith("/inject"):
            self.reply(200, json.dumps({"ok": True}).encode())
        elif self.path.startswith("/redirect-cross"):
            self.reply(302, b"", {"Location": "https://evil.example/steal"})
        elif self.path.startswith("/redirect-same"):
            self.reply(302, b"", {"Location": "/next"})
        elif self.path.startswith("/echo"):
            self.reply(200, f"token={SECRET}".encode())
        elif self.path.startswith("/agent-echo"):
            self.reply(200, f"echo={AGENT_SECRET}".encode())
        else:
            self.reply(404, b"unknown")

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length)
        type(self).requests.append(
            {
                "path": self.path,
                "headers": {name.lower(): value for name, value in self.headers.items()},
                "body": body,
            }
        )
        self.reply(200, b"posted")


def resolver_public(host: str, port: int, *, type: int = socket.SOCK_STREAM) -> list[tuple]:
    return [(socket.AF_INET, type, 6, "", ("140.82.1.1", port))]


def resolver_pod(host: str, port: int, *, type: int = socket.SOCK_STREAM) -> list[tuple]:
    return [(socket.AF_INET, type, 6, "", ("10.42.0.9", port))]


class BrokerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.upstream = ThreadingHTTPServer(("127.0.0.1", 0), UpstreamHandler)
        cls.upstream.daemon_threads = True
        threading.Thread(target=cls.upstream.serve_forever, daemon=True).start()
        cls.upstream_port = cls.upstream.server_address[1]

    @classmethod
    def tearDownClass(cls) -> None:
        cls.upstream.shutdown()
        cls.upstream.server_close()

    def setUp(self) -> None:
        UpstreamHandler.requests = []
        self.config = Config(
            listen_host="127.0.0.1",
            listen_port=0,
            deny_addresses=(),
            hosts={
                "upstream": Enrollment(
                    name="upstream",
                    scheme="https",
                    host="upstream.test",
                    port=443,
                    allow_private=False,
                    secret_name="test-token",
                    injection=Injection(kind="bearer"),
                ),
                "open": Enrollment(
                    name="open",
                    scheme="https",
                    host="open.test",
                    port=443,
                    allow_private=False,
                    secret_name="",
                    injection=Injection(kind="none"),
                ),
                "loki": Enrollment(
                    name="loki",
                    scheme="http",
                    host="loki-gateway.observability.svc.cluster.local",
                    port=3100,
                    allow_private=True,
                    secret_name="loki-token",
                    injection=Injection(kind="bearer"),
                    headers=(("X-Scope-OrgID", "data"),),
                ),
                "hooks": Enrollment(
                    name="hooks",
                    scheme="https",
                    host="api.github.com",
                    port=443,
                    allow_private=False,
                    secret_name="hooks-token",
                    injection=Injection(kind="bearer"),
                    allow_methods=("POST",),
                    allow_paths=(re.compile(r"^/repos/[^/]+/[^/]+/hooks$"),),
                    body_guards=(
                        BodyGuard(
                            methods=("POST",),
                            paths=(re.compile(r"^/repos/[^/]+/[^/]+/hooks$"),),
                            field="config.url",
                            allow=re.compile(r"^https://agent-hooks\.sammohr\.dev/"),
                        ),
                    ),
                ),
                "limited": Enrollment(
                    name="limited",
                    scheme="https",
                    host="notify.test",
                    port=443,
                    allow_private=False,
                    secret_name="",
                    injection=Injection(kind="none"),
                    allow_methods=("POST",),
                    max_body_bytes=8,
                ),
            },
        )
        self.logs = io.StringIO()
        self.log_handler = logging.StreamHandler(self.logs)
        logger = logging.getLogger("broker")
        logger.setLevel(logging.INFO)
        logger.addHandler(self.log_handler)
        self.addCleanup(logger.removeHandler, self.log_handler)
        self.start(resolver_public)

    def start(self, resolver) -> None:
        broker = Broker(
            config=self.config,
            credentials=StaticCredentials(
                {"test-token": SECRET, "loki-token": "loki-secret", "hooks-token": "hooks-secret"}
            ),
            resolver=resolver,
            connector=lambda enrollment, pinned, timeout: http.client.HTTPConnection(
                "127.0.0.1", self.upstream_port, timeout=timeout
            ),
        )
        self.server = make_server(broker, "127.0.0.1", 0)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.port = self.server.server_address[1]

    def wait_for_log(self, needle: str, timeout: float = 5.0) -> str:
        """The log line is written after the response body; poll rather than race it."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = self.logs.getvalue()
            if needle in value:
                return value
            time.sleep(0.02)
        return self.logs.getvalue()

    def request(self, method: str, path: str, headers: dict[str, str] | None = None, body: bytes | None = None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        sent = {"Host": "broker.test"}
        sent.update(headers or {})
        connection.request(method, path, body=body, headers=sent)
        response = connection.getresponse()
        data = response.read()
        connection.close()
        return response, data

    def test_injects_one_credential_and_strips_agent_auth(self) -> None:
        response, _ = self.request(
            "GET",
            "/upstream/inject",
            {
                "Authorization": f"Bearer {AGENT_SECRET}",
                "Proxy-Authorization": "Basic abc",
                "Cookie": "session=1",
                "X-Api-Key": AGENT_SECRET,
                "X-Scope-OrgID": "raw",
            },
        )
        self.assertEqual(response.status, 200)
        headers = UpstreamHandler.requests[-1]["headers"]
        self.assertEqual(headers["authorization"], f"Bearer {SECRET}")
        self.assertEqual(headers["host"], "upstream.test")
        for name in ("cookie", "proxy-authorization", "x-api-key", "x-scope-orgid"):
            self.assertNotIn(name, headers, name)

    def test_loki_header_is_forced(self) -> None:
        response, _ = self.request("GET", "/loki/loki/api/v1/labels", {"X-Scope-OrgID": "raw"})
        self.assertEqual(response.status, 404)  # fake upstream has no such path
        headers = UpstreamHandler.requests[-1]["headers"]
        self.assertEqual(headers["x-scope-orgid"], "data")
        self.assertEqual(headers["authorization"], "Bearer loki-secret")
        self.assertEqual(headers["host"], "loki-gateway.observability.svc.cluster.local:3100")

    def test_uncredentialed_host_forwards_without_auth(self) -> None:
        response, _ = self.request("GET", "/open/inject")
        self.assertEqual(response.status, 200)
        self.assertNotIn("authorization", UpstreamHandler.requests[-1]["headers"])

    def test_log_has_metadata_and_no_secrets_or_query(self) -> None:
        self.request("GET", f"/upstream/inject?q={AGENT_SECRET}")
        logs = self.wait_for_log("logical=upstream")
        self.assertIn("logical=upstream method=GET path=/inject status=200 bytes=", logs)
        self.assertNotIn(SECRET, logs)
        self.assertNotIn(AGENT_SECRET, logs)
        self.assertNotIn("?q=", logs)

    def test_cross_host_redirect_is_a_denial(self) -> None:
        response, body = self.request("GET", "/upstream/redirect-cross")
        self.assertEqual(response.status, 502)
        self.assertIn(b"cross_host_redirect", body)
        self.assertNotIn(b"evil.example", body)

    def test_same_host_redirect_is_returned_unfollowed(self) -> None:
        response, _ = self.request("GET", "/upstream/redirect-same")
        self.assertEqual(response.status, 302)
        self.assertEqual(response.getheader("Location"), "/next")

    def test_response_body_redaction(self) -> None:
        response, body = self.request("GET", "/upstream/echo")
        self.assertNotIn(SECRET.encode(), body)
        self.assertIn(b"[REDACTED:upstream]", body)
        self.assertEqual(response.getheader("X-Broker-Redaction"), "full")

    def test_connect_is_refused(self) -> None:
        response, body = self.request("CONNECT", "upstream.test:443")
        self.assertEqual(response.status, 405)
        self.assertIn(b"connect_not_allowed", body)

    def test_ip_literal_host_is_refused(self) -> None:
        response, body = self.request("GET", "/upstream/inject", {"Host": "10.0.0.1"})
        self.assertEqual(response.status, 400)
        self.assertIn(b"ip_literal_host", body)

    def test_unknown_logical_host(self) -> None:
        response, body = self.request("GET", "/nope/inject")
        self.assertEqual(response.status, 404)
        self.assertIn(b"unknown_logical_host", body)

    def test_post_body_is_forwarded(self) -> None:
        response, _ = self.request("POST", "/upstream/post", body=b'{"a":1}')
        self.assertEqual(response.status, 200)
        self.assertEqual(UpstreamHandler.requests[-1]["body"], b'{"a":1}')

    def test_hook_creation_with_the_enrolled_url_is_forwarded(self) -> None:
        raw = json.dumps({"config": {"url": HOOK_URL, "content_type": "json"}}).encode()
        response, _ = self.request("POST", f"/hooks{HOOK_PATH}", body=raw)
        self.assertEqual(response.status, 200)
        forwarded = UpstreamHandler.requests[-1]
        self.assertEqual(forwarded["path"], HOOK_PATH)
        self.assertEqual(forwarded["body"], raw)
        self.assertEqual(forwarded["headers"]["authorization"], "Bearer hooks-secret")

    def test_hook_creation_with_another_url_is_refused_and_not_forwarded(self) -> None:
        raw = json.dumps({"config": {"url": "https://evil.example/collect"}}).encode()
        response, body = self.request("POST", f"/hooks{HOOK_PATH}", body=raw)
        self.assertEqual(response.status, 403)
        self.assertIn(b"body_field_denied", body)
        self.assertNotIn(b"evil.example", body)
        self.assertEqual(UpstreamHandler.requests, [])

    def test_malformed_hook_body_is_refused(self) -> None:
        response, body = self.request("POST", f"/hooks{HOOK_PATH}", body=b"{not json")
        self.assertEqual(response.status, 403)
        self.assertIn(b"body_json_invalid", body)
        self.assertEqual(UpstreamHandler.requests, [])

    def test_method_and_path_predicates_refuse_without_forwarding(self) -> None:
        response, body = self.request("GET", f"/hooks{HOOK_PATH}")
        self.assertEqual(response.status, 403)
        self.assertIn(b"method_denied", body)
        response, body = self.request(
            "POST", "/hooks/repos/smores56/kanbei/issues", body=b'{"config": {"url": "x"}}'
        )
        self.assertEqual(response.status, 403)
        self.assertIn(b"path_denied", body)
        self.assertEqual(UpstreamHandler.requests, [])

    def test_oversized_body_is_refused_before_forwarding(self) -> None:
        response, body = self.request("POST", "/limited/notify", body=b"x" * 9)
        self.assertEqual(response.status, 413)
        self.assertIn(b"request_too_large", body)
        self.assertEqual(UpstreamHandler.requests, [])

    def test_denial_logs_the_reason_and_never_the_body(self) -> None:
        raw = json.dumps({"config": {"url": "https://evil.example/" + AGENT_SECRET}}).encode()
        self.request("POST", f"/hooks{HOOK_PATH}", body=raw)
        logs = self.wait_for_log("reason=body_field_denied")
        self.assertIn("logical=hooks method=POST path=" + HOOK_PATH, logs)
        self.assertIn("status=403", logs)
        self.assertIn("outcome=denied", logs)
        self.assertNotIn(AGENT_SECRET, logs)
        self.assertNotIn("evil.example", logs)

    def test_pod_address_is_refused_before_connect(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.start(resolver_pod)
        response, body = self.request("GET", "/upstream/inject")
        self.assertEqual(response.status, 403)
        self.assertIn(b"address_refused", body)
        self.assertEqual(UpstreamHandler.requests, [])


if __name__ == "__main__":
    unittest.main()
