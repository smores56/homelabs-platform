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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Mapping
from urllib.parse import parse_qs, urlsplit

from data_plane.errors import (
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

Query = Mapping[str, list[str]]
Route = Callable[[BaseHTTPRequestHandler, Query], tuple[int, Any]]
Routes = Mapping[tuple[str, str], Route]

# Most specific first: several of these are ValidationError subclasses.
_STATUS_BY_ERROR: tuple[tuple[type[DataPlaneError], int, str], ...] = (
    (ConfirmationRequired, 403, "confirmation_required"),
    (ConfirmationError, 403, "confirmation_invalid"),
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
    body = request.rfile.read(length)
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


def request_token(
    secret: bytes | str, method: str, target: str, body: bytes = b""
) -> str:
    """A shared-key HMAC over ``method\\ntarget\\nsha256(body)``.

    Binding the method, the full request target (path and query), and the body
    digest means a token for one request cannot be replayed for another.
    """

    message = (
        f"{method.upper()}\n{target}\n{hashlib.sha256(body).hexdigest()}".encode(
            "utf-8"
        )
    )
    return "sha256=" + hmac.new(_key(secret), message, hashlib.sha256).hexdigest()


def require_request_token(
    request: BaseHTTPRequestHandler,
    secret: bytes | str,
    header: str,
    body: bytes = b"",
) -> None:
    presented = request.headers.get(header, "").strip()
    if not presented:
        raise HttpError(401, "unauthorized", "missing request token")
    expected = request_token(secret, request.command, request.path, body)
    if not hmac.compare_digest(
        presented.encode("utf-8", "ignore"), expected.encode("ascii")
    ):
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


def build_server(
    name: str,
    handler: type[BaseHTTPRequestHandler],
    host: str = "127.0.0.1",
    port: int = 0,
) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), handler)
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
