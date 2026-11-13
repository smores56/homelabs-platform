"""Alertmanager webhook to Cloudflare Email Service.

Alertmanager's own `email` receiver needs an SMTP server; this cluster has none,
and the host's alert path is already a single POST to Cloudflare's Email Service
API (`modules/nixos/alerting.nix` in nix-config, `cloudflare-email`). This shim
accepts Alertmanager's webhook payload and speaks that API, so cluster alerts and
host unit failures leave through the same sender and the same verified domain.

Deployed from source through a generated ConfigMap, so this module is its own
test surface (tests/observability/test_mailrelay.py).
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

DEFAULT_PORT = 8080
DEFAULT_API_BASE = "https://api.cloudflare.com/client/v4"
TOKEN_FILE = "/etc/mailrelay/token"
CLUSTER = "cluster"


def _labels(alert: dict[str, Any]) -> list[str]:
    labels = alert.get("labels") or {}
    return [f"{key}={value}" for key, value in sorted(labels.items())]


def summarize(payload: dict[str, Any]) -> tuple[str, str]:
    """Subject and plain-text body for one Alertmanager webhook batch.

    A batch is either firing, resolved or mixed; the subject names the counts and
    the first two alert names so a mail client's preview is enough to triage,
    and the body carries every alert in full.
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
    subject = f"[{CLUSTER}] {state}: {', '.join(names[:2])}"
    if len(names) > 2:
        subject += f" (+{len(names) - 2})"

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
    return subject, "\n".join(lines).rstrip() + "\n"


def send(subject: str, body: str, *, account_id: str, sender: str, recipient: str,
         token: str, api_base: str = DEFAULT_API_BASE) -> None:
    """POST one message. Raises on transport failure or `success != true`.

    Cloudflare answers HTTP 200 with `success: false` for a rejected sender or
    recipient, so the body is checked, not just the status line.
    """
    request = urllib.request.Request(
        f"{api_base}/accounts/{account_id}/email/sending/send",
        data=json.dumps({"to": recipient, "from": sender, "subject": subject, "text": body}).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as error:
        raise RuntimeError(f"cloudflare email api: HTTP {error.code}") from error
    if payload.get("success") is not True:
        raise RuntimeError(f"cloudflare email api: {payload!r}")


def read_token(path: str) -> str:
    """Read the API token file at use time, not at start.

    ESO rewrites the mounted file when the SOPS Secret changes; reading it per
    delivery is what makes a rotation take effect without a pod restart.
    """
    with open(path, encoding="utf-8") as handle:
        token = handle.read().strip()
    if not token:
        raise RuntimeError(f"{path} is empty")
    return token


def deliver(payload: dict[str, Any], config: dict[str, str]) -> None:
    subject, body = summarize(payload)
    send(
        subject,
        body,
        account_id=config["account_id"],
        sender=config["sender"],
        recipient=config["recipient"],
        token=read_token(config["token_file"]),
        api_base=config.get("api_base", DEFAULT_API_BASE),
    )


class Handler(BaseHTTPRequestHandler):
    """`GET /healthz` for probes, `POST /alert` for Alertmanager."""

    config: dict[str, str] = {}
    server_version = "mailrelay"

    def log_message(self, fmt: str, *args: Any) -> None:  # keep the log shape stable
        print(f"mailrelay: {fmt % args}", flush=True)

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
        if self.path != "/alert":
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
            deliver(payload, self.config)
        except Exception as error:  # noqa: BLE001 - any failure must reach Alertmanager
            # A non-2xx makes Alertmanager retry, which is what we want: the
            # alert is still in its notification log until this pod accepts it.
            self._respond(502, f"delivery failed: {error}")
            return
        self._respond(200, "sent")


def config_from_env() -> dict[str, str]:
    """Configuration from the environment; the token stays a file.

    Fail at start when the token is absent rather than on the first alert: a
    relay that accepts webhooks and cannot send them is worse than one that is
    plainly not ready.
    """
    token_file = os.environ.get("CF_API_TOKEN_FILE", TOKEN_FILE)
    read_token(token_file)
    return {
        "account_id": os.environ["CF_ACCOUNT_ID"],
        "sender": os.environ["MAIL_FROM"],
        "recipient": os.environ["MAIL_TO"],
        "api_base": os.environ.get("CF_API_BASE", DEFAULT_API_BASE),
        "token_file": token_file,
    }


def main() -> None:
    Handler.config = config_from_env()
    port = int(os.environ.get("PORT", DEFAULT_PORT))
    server = HTTPServer(("0.0.0.0", port), Handler)  # noqa: S104 - in-cluster listener
    print(f"mailrelay: listening on {port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
