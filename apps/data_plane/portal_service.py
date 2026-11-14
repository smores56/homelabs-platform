"""Read-only HTTP service over a generated recovery portal directory.

The generator writes ``index.html`` and ``index.json`` atomically. This service
serves exactly those two files plus a health endpoint. It has no credentials, no
mutation route, no filesystem traversal, and no outbound request: the portal is
safe to expose through the existing ingress tunnel because the page itself is
static and self-contained.

Cache headers are deliberately short and public for the page and the index;
``/healthz`` is ``no-store`` so a probe always observes the current state.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from email.utils import formatdate
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

LOG = logging.getLogger("data_plane.portal")

DEFAULT_CACHE_SECONDS = 60
_CONTENT_SECURITY_POLICY = (
    "default-src 'none'; style-src 'unsafe-inline'; "
    "base-uri 'none'; form-action 'none'"
)


@dataclass(frozen=True)
class PortalService:
    """One generated portal directory, served without authentication."""

    directory: str | Path
    cache_seconds: int = DEFAULT_CACHE_SECONDS

    def __post_init__(self) -> None:
        object.__setattr__(self, "directory", Path(self.directory))
        if (
            isinstance(self.cache_seconds, bool)
            or not isinstance(self.cache_seconds, int)
            or self.cache_seconds < 0
        ):
            raise ValueError("portal cache_seconds must be a non-negative integer")

    @property
    def index_html(self) -> Path:
        return self.directory / "index.html"

    @property
    def index_json(self) -> Path:
        return self.directory / "index.json"

    def handler(self) -> type["PortalHandler"]:
        return handler_for(self)


class PortalHandler(BaseHTTPRequestHandler):
    """GET/HEAD only. Every other method is refused as read-only."""

    protocol_version = "HTTP/1.1"
    server_version = "data-plane/1"
    sys_version = ""
    portal_service: Any = None

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        self._serve(send_body=True)

    def do_HEAD(self) -> None:  # noqa: N802 - http.server API
        self._serve(send_body=False)

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        self._method_not_allowed()

    def do_PUT(self) -> None:  # noqa: N802 - http.server API
        self._method_not_allowed()

    def do_PATCH(self) -> None:  # noqa: N802 - http.server API
        self._method_not_allowed()

    def do_DELETE(self) -> None:  # noqa: N802 - http.server API
        self._method_not_allowed()

    def do_OPTIONS(self) -> None:  # noqa: N802 - http.server API
        self._method_not_allowed()

    def log_message(self, format: str, *args: Any) -> None:
        LOG.debug("%s %s", self.address_string(), format % args)

    def _serve(self, *, send_body: bool) -> None:
        service: PortalService = self.portal_service
        target = urlsplit(self.path)
        if target.path in ("/", "/index.html"):
            self._serve_file(
                service.index_html,
                "text/html; charset=utf-8",
                send_body,
                {"Content-Security-Policy": _CONTENT_SECURITY_POLICY},
            )
            return
        if target.path == "/index.json":
            self._serve_file(
                service.index_json,
                "application/json; charset=utf-8",
                send_body,
            )
            return
        if target.path == "/healthz":
            self._health(send_body=send_body)
            return
        self._error(404, "not_found", f"no route for {self.command} {target.path}", send_body)

    def _serve_file(
        self,
        path: Path,
        content_type: str,
        send_body: bool,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        service: PortalService = self.portal_service
        try:
            data = path.read_bytes()
            modified = path.stat().st_mtime
        except OSError:
            # Never echo the path: the wire carries no filesystem detail.
            self._error(404, "not_found", "the portal has not been generated", send_body)
            return
        headers = {
            "Last-Modified": formatdate(modified, usegmt=True),
            **(extra_headers or {}),
        }
        self._send(
            200,
            data,
            content_type,
            f"public, max-age={service.cache_seconds}",
            headers,
            send_body,
        )

    def _health(self, *, send_body: bool) -> None:
        service: PortalService = self.portal_service
        try:
            if not service.index_html.is_file() or not service.index_json.is_file():
                raise OSError("portal files are absent")
            payload = json.loads(service.index_json.read_text(encoding="utf-8"))
            entries = payload.get("entries") if isinstance(payload, dict) else None
            if not isinstance(entries, list):
                raise ValueError("portal index has no entries list")
        except (OSError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
            body = {
                "status": "unavailable",
                "service": "portal",
                "entries": 0,
            }
            self._send_json(503, body, send_body)
            return
        body = {
            "status": "ok",
            "service": "portal",
            "entries": len(entries),
        }
        self._send_json(200, body, send_body)

    def _method_not_allowed(self) -> None:
        self._error(
            405,
            "method_not_allowed",
            "the recovery portal is read-only",
            send_body=True,
            extra_headers={"Allow": "GET, HEAD"},
        )

    def _send_json(self, status: int, payload: dict[str, Any], send_body: bool) -> None:
        body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8", "no-store", {}, send_body)

    def _error(
        self,
        status: int,
        code: str,
        message: str,
        send_body: bool,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        body = json.dumps(
            {"error": {"code": code, "message": message}},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        self._send(
            status,
            body,
            "application/json; charset=utf-8",
            "no-store",
            extra_headers or {},
            send_body,
        )

    def _send(
        self,
        status: int,
        body: bytes,
        content_type: str,
        cache_control: str,
        extra_headers: dict[str, str],
        send_body: bool,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache_control)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for name, value in extra_headers.items():
            self.send_header(name, value)
        self.send_header("Connection", "close")
        self.end_headers()
        if send_body and body:
            self.wfile.write(body)
        self.close_connection = True


def handler_for(service: PortalService) -> type[PortalHandler]:
    """Bind one service instance to a handler class for ``ThreadingHTTPServer``."""

    return type(
        f"{type(service).__name__}Handler",
        (PortalHandler,),
        {"portal_service": service},
    )
