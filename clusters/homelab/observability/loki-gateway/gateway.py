#!/usr/bin/env python3
"""Loki write/read gateway for the `data` tenant.

Loki runs with `auth_enabled: true`. Vector writes the `raw` tenant directly; the
`data` tenant is only reachable here, where the caller must present the bearer
token that ESO minted and the gateway replaces whatever tenant header arrived
with `X-Scope-OrgID: data`. The token, not the header, decides the tenant, so a
compromised agent that reaches the gateway still cannot read `raw`.

Only read paths are proxied. Push, delete and flush are denied, so the gateway
cannot be used to inject log lines into the data tenant.

The broker (docs/observability.md) holds the token and calls this URL on behalf
of nanobot; the agent never sees it.
"""

from __future__ import annotations

import hmac
import http.client
import os
import re
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Mapping

TENANT = "data"
DEFAULT_UPSTREAM = "http://loki.observability.svc.cluster.local:3100"
DEFAULT_PORT = 3100
MAX_BODY_BYTES = 1 << 20

# Loki's read API. Full-match, so path confusion (`/loki/api/v1/push`,
# `/loki/api/v1/query_range/../../push`) cannot slip through the prefix.
READ_PATHS = (
    re.compile(r"/loki/api/v1/query"),
    re.compile(r"/loki/api/v1/query_range"),
    re.compile(r"/loki/api/v1/labels"),
    re.compile(r"/loki/api/v1/label/[A-Za-z0-9_.-]+/values"),
    re.compile(r"/loki/api/v1/series"),
    re.compile(r"/loki/api/v1/index/stats"),
    re.compile(r"/loki/api/v1/patterns"),
    re.compile(r"/loki/api/v1/format_query"),
)
READ_METHODS = ("GET", "POST")


def bearer_token(headers: Mapping[str, str]) -> str:
    """Return the bearer credential, or "" when the header is absent/malformed."""
    value = headers.get("Authorization", "")
    prefix = "Bearer "
    if not value.startswith(prefix):
        return ""
    return value[len(prefix) :].strip()


def is_authorized(headers: Mapping[str, str], token: str) -> bool:
    """Constant-time comparison; a missing server token denies everything."""
    presented = bearer_token(headers)
    if not token or not presented:
        return False
    return hmac.compare_digest(presented.encode("utf-8"), token.encode("utf-8"))


def is_allowed(method: str, path: str) -> bool:
    if method not in READ_METHODS:
        return False
    base = path.split("?", 1)[0]
    return any(pattern.fullmatch(base) for pattern in READ_PATHS)


def upstream_headers(client_headers: Mapping[str, str]) -> dict[str, str]:
    """Headers sent to Loki: no client identity, no client tenant, forced scope."""
    headers = {"Accept": "application/json", "X-Scope-OrgID": TENANT}
    if "Content-Type" in client_headers:
        headers["Content-Type"] = client_headers["Content-Type"]
    return headers


def upstream_target(url: str) -> tuple[str, int, str]:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "http":
        raise ValueError("only plain-http in-cluster upstreams are supported")
    return parsed.hostname or "", parsed.port or 80, parsed.path.rstrip("/")


class Handler(BaseHTTPRequestHandler):
    token: str = ""
    upstream: str = DEFAULT_UPSTREAM

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._handle("GET")

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._handle("POST")

    def _handle(self, method: str) -> None:
        if self.path == "/healthz":
            self._respond(200, "text/plain; charset=utf-8", "ok\n")
            return
        if not is_authorized(self.headers, self.token):
            self._respond(
                401, "text/plain; charset=utf-8", "unauthorized\n", {"WWW-Authenticate": "Bearer"}
            )
            return
        if not is_allowed(method, self.path):
            self._respond(403, "text/plain; charset=utf-8", "read-only gateway\n")
            return
        self._proxy(method)

    def _proxy(self, method: str) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY_BYTES:
            self._respond(413, "text/plain; charset=utf-8", "body too large\n")
            return
        body = self.rfile.read(length) if length else None
        host, port, prefix = upstream_target(self.upstream)
        connection = http.client.HTTPConnection(host, port, timeout=30)
        try:
            connection.request(
                method, prefix + self.path, body=body, headers=upstream_headers(self.headers)
            )
            response = connection.getresponse()
            payload = response.read()
            self._respond(
                response.status,
                response.getheader("Content-Type", "application/json"),
                payload,
            )
        except OSError:
            self._respond(502, "text/plain; charset=utf-8", "upstream unavailable\n")
        finally:
            connection.close()

    def _respond(
        self,
        status: int,
        content_type: str,
        body: str | bytes,
        extra: Mapping[str, str] | None = None,
    ) -> None:
        payload = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, fmt: str, *args: object) -> None:
        pass


def main() -> None:
    token = os.environ.get("LOKI_GATEWAY_TOKEN", "")
    if not token:
        # Fail closed: a gateway without a token would either open the data
        # tenant or accept everything. Refuse to start instead.
        print("loki-gateway: LOKI_GATEWAY_TOKEN is unset or empty", file=sys.stderr)
        sys.exit(2)
    Handler.token = token
    Handler.upstream = os.environ.get("LOKI_URL", DEFAULT_UPSTREAM)
    port = int(os.environ.get("LISTEN_PORT", DEFAULT_PORT))
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
