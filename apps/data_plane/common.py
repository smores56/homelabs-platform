"""Shared HTTP plumbing for the thin data-plane services.

Each service is the same shape: stdlib ``http.server``, JSON in and out, a size
cap before any parsing, constant-time credentials, and one structured error
body. The libraries own the semantics; this module only transports their data
and maps their errors to status codes.

Errors never carry a stack trace or an exception message from an unexpected
failure: a 500 body is a fixed string so a secret or a filesystem path cannot
leak through the wire.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import secrets
import threading
import time
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Mapping
from urllib.parse import parse_qs, urlsplit

from data_plane.errors import (
    AuthenticationError,
    AuthorizationError,
    ConfirmationError,
    ConfirmationRequired,
    CursorError,
    DataPlaneError,
    EpochError,
    IdempotencyConflict,
    ManifestError,
    OutcomeUnknown,
    StaleError,
    ValidationError,
)

LOG = logging.getLogger("data_plane.http")

MAX_BODY_BYTES = 1 << 20
_MAX_QUERY_FIELDS = 32

# Request-token framing and freshness. A token is bound to one method, target,
# body, timestamp, and nonce, and is accepted once inside the freshness window.
TOKEN_TTL_SECONDS = 60
TOKEN_VERSION = "v1"
MAX_NONCE_CACHE = 4096
_NONCE_PATTERN = re.compile(r"\A[0-9a-f]{16,64}\Z")
_MAC_PATTERN = re.compile(r"\A[0-9a-f]{64}\Z")

# Transport hardening: one slow body must not hold a worker forever, and the
# server refuses connections beyond a fixed budget rather than spawning threads
# without bound.
BODY_READ_TIMEOUT_SECONDS = 10.0
REQUEST_TIMEOUT_SECONDS = 15.0
MAX_CONCURRENT_CONNECTIONS = 64
_MAX_REQUEST_BACKLOG = 16
_BUSY_RESPONSE = (
    b"HTTP/1.1 503 Service Unavailable\r\n"
    b"Content-Length: 0\r\nConnection: close\r\n\r\n"
)

Query = Mapping[str, list[str]]
Route = Callable[[BaseHTTPRequestHandler, Query], tuple[int, Any]]
Routes = Mapping[tuple[str, str], Route]

# Most specific first: several of these are ValidationError subclasses.
_STATUS_BY_ERROR: tuple[tuple[type[DataPlaneError], int, str], ...] = (
    (ConfirmationRequired, 403, "confirmation_required"),
    (ConfirmationError, 403, "confirmation_invalid"),
    (AuthenticationError, 401, "unauthorized"),
    (AuthorizationError, 403, "forbidden"),
    (IdempotencyConflict, 409, "idempotency_conflict"),
    (CursorError, 409, "cursor"),
    (EpochError, 409, "epoch"),
    (StaleError, 409, "stale"),
    (OutcomeUnknown, 409, "outcome_unknown"),
    (ManifestError, 400, "manifest"),
    (ValidationError, 400, "validation"),
)


class HttpError(Exception):
    """A transport-level refusal with an explicit status and error code."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def _key(secret: bytes | str) -> bytes:
    key = secret.encode("utf-8") if isinstance(secret, str) else bytes(secret)
    if not key:
        raise HttpError(500, "internal", "service secret is empty")
    return key


def error_status(exc: DataPlaneError) -> tuple[int, str]:
    """Map a library failure to ``(status, code)``."""

    for types, status, code in _STATUS_BY_ERROR:
        if isinstance(exc, types):
            return status, code
    return 400, "data_plane_error"


def send_json(request: BaseHTTPRequestHandler, status: int, payload: Any) -> None:
    """Write one JSON response and close the connection."""

    body = b"" if payload is None else json.dumps(
        payload, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    request.send_response(status)
    if body:
        request.send_header("Content-Type", "application/json")
    request.send_header("Content-Length", str(len(body)))
    request.send_header("Cache-Control", "no-store")
    request.send_header("Connection", "close")
    request.end_headers()
    if body:
        request.wfile.write(body)
    request.close_connection = True


def send_error(request: BaseHTTPRequestHandler, status: int, code: str, message: str) -> None:
    send_json(request, status, {"error": {"code": code, "message": message}})


def read_body(
    request: BaseHTTPRequestHandler, max_bytes: int = MAX_BODY_BYTES
) -> bytes:
    """Read the request body, refusing anything over ``max_bytes`` first."""

    if max_bytes < 1:
        raise HttpError(500, "internal", "body limit is not positive")
    raw_length = request.headers.get("Content-Length")
    if raw_length is None:
        raise HttpError(411, "length_required", "Content-Length is required")
    try:
        length = int(raw_length)
    except ValueError as exc:
        raise HttpError(400, "bad_request", "Content-Length is not an integer") from exc
    if length < 0:
        raise HttpError(400, "bad_request", "Content-Length is negative")
    if length > max_bytes:
        raise HttpError(
            413,
            "payload_too_large",
            f"request body exceeds the {max_bytes}-byte limit",
        )
    connection = getattr(request, "connection", None)
    deadline = time.monotonic() + BODY_READ_TIMEOUT_SECONDS
    chunks: list[bytes] = []
    remaining = length
    while remaining > 0:
        if connection is not None:
            try:
                connection.settimeout(max(0.1, deadline - time.monotonic()))
            except OSError:
                pass
        try:
            chunk = request.rfile.read(min(remaining, 65536))
        except TimeoutError as exc:
            raise HttpError(408, "timeout", "request body read timed out") from exc
        except OSError as exc:
            raise HttpError(400, "bad_request", "request body could not be read") from exc
        if not chunk:
            raise HttpError(400, "bad_request", "request body ended early")
        chunks.append(chunk)
        remaining -= len(chunk)
        if time.monotonic() > deadline:
            raise HttpError(408, "timeout", "request body read timed out")
    body = b"".join(chunks)
    if len(body) != length:
        raise HttpError(400, "bad_request", "request body ended early")
    return body


def parse_json_object(body: bytes) -> dict[str, Any]:
    try:
        value = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HttpError(400, "bad_request", "body is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise HttpError(400, "validation", "body must be a JSON object")
    return value


def read_json_object(
    request: BaseHTTPRequestHandler, max_bytes: int = MAX_BODY_BYTES
) -> dict[str, Any]:
    return parse_json_object(read_body(request, max_bytes))


def field_str(body: Mapping[str, Any], name: str) -> str:
    value = body.get(name)
    if not isinstance(value, str) or not value.strip():
        raise HttpError(400, "validation", f"{name} must be a non-empty string")
    return value


def field_optional_str(body: Mapping[str, Any], name: str) -> str | None:
    value = body.get(name)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise HttpError(400, "validation", f"{name} must be a non-empty string")
    return value


def field_int(body: Mapping[str, Any], name: str) -> int:
    value = body.get(name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise HttpError(400, "validation", f"{name} must be an integer")
    return value


def field_optional_int(body: Mapping[str, Any], name: str) -> int | None:
    value = body.get(name)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise HttpError(400, "validation", f"{name} must be an integer")
    return value


def field_str_list(body: Mapping[str, Any], name: str) -> list[str]:
    value = body.get(name)
    if value is None:
        return []
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise HttpError(400, "validation", f"{name} must be a list of strings")
    return list(value)


def query_str(
    query: Query, name: str, *, required: bool = False, default: str | None = None
) -> str | None:
    values = query.get(name) or []
    if not values:
        if required:
            raise HttpError(400, "validation", f"query parameter {name!r} is required")
        return default
    value = values[-1]
    if not value.strip():
        raise HttpError(400, "validation", f"query parameter {name!r} is empty")
    return value


def query_int(
    query: Query,
    name: str,
    *,
    required: bool = False,
    default: int | None = None,
    minimum: int = 0,
    maximum: int | None = None,
) -> int | None:
    value = query_str(query, name, required=required, default=None)
    if value is None:
        return default
    try:
        parsed = int(value)
    except ValueError as exc:
        raise HttpError(400, "validation", f"query parameter {name!r} must be an integer") from exc
    if parsed < minimum:
        raise HttpError(
            400, "validation", f"query parameter {name!r} must be at least {minimum}"
        )
    if maximum is not None and parsed > maximum:
        raise HttpError(
            400, "validation", f"query parameter {name!r} must be at most {maximum}"
        )
    return parsed


def query_float(
    query: Query,
    name: str,
    *,
    default: float,
    minimum: float = 0.0,
    maximum: float | None = None,
) -> float:
    value = query_str(query, name, default=None)
    if value is None:
        return default
    try:
        parsed = float(value)
    except ValueError as exc:
        raise HttpError(400, "validation", f"query parameter {name!r} must be a number") from exc
    if parsed != parsed:  # NaN
        raise HttpError(400, "validation", f"query parameter {name!r} must be a number")
    if parsed < minimum:
        raise HttpError(
            400, "validation", f"query parameter {name!r} must be at least {minimum}"
        )
    if maximum is not None and parsed > maximum:
        raise HttpError(
            400, "validation", f"query parameter {name!r} must be at most {maximum}"
        )
    return parsed


class NonceCache:
    """A bounded, thread-safe store of recently seen request nonces."""

    def __init__(self, max_entries: int = MAX_NONCE_CACHE) -> None:
        if max_entries < 1:
            raise HttpError(500, "internal", "nonce cache size is not positive")
        self._max_entries = max_entries
        self._seen: OrderedDict[str, float] = OrderedDict()
        self._lock = threading.Lock()

    def _purge(self, now: float, ttl: float) -> None:
        while self._seen:
            nonce, seen_at = next(iter(self._seen.items()))
            if now - seen_at < ttl:
                return
            self._seen.pop(nonce, None)

    def check_and_add(self, nonce: str, now: float, ttl: float) -> bool:
        with self._lock:
            self._purge(now, ttl)
            if nonce in self._seen:
                return False
            self._seen[nonce] = now
            while len(self._seen) > self._max_entries:
                self._seen.popitem(last=False)
            return True


_NONCES = NonceCache()


def _token_mac(
    secret: bytes | str,
    method: str,
    target: str,
    body: bytes,
    timestamp: int,
    nonce: str,
) -> str:
    message = (
        f"{method.upper()}\n{target}\n{hashlib.sha256(body).hexdigest()}\n"
        f"{timestamp}\n{nonce}"
    ).encode("utf-8")
    return hmac.new(_key(secret), message, hashlib.sha256).hexdigest()


def request_token(
    secret: bytes | str,
    method: str,
    target: str,
    body: bytes = b"",
    *,
    timestamp: int | None = None,
    nonce: str | None = None,
) -> str:
    """A shared-key HMAC over method, target, body digest, timestamp, and nonce.

    Binding all five means a token cannot be replayed for another request or
    reused inside its freshness window.
    """

    stamp = int(time.time()) if timestamp is None else int(timestamp)
    token_nonce = secrets.token_hex(16) if nonce is None else nonce
    mac = _token_mac(secret, method, target, body, stamp, token_nonce)
    return f"{TOKEN_VERSION}:{stamp}:{token_nonce}:{mac}"


def _parse_request_token(presented: str) -> tuple[int, str, str]:
    parts = presented.split(":")
    if len(parts) != 4:
        raise HttpError(403, "forbidden", "request token is malformed")
    version, raw_timestamp, nonce, mac = parts
    if version != TOKEN_VERSION or not _NONCE_PATTERN.match(nonce):
        raise HttpError(403, "forbidden", "request token is malformed")
    if not _MAC_PATTERN.match(mac):
        raise HttpError(403, "forbidden", "request token is malformed")
    try:
        timestamp = int(raw_timestamp)
    except ValueError as exc:
        raise HttpError(403, "forbidden", "request token is malformed") from exc
    return timestamp, nonce, mac


def require_request_token(
    request: BaseHTTPRequestHandler,
    secret: bytes | str,
    header: str,
    body: bytes = b"",
    *,
    ttl: float = TOKEN_TTL_SECONDS,
    nonce_cache: NonceCache | None = None,
    now: float | None = None,
) -> None:
    presented = request.headers.get(header, "").strip()
    if not presented:
        raise HttpError(401, "unauthorized", "missing request token")
    timestamp, nonce, mac = _parse_request_token(presented)
    current = time.time() if now is None else float(now)
    if abs(current - timestamp) > ttl:
        raise HttpError(403, "forbidden", "request token is stale")
    cache = _NONCES if nonce_cache is None else nonce_cache
    if not cache.check_and_add(nonce, current, ttl):
        raise HttpError(403, "forbidden", "request token was already used")
    expected = _token_mac(secret, request.command, request.path, body, timestamp, nonce)
    if not hmac.compare_digest(mac, expected):
        raise HttpError(403, "forbidden", "request token does not match")


def require_bearer(
    request: BaseHTTPRequestHandler,
    secret: bytes | str,
    header: str = "authorization",
) -> None:
    """Constant-time shared bearer token check for services that want one."""

    value = request.headers.get(header, "").strip()
    scheme, _, presented = value.partition(" ")
    if scheme.lower() != "bearer" or not presented:
        raise HttpError(401, "unauthorized", "missing bearer token")
    if not hmac.compare_digest(presented.encode("utf-8", "ignore"), _key(secret)):
        raise HttpError(403, "forbidden", "bearer token does not match")


class JsonHandler(BaseHTTPRequestHandler):
    """Dispatch to a service's ``routes`` table and render JSON errors."""

    protocol_version = "HTTP/1.1"
    server_version = "data-plane/1"
    sys_version = ""
    # Bound the time a client may take to send its request line and headers.
    timeout = REQUEST_TIMEOUT_SECONDS
    service: Any = None

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        self._dispatch("POST")

    def log_message(self, format: str, *args: Any) -> None:
        LOG.debug("%s %s", self.address_string(), format % args)

    def _dispatch(self, method: str) -> None:
        try:
            target = urlsplit(self.path)
            try:
                query = parse_qs(
                    target.query,
                    keep_blank_values=True,
                    max_num_fields=_MAX_QUERY_FIELDS,
                )
            except ValueError as exc:
                raise HttpError(400, "bad_request", "query string is too large") from exc
            routes: Routes = self.service.routes
            route = routes.get((method, target.path))
            if route is None:
                raise HttpError(404, "not_found", f"no route for {method} {target.path}")
            status, payload = route(self, query)
            send_json(self, status, payload)
        except HttpError as exc:
            send_error(self, exc.status, exc.code, exc.message)
        except DataPlaneError as exc:
            status, code = error_status(exc)
            send_error(self, status, code, str(exc))
        except Exception:  # noqa: BLE001 - the wire must not carry internals
            LOG.exception("unhandled error in %s %s", method, self.path)
            send_error(self, 500, "internal", "internal server error")


def handler_for(service: Any) -> type[JsonHandler]:
    """Bind one service instance to a handler class for ``ThreadingHTTPServer``."""

    return type(f"{type(service).__name__}Handler", (JsonHandler,), {"service": service})


class BoundedThreadingHTTPServer(ThreadingHTTPServer):
    """A threading server with a hard cap on concurrently served connections."""

    daemon_threads = True
    request_queue_size = _MAX_REQUEST_BACKLOG
    max_connections = MAX_CONCURRENT_CONNECTIONS

    def __init__(
        self,
        server_address: tuple[str, int],
        handler: type[BaseHTTPRequestHandler],
        *,
        max_connections: int | None = None,
    ) -> None:
        limit = self.max_connections if max_connections is None else max_connections
        if limit < 1:
            raise ValueError("max_connections must be positive")
        self._connection_slots = threading.BoundedSemaphore(limit)
        super().__init__(server_address, handler)

    def process_request(self, request: Any, client_address: Any) -> None:
        if not self._connection_slots.acquire(blocking=False):
            try:
                request.sendall(_BUSY_RESPONSE)
            except OSError:
                pass
            finally:
                try:
                    request.close()
                except OSError:
                    pass
            return
        super().process_request(request, client_address)

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._connection_slots.release()


def build_server(
    name: str,
    handler: type[BaseHTTPRequestHandler],
    host: str = "127.0.0.1",
    port: int = 0,
    max_connections: int | None = None,
) -> BoundedThreadingHTTPServer:
    server = BoundedThreadingHTTPServer(
        (host, port), handler, max_connections=max_connections
    )
    server.daemon_threads = True
    server.service_name = name  # type: ignore[attr-defined]
    return server


def serve(
    name: str,
    handler: type[BaseHTTPRequestHandler],
    port: int = 0,
    host: str = "127.0.0.1",
) -> None:
    """Run one service until interrupted."""

    server = build_server(name, handler, host=host, port=port)
    bound_host, bound_port = server.server_address[:2]
    LOG.info("%s listening on http://%s:%s", name, bound_host, bound_port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
