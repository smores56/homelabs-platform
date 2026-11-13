"""The HTTP contract: register a source, receive a delivery, report health.

`POST /endpoints` is the only way to mint a capability URL, so it requires the
platform admin token: the hook path is public by construction, and an
unauthenticated registration route would let anyone mint their own. The hook
path itself carries no session auth; the unguessable id is the credential, plus
the source's HMAC secret when one was registered.

Dispatch is a pure function over (method, path, headers, body), which is what
the tests drive; `make_handler` only moves bytes.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .dedupe import DedupeCache
from .signature import SIGNATURE_HEADER, verify
from .store import SourceStore

LOG = logging.getLogger("webhook")

MAX_BODY_BYTES = 1024 * 1024
MAX_FORWARD_BYTES = 64 * 1024
DELIVERY_HEADERS = ("X-GitHub-Delivery", "X-Delivery-Id", "X-Request-Id")
EVENT_HEADERS = ("X-GitHub-Event", "X-Event-Type")

JSON_HEADERS = (("Cache-Control", "no-store"),)


class BodyError(Exception):
    """The request body cannot be read under the size contract."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass(frozen=True)
class Response:
    status: int
    payload: dict[str, Any] | None = None
    headers: tuple[tuple[str, str], ...] = JSON_HEADERS


def header(headers: Mapping[str, str], name: str) -> str | None:
    """Case-insensitive lookup that works for HTTPMessage and plain dicts."""
    value = headers.get(name)
    if value is not None:
        return value
    wanted = name.lower()
    for key, candidate in headers.items():
        if key.lower() == wanted:
            return candidate
    return None


def read_body(headers: Mapping[str, str], stream: Any, max_bytes: int) -> bytes:
    raw = header(headers, "Content-Length")
    if raw is None:
        # No length means no known boundary; refuse rather than guess (a chunked
        # body is not something GitHub sends).
        raise BodyError(411, "content-length required")
    try:
        length = int(raw)
    except ValueError as exc:
        raise BodyError(400, "invalid content-length") from exc
    if length < 0:
        raise BodyError(400, "invalid content-length")
    if length > max_bytes:
        raise BodyError(413, f"body exceeds {max_bytes} bytes")
    body = stream.read(length)
    if len(body) != length:
        raise BodyError(400, "truncated body")
    return body


def render_message(source: str, delivery: str, headers: Mapping[str, str], body: bytes) -> str:
    """One agent turn's text: who delivered what, then the payload."""
    event = next((value for name in EVENT_HEADERS if (value := header(headers, name))), "unknown")
    truncated = len(body) > MAX_FORWARD_BYTES
    text = body[:MAX_FORWARD_BYTES].decode("utf-8", "replace")
    message = (
        f"A webhook delivery arrived for source {source!r}.\n"
        f"event: {event}\n"
        f"delivery: {delivery}\n"
        "\n"
        f"{text}"
    )
    if truncated:
        message += f"\n\n[body truncated at {MAX_FORWARD_BYTES} bytes]"
    return message


def _route(path: str) -> tuple[str, str]:
    path = path.split("?", 1)[0]
    if path == "/healthz":
        return "healthz", ""
    if path == "/endpoints":
        return "endpoints", ""
    if not path.startswith("/"):
        return "unknown", ""
    # No aliasing: a trailing slash is a different path and stays a 404.
    segments = path[1:].split("/")
    if len(segments) == 2 and segments[0] == "endpoints" and segments[1]:
        return "endpoint", segments[1]
    if len(segments) == 2 and segments[0] == "hooks" and segments[1]:
        return "hook", segments[1]
    return "unknown", ""


class Application:
    def __init__(
        self,
        *,
        store: SourceStore,
        dedupe: DedupeCache,
        enqueue: Callable[[str, str | None], None],
        admin_token: str,
        public_url: str,
        max_body_bytes: int = MAX_BODY_BYTES,
    ) -> None:
        if not admin_token:
            raise ValueError("admin_token must not be empty")
        self._store = store
        self._dedupe = dedupe
        self._enqueue = enqueue
        self._admin_token = admin_token
        self._public_url = public_url.rstrip("/")
        self.max_body_bytes = max_body_bytes

    def dispatch(
        self, method: str, path: str, headers: Mapping[str, str], body: bytes
    ) -> Response:
        route, parameter = _route(path)
        if route == "healthz":
            response = self._only(method, "GET", lambda: Response(200, {"status": "ok"}))
        elif route == "endpoints":
            response = self._only(method, "POST", lambda: self._register(headers, body))
        elif route == "endpoint":
            response = self._only(method, "DELETE", lambda: self._revoke(parameter, headers))
        elif route == "hook":
            response = self._only(method, "POST", lambda: self._hook(parameter, headers, body))
        else:
            response = Response(404, {"error": "not found"})
        # Metadata only: no capability id, no header value, no body.
        LOG.info(
            "method=%s route=%s status=%d bytes=%d", method, route, response.status, len(body)
        )
        return response

    @staticmethod
    def _only(method: str, allowed: str, handle: Callable[[], Response]) -> Response:
        if method != allowed:
            return Response(405, {"error": "method not allowed"}, (("Allow", allowed),))
        return handle()

    def _authorized(self, headers: Mapping[str, str]) -> bool:
        supplied = header(headers, "Authorization") or ""
        # Bytes, not str: compare_digest rejects non-ASCII text with a
        # TypeError, and this runs on a public route.
        return hmac.compare_digest(supplied.encode("utf-8", "replace"), f"Bearer {self._admin_token}".encode())

    def _register(self, headers: Mapping[str, str], body: bytes) -> Response:
        if not self._authorized(headers):
            return Response(401, {"error": "unauthorized"})
        if len(body) > self.max_body_bytes:
            return Response(413, {"error": f"body exceeds {self.max_body_bytes} bytes"})
        try:
            payload = json.loads(body or b"null")
        except json.JSONDecodeError:
            return Response(400, {"error": "body is not JSON"})
        if not isinstance(payload, dict):
            return Response(400, {"error": "body must be an object"})
        name = payload.get("name")
        secret = payload.get("secret")
        if not isinstance(name, str):
            return Response(400, {"error": "name is required"})
        if secret is not None and not isinstance(secret, str):
            return Response(400, {"error": "secret must be a string"})
        try:
            source = self._store.register(name, secret)
        except ValueError as exc:
            return Response(400, {"error": str(exc)})
        return Response(
            201,
            {
                "name": source.name,
                "capability_id": source.capability,
                "capability_url": f"{self._public_url}/hooks/{source.capability}",
                "hmac": source.secret is not None,
            },
        )

    def _revoke(self, capability: str, headers: Mapping[str, str]) -> Response:
        if not self._authorized(headers):
            return Response(401, {"error": "unauthorized"})
        if not self._store.revoke(capability):
            return Response(404, {"error": "unknown source"})
        return Response(204)

    def _hook(self, capability: str, headers: Mapping[str, str], body: bytes) -> Response:
        source = self._store.get(capability)
        if source is None:
            return Response(404, {"error": "unknown source"})
        if len(body) > self.max_body_bytes:
            return Response(413, {"error": f"body exceeds {self.max_body_bytes} bytes"})
        if source.secret is not None and not verify(
            source.secret.encode(), body, header(headers, SIGNATURE_HEADER)
        ):
            return Response(401, {"error": "invalid signature"})
        delivery = self._delivery(headers, body)
        key = f"{source.name}:{delivery}"
        if not self._dedupe.claim(key):
            return Response(200, {"status": "duplicate", "delivery": delivery})
        try:
            self._enqueue(render_message(source.name, delivery, headers, body), delivery)
        except Exception as exc:  # noqa: BLE001 - any failure is a gateway failure
            self._dedupe.release(key)
            # The exception class only: a message could echo request content.
            LOG.warning(
                "enqueue failed source=%s delivery=%s error=%s",
                source.name,
                delivery,
                type(exc).__name__,
            )
            return Response(502, {"error": "gateway unavailable"})
        return Response(202, {"status": "accepted", "source": source.name, "delivery": delivery})

    @staticmethod
    def _delivery(headers: Mapping[str, str], body: bytes) -> str:
        for name in DELIVERY_HEADERS:
            value = header(headers, name)
            if value:
                return value.strip()
        return hashlib.sha256(body).hexdigest()[:32]


class _Handler(BaseHTTPRequestHandler):
    server_version = "webhook"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    app: Application

    def do_GET(self) -> None:  # noqa: N802 - http.server's naming
        self._handle("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._handle("POST")

    def do_DELETE(self) -> None:  # noqa: N802
        self._handle("DELETE")

    def _handle(self, method: str) -> None:
        body = b""
        if method == "POST":
            try:
                body = read_body(self.headers, self.rfile, self.app.max_body_bytes)
            except BodyError as exc:
                self.close_connection = True
                self._reply(Response(exc.status, {"error": exc.message}))
                return
        self._reply(self.app.dispatch(method, self.path, self.headers, body))

    def _reply(self, response: Response) -> None:
        body = b"" if response.payload is None else json.dumps(response.payload).encode()
        self.send_response(response.status)
        if response.payload is not None:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for name, value in response.headers:
            self.send_header(name, value)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def handle_expect_100(self) -> bool:
        try:
            length = int(header(self.headers, "Content-Length") or "-1")
        except ValueError:
            length = -1
        if length > self.app.max_body_bytes:
            self.close_connection = True
            self._reply(Response(413, {"error": f"body exceeds {self.app.max_body_bytes} bytes"}))
            return False
        return super().handle_expect_100()

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        # The default writes to stderr with the raw path; the dispatch line is
        # the reviewed shape and it never carries a capability id.
        LOG.debug("http %s", format % args)


def make_handler(app: Application) -> type[_Handler]:
    return type("BoundHandler", (_Handler,), {"app": app})


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def make_server(app: Application, address: str, port: int) -> Server:
    return Server((address, port), make_handler(app))
