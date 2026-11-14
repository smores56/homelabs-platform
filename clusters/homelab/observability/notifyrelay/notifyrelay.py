"""Alertmanager and Flux webhooks to the ntfy notification bus.

Alertmanager's own `email` receiver needs an SMTP server; this cluster has none,
and the host's alert path is a single POST to ntfy (`modules/nixos/alerting.nix`
in nix-config, `ntfy-publish@`). This shim accepts Alertmanager's webhook payload
and Flux's generic-provider payload and turns each into one ntfy publish, so
cluster alerts and reconcile failures arrive on the same bus as host unit
failures.

It posts to the in-cluster Service, not the public hostname: it has no
`allow_private` rule to honour, and cluster alerts must not depend on the tunnel
they may be reporting on.

Deployed from source through a generated ConfigMap, so this module is its own
test surface (tests/observability/test_notifyrelay.py).
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

DEFAULT_PORT = 8080
DEFAULT_BASE_URL = "http://ntfy.notify.svc.cluster.local:8080"
DEFAULT_TOPIC = "alerts"
TOKEN_FILE = "/etc/notifyrelay/token"
CLUSTER = "cluster"
# ntfy's own `message-size-limit` is raised to 16k in the bus's server.yml; a
# batch larger than that is truncated rather than rejected, because a
# notification that names the first ten alerts is worth more than a 413.
MAX_BODY_BYTES = 16000
TRUNCATED = "\n[truncated: the batch is larger than one notification]\n"


def _labels(alert: dict[str, Any]) -> list[str]:
    labels = alert.get("labels") or {}
    return [f"{key}={value}" for key, value in sorted(labels.items())]


def summarize_alertmanager(payload: dict[str, Any]) -> tuple[str, str, str]:
    """Title, body and ntfy priority for one Alertmanager webhook batch.

    A batch is either firing, resolved or mixed; the title names the counts and
    the first two alert names so the notification's collapsed form is enough to
    triage, and the body carries every alert in full. Priority is `high` while
    anything is firing and `default` once everything has resolved, so a recovery
    does not buzz like a failure.
    """
    alerts = payload.get("alerts") or []
    firing = [alert for alert in alerts if alert.get("status") != "resolved"]
    resolved = [alert for alert in alerts if alert.get("status") == "resolved"]

    names = []
    for alert in firing + resolved:
        name = (alert.get("labels") or {}).get("alertname", "unknown")
        if name not in names:
            names.append(name)

    parts = []
    if firing:
        parts.append(f"{len(firing)} firing")
    if resolved:
        parts.append(f"{len(resolved)} resolved")
    state = ", ".join(parts) or "no alerts"
    title = f"[{CLUSTER}] {state}: {', '.join(names[:2])}"
    if len(names) > 2:
        title += f" (+{len(names) - 2})"

    lines = [f"cluster: {CLUSTER}", f"status: {state}", f"alerts: {len(alerts)}", ""]
    for alert in alerts:
        labels = alert.get("labels") or {}
        annotations = alert.get("annotations") or {}
        lines.append(f"* {alert.get('status', 'firing')}: {labels.get('alertname', 'unknown')}")
        lines.append(f"  {', '.join(_labels(alert))}")
        for key in ("summary", "description"):
            if annotations.get(key):
                lines.append(f"  {key}: {annotations[key]}")
        if alert.get("generatorURL"):
            lines.append(f"  source: {alert['generatorURL']}")
        lines.append("")
    if not alerts:
        lines.append("Alertmanager sent a batch with no alerts.")
    return title, "\n".join(lines).rstrip() + "\n", "high" if firing else "default"


def summarize_flux(payload: dict[str, Any]) -> tuple[str, str, str]:
    """Title, body and ntfy priority for one Flux generic-provider event.

    The generic provider sends `involvedObject`, `severity`, `reason`, `message`
    and `metadata.revision`; the object is what a reader needs first, so it is in
    the title and the message is the body.
    """
    obj = payload.get("involvedObject") or {}
    kind = obj.get("kind") or "object"
    name = obj.get("name") or "unknown"
    namespace = obj.get("namespace")
    target = f"{namespace}/{name}" if namespace else name
    severity = str(payload.get("severity") or "info").lower()
    reason = payload.get("reason") or ""
    message = str(payload.get("message") or "").strip()

    title = f"[{CLUSTER}] {kind} {target}"
    if reason:
        title += f": {reason}"

    lines = [f"cluster: {CLUSTER}", f"severity: {severity}", f"object: {kind} {target}"]
    if payload.get("reportingController"):
        lines.append(f"controller: {payload['reportingController']}")
    if payload.get("timestamp"):
        lines.append(f"time: {payload['timestamp']}")
    metadata = payload.get("metadata") or {}
    if metadata.get("revision"):
        lines.append(f"revision: {metadata['revision']}")
    lines += ["", message or "(no message)"]
    # A failed reconcile is the case this exists for; anything else is a note.
    priority = "high" if severity == "error" else "default"
    return title, "\n".join(lines).rstrip() + "\n", priority


def fit(body: str) -> str:
    """Truncate a body to the bus's message limit, keeping the head.

    The head is the part that names the alerts; the tail is repetition. The
    marker is ASCII so the byte count is the character count.
    """
    encoded = body.encode("utf-8")
    if len(encoded) <= MAX_BODY_BYTES:
        return body
    keep = MAX_BODY_BYTES - len(TRUNCATED.encode("utf-8"))
    return encoded[:keep].decode("utf-8", "ignore") + TRUNCATED


def publish(title: str, body: str, priority: str, *, base_url: str, topic: str,
            token: str, timeout: int = 30) -> None:
    """POST one message to ntfy. Raises on transport failure or a non-2xx.

    The path is the topic and the message is the plain-text body; `title` and
    `priority` are query parameters, which is the same shape the broker's
    `notify` host forwards.
    """
    query = urllib.parse.urlencode({"title": title, "priority": priority})
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/{topic}?{query}",
        data=body.encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "text/plain; charset=utf-8",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response.read()
    except urllib.error.HTTPError as error:
        raise RuntimeError(f"ntfy publish: HTTP {error.code}") from error


def read_token(path: str) -> str:
    """Read the access token file at use time, not at start.

    ESO rewrites the mounted file when the SOPS Secret changes; reading it per
    delivery is what makes a rotation take effect without a pod restart.
    """
    with open(path, encoding="utf-8") as handle:
        token = handle.read().strip()
    if not token:
        raise RuntimeError(f"{path} is empty")
    return token


def deliver(kind: str, payload: dict[str, Any], config: dict[str, str]) -> None:
    if kind == "flux":
        title, body, priority = summarize_flux(payload)
    else:
        title, body, priority = summarize_alertmanager(payload)
    publish(
        title,
        fit(body),
        priority,
        base_url=config["base_url"],
        topic=config["topic"],
        token=read_token(config["token_file"]),
    )


class Handler(BaseHTTPRequestHandler):
    """`GET /healthz` for probes, `POST /alert` and `POST /flux` for senders."""

    config: dict[str, str] = {}
    server_version = "notifyrelay"
    routes = {"/alert": "alert", "/flux": "flux"}

    def log_message(self, fmt: str, *args: Any) -> None:  # keep the log shape stable
        print(f"notifyrelay: {fmt % args}", flush=True)

    def _respond(self, status: int, text: str) -> None:
        body = (text + "\n").encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if self.path == "/healthz":
            self._respond(200, "ok")
        else:
            self._respond(404, "not found")

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        kind = self.routes.get(self.path)
        if kind is None:
            self._respond(404, "not found")
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            payload = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            self._respond(400, "invalid json")
            return
        try:
            deliver(kind, payload, self.config)
        except Exception as error:  # noqa: BLE001 - any failure must reach the sender
            # A non-2xx makes Alertmanager retry from its notification log and
            # Flux retry the event, which is what we want: the alert is not lost
            # until this pod accepts it.
            self._respond(502, f"delivery failed: {error}")
            return
        self._respond(200, "sent")


def config_from_env() -> dict[str, str]:
    """Configuration from the environment; the token stays a file.

    Fail at start when the token is absent rather than on the first alert: a
    relay that accepts webhooks and cannot deliver them is worse than one that is
    plainly not ready.
    """
    token_file = os.environ.get("NTFY_TOKEN_FILE", TOKEN_FILE)
    read_token(token_file)
    return {
        "base_url": os.environ.get("NTFY_URL", DEFAULT_BASE_URL),
        "topic": os.environ.get("NTFY_TOPIC", DEFAULT_TOPIC),
        "token_file": token_file,
    }


def main() -> None:
    Handler.config = config_from_env()
    port = int(os.environ.get("PORT", DEFAULT_PORT))
    server = HTTPServer(("0.0.0.0", port), Handler)  # noqa: S104 - in-cluster listener
    print(f"notifyrelay: listening on {port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
