"""HTTP front end: `/ <logical-host> / <path>` -> enrolled upstream.

One listener, one credential per logical host, no CONNECT, no redirect following.
The broker reads the request body, resolves the enrolled host, dials the address
it checked, and streams the answer back with known secret values redacted.
"""

from __future__ import annotations

import base64
import http.client
import logging
import socket
import ssl
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable
from urllib.parse import quote

from .config import HEADER_NAME, Config, Enrollment
from .credentials import Credentials, StaticCredentials
from .github_app import CredentialUnavailable
from .policy import Denied, PinnedAddress, check_redirect, resolve_pinned, split_target
from .redact import redact, redact_header

LOG = logging.getLogger("broker")

HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-connection",
        "te",
        "trailer",
        "trailers",
        "transfer-encoding",
        "upgrade",
        "host",
        "content-length",
        "accept-encoding",
        "expect",
    }
)
# Everything an agent might send in place of a credential. None of it may reach
# an upstream; the only credential on the wire is the enrolled one.
AGENT_CREDENTIALS = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "cookie",
        "set-cookie",
        "x-api-key",
        "api-key",
        "x-auth-token",
        "x-github-token",
        "private-token",
        "x-scope-orgid",
        "x-forwarded-for",
        "forwarded",
    }
)
METHODS = ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS")
MAX_REQUEST_BYTES = 64 * 1024 * 1024
REDACTION_CAP = 1024 * 1024
CONNECT_TIMEOUT = 10.0
READ_TIMEOUT = 60.0

Connector = Callable[[Enrollment, PinnedAddress, float], http.client.HTTPConnection]
Resolver = Callable[..., list[tuple]]


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """TLS to the checked address with the enrolled name for SNI and verification."""

    def __init__(self, host: str, port: int, ip: str, context: ssl.SSLContext, timeout: float) -> None:
        super().__init__(host, port, timeout=timeout, context=context)
        self._pinned_ip = ip

    def connect(self) -> None:
        self.sock = socket.create_connection((self._pinned_ip, self.port), self.timeout)
        self.sock = self._context.wrap_socket(self.sock, server_hostname=self.host)


def connect_pinned(enrollment: Enrollment, pinned: PinnedAddress, timeout: float) -> http.client.HTTPConnection:
    if enrollment.scheme == "https":
        context = ssl.create_default_context()
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        return _PinnedHTTPSConnection(enrollment.host, enrollment.port, str(pinned.ip), context, timeout)
    return http.client.HTTPConnection(str(pinned.ip), enrollment.port, timeout=timeout)


def build_outbound_headers(
    inbound: list[tuple[str, str]],
    enrollment: Enrollment,
    secret: str,
) -> list[tuple[str, str]]:
    """Drop hop-by-hop and every agent credential, then inject exactly one."""
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for name, value in inbound:
        lower = name.lower()
        if lower in HOP_BY_HOP or lower in AGENT_CREDENTIALS:
            continue
        if lower in seen:
            continue
        seen.add(lower)
        out.append((name, value))
    out.append(("Host", enrollment.host_header))
    for name, value in enrollment.headers:
        if name.lower() in seen:
            out = [(n, v) for n, v in out if n.lower() != name.lower()]
        out.append((name, value))
        seen.add(name.lower())
    injection = enrollment.injection
    if injection.kind == "bearer":
        out.append((injection.header, f"Bearer {secret}"))
    elif injection.kind == "basic":
        token = base64.b64encode(f"{injection.username}:{secret}".encode()).decode()
        out.append((injection.header, f"Basic {token}"))
    elif injection.kind == "header":
        out.append((injection.header, secret))
    out.append(("Accept-Encoding", "identity"))
    return out


@dataclass
class Broker:
    config: Config
    credentials: Credentials
    resolver: Resolver = socket.getaddrinfo
    connector: Connector = connect_pinned

    def credential(self, enrollment: Enrollment) -> str:
        return self.credentials.value(enrollment)

    def handle(self, handler: BaseHTTPRequestHandler) -> None:
        method = handler.command.upper()
        raw_target = handler.path
        logical = "-"
        path = "-"
        status = 500
        size = 0
        outcome = "error"
        try:
            if method in ("CONNECT", "TRACE"):
                raise Denied("connect_not_allowed", status=405)
            if method not in METHODS:
                raise Denied("method_not_allowed", status=405)
            if raw_target.split("?", 1)[0] == "/healthz":
                status = self._health(handler)
                outcome = "health"
                return
            logical, path, query = split_target(raw_target, handler.headers.get("Host"))
            enrollment = self.config.hosts.get(logical)
            if enrollment is None:
                raise Denied("unknown_logical_host", status=404)
            body = self._read_body(handler)
            pinned = resolve_pinned(
                enrollment.host,
                enrollment.port,
                allow_private=enrollment.allow_private,
                deny_addresses=self.config.deny_addresses,
                resolver=self.resolver,
            )
            status, size = self._relay(handler, enrollment, pinned, method, path, query, body)
            outcome = "proxied"
        except Denied as denial:
            status = self._deny(handler, denial, logical)
            outcome = "denied"
        except CredentialUnavailable as exc:
            # Never forward without the credential: a minting failure is a 503.
            LOG.warning("broker credential unavailable logical=%s class=%s", logical, type(exc).__name__)
            status = self._deny(handler, Denied("credential_unavailable", status=503), logical)
            outcome = "denied"
        except (OSError, http.client.HTTPException, ssl.SSLError) as exc:
            # The exception text can name the pinned address; log its class only.
            status = self._deny(handler, Denied("upstream_unreachable", status=502), logical)
            outcome = "denied"
            LOG.warning("broker upstream failure class=%s", type(exc).__name__)
        except Exception as exc:  # pragma: no cover - last-resort denial
            LOG.error("unhandled broker error class=%s", type(exc).__name__)
            status = self._deny(handler, Denied("internal_error", status=500), logical)
            outcome = "error"
        finally:
            if outcome != "health":
                LOG.info(
                    "logical=%s method=%s path=%s status=%d bytes=%d outcome=%s",
                    logical,
                    method,
                    self._safe_path(path, logical),
                    status,
                    size,
                    outcome,
                )

    def _safe_path(self, path: str, logical: str) -> str:
        return redact(path, self.credentials.values(), label=logical)

    def _health(self, handler: BaseHTTPRequestHandler) -> int:
        handler.send_response_only(200)
        handler.send_header("Content-Type", "text/plain; charset=utf-8")
        handler.send_header("Content-Length", "3")
        handler.end_headers()
        handler.wfile.write(b"ok\n")
        return 200

    def _read_body(self, handler: BaseHTTPRequestHandler) -> bytes:
        if handler.headers.get("Transfer-Encoding", "").lower() not in ("", "identity"):
            raise Denied("chunked_request_body", status=411)
        try:
            length = int(handler.headers.get("Content-Length") or 0)
        except ValueError:
            raise Denied("bad_content_length", status=400) from None
        if length < 0:
            raise Denied("bad_content_length", status=400)
        if length > MAX_REQUEST_BYTES:
            raise Denied("request_too_large", status=413)
        if handler.headers.get("Expect", "").lower() == "100-continue":
            # Git and curl wait for this before sending the body. The broker
            # answers it itself; the upstream is contacted only after the body
            # and the target checks are complete.
            handler.wfile.write(b"HTTP/1.1 100 Continue\r\n\r\n")
            handler.wfile.flush()
        try:
            body = handler.rfile.read(length)
        except OSError:
            raise Denied("body_read_failed", status=400) from None
        if len(body) != length:
            raise Denied("body_read_failed", status=400)
        return body

    def _relay(
        self,
        handler: BaseHTTPRequestHandler,
        enrollment: Enrollment,
        pinned: PinnedAddress,
        method: str,
        path: str,
        query: str,
        body: bytes,
    ) -> tuple[int, int]:
        headers = build_outbound_headers(list(handler.headers.items()), enrollment, self.credential(enrollment))
        if body or method in ("POST", "PUT", "PATCH"):
            headers.append(("Content-Length", str(len(body))))
        if any(character < " " for character in query):
            raise Denied("ambiguous_query", status=400)
        # Re-encode the decoded path: dot segments are already canonical, and a
        # space or control character must never reach the request line.
        target = quote(path, safe="/:@!$&'()*+,;=-._~") + (f"?{query}" if query else "")

        connection = self.connector(enrollment, pinned, CONNECT_TIMEOUT)
        try:
            connection.putrequest(method, target, skip_host=True, skip_accept_encoding=True)
            for name, value in headers:
                connection.putheader(name, value)
            connection.endheaders()
            if body:
                connection.send(body)
            sock = getattr(connection, "sock", None)
            if sock is not None:
                sock.settimeout(READ_TIMEOUT)
            upstream = connection.getresponse()
            return self._send_response(handler, enrollment, method, upstream)
        finally:
            connection.close()

    def _send_response(
        self,
        handler: BaseHTTPRequestHandler,
        enrollment: Enrollment,
        method: str,
        upstream: http.client.HTTPResponse,
    ) -> tuple[int, int]:
        secrets = self.credentials.values()
        label = enrollment.name
        upstream_headers = upstream.getheaders()
        if 300 <= upstream.status < 400:
            for name, value in upstream_headers:
                if name.lower() == "location":
                    check_redirect(value, scheme=enrollment.scheme, host=enrollment.host, port=enrollment.port)

        forwarded: list[tuple[str, str]] = []
        for name, value in upstream_headers:
            if name.lower() in HOP_BY_HOP or name.lower() == "content-length":
                continue
            if not HEADER_NAME.match(name) or any(bad in value for bad in ("\r", "\n", "\0")):
                continue  # never let an upstream split a header or the response
            forwarded.append((name, redact_header(name, value, secrets, label=label)))
        if method == "HEAD" or upstream.status in (204, 304):
            handler.send_response_only(upstream.status)
            if upstream.status not in (204, 304):
                handler.send_header("Content-Length", "0")
            for name, value in forwarded:
                handler.send_header(name, value)
            handler.end_headers()
            return upstream.status, 0

        # Read before writing a status: a body read failure is then still a clean
        # denial instead of a second status line on a half-written response.
        chunk = upstream.read(REDACTION_CAP + 1)
        size = 0
        if len(chunk) <= REDACTION_CAP:
            body = redact(chunk.decode("latin-1"), secrets, label=label).encode("latin-1")
            size = len(body)
            handler.send_response_only(upstream.status)
            handler.send_header("Content-Length", str(size))
            handler.send_header("X-Broker-Redaction", "full")
            for name, value in forwarded:
                handler.send_header(name, value)
            handler.end_headers()
            if body:
                handler.wfile.write(body)
            return upstream.status, size

        # Beyond the cap the body is streamed; redaction coverage is partial and
        # the response says so instead of pretending otherwise.
        handler.send_response_only(upstream.status)
        handler.send_header("X-Broker-Redaction", "partial")
        handler.send_header("Connection", "close")
        handler.close_connection = True
        for name, value in forwarded:
            handler.send_header(name, value)
        handler.end_headers()
        first = redact(chunk.decode("latin-1"), secrets, label=label).encode("latin-1")
        handler.wfile.write(first)
        size += len(first)
        while True:
            block = upstream.read(64 * 1024)
            if not block:
                break
            handler.wfile.write(block)
            size += len(block)
        return upstream.status, size

    def _deny(self, handler: BaseHTTPRequestHandler, denial: Denied, logical: str) -> int:
        text = f"broker: {denial.reason}\n".encode()
        text = redact(text.decode(), self.credentials.values(), label=logical).encode()
        try:
            handler.send_response_only(denial.status)
            handler.send_header("Content-Type", "text/plain; charset=utf-8")
            handler.send_header("Content-Length", str(len(text)))
            handler.end_headers()
            handler.wfile.write(text)
        except OSError:
            pass
        return denial.status


def make_handler(broker: Broker) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "broker"
        sys_version = ""

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - stdlib signature
            # The default access log writes the raw target; all logging goes
            # through Broker.handle, which logs path only and no headers.
            return

        def _dispatch(self) -> None:
            broker.handle(self)

        do_GET = _dispatch
        do_HEAD = _dispatch
        do_POST = _dispatch
        do_PUT = _dispatch
        do_PATCH = _dispatch
        do_DELETE = _dispatch
        do_OPTIONS = _dispatch
        do_CONNECT = _dispatch
        do_TRACE = _dispatch

    return Handler


def make_server(broker: Broker, host: str | None = None, port: int | None = None) -> ThreadingHTTPServer:
    handler = make_handler(broker)
    server = ThreadingHTTPServer((host or broker.config.listen_host, port or broker.config.listen_port), handler)
    server.daemon_threads = True
    return server


def main(config: Config, credentials: Credentials) -> int:
    server = make_server(Broker(config=config, credentials=credentials))
    LOG.info("broker listening on %s:%d hosts=%s", config.listen_host, config.listen_port, ",".join(sorted(config.hosts)))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()
    return 0
