"""Enqueue-only client for nanobot's gateway (websocket listener, 8765).

The researched contract, read from nanobot-ai 0.4.1:

1. `GET /webui/bootstrap` with `Authorization: Bearer <channels.websocket
   .tokenIssueSecret>` returns `{"token": <one-time>, "ws_url": ...}`.
2. The WebSocket handshake carries `?token=<one-time>&client_id=<id>`; the
   gateway answers `{"event":"ready","chat_id":...}`.
3. One `{"type":"message","chat_id":...,"content":...,"webui":true,
   "turn_id":...}` frame is accepted with `{"event":"message_accepted"}`.

The client sends nothing else: no settings, no chat creation, no attachment
upload. `webui: true` is what makes the gateway emit the acceptance event; the
connection is already authenticated with the webui-audience token.
"""

from __future__ import annotations

import http.client
import json
import time
from dataclasses import dataclass
from urllib.parse import urlencode, urlunsplit, urlsplit

from .ws import WebSocket, WebSocketError

BOOTSTRAP_PATH = "/webui/bootstrap"
BOOTSTRAP_MAX_BYTES = 64 * 1024
MAX_FRAMES = 32
CLIENT_ID = "webhook"


class GatewayError(Exception):
    """The gateway refused the enqueue or could not be reached."""


@dataclass(frozen=True)
class Bootstrap:
    token: str
    ws_url: str


class GatewayClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout: float = 10.0,
        client_id: str = CLIENT_ID,
    ) -> None:
        if not token:
            raise GatewayError("gateway token is empty")
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout
        self._client_id = client_id

    def enqueue(self, text: str, delivery: str | None = None) -> None:
        """Hand one message to the gateway and wait only for its acceptance."""
        parts = urlsplit(self._base_url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise GatewayError(f"unsupported gateway URL: {self._base_url!r}")
        bootstrap = self._bootstrap(parts)
        deadline = time.monotonic() + self._timeout
        try:
            socket = WebSocket.connect(self._ws_target(bootstrap, parts), self._timeout)
        except (OSError, WebSocketError) as exc:
            raise GatewayError(f"websocket connect failed ({type(exc).__name__})") from exc
        try:
            chat_id = self._await_event(socket, deadline, "ready", field="chat_id")
            frame = {
                "type": "message",
                "chat_id": chat_id,
                "content": text,
                "webui": True,
            }
            if delivery is not None:
                frame["turn_id"] = delivery
            socket.send_text(json.dumps(frame, ensure_ascii=False))
            self._await_event(socket, deadline, "message_accepted")
        except (OSError, TimeoutError, WebSocketError) as exc:
            raise GatewayError(f"enqueue failed ({type(exc).__name__})") from exc
        finally:
            socket.close()

    def _bootstrap(self, parts: urlsplit) -> Bootstrap:
        # The base URL is an origin: a path on it is not a prefix for the
        # gateway's own route.
        path = BOOTSTRAP_PATH
        factory = http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
        connection = factory(parts.hostname, parts.port, timeout=self._timeout)
        try:
            connection.request(
                "GET",
                path,
                headers={"Authorization": f"Bearer {self._token}", "Accept": "application/json"},
            )
            response = connection.getresponse()
            body = response.read(BOOTSTRAP_MAX_BYTES + 1)
        except (OSError, http.client.HTTPException) as exc:
            raise GatewayError(f"bootstrap request failed ({type(exc).__name__})") from exc
        finally:
            connection.close()
        if response.status != 200:
            raise GatewayError(f"bootstrap returned {response.status}")
        if len(body) > BOOTSTRAP_MAX_BYTES:
            raise GatewayError("bootstrap response too large")
        try:
            payload = json.loads(body)
        except json.JSONDecodeError as exc:
            raise GatewayError("bootstrap response is not JSON") from exc
        if not isinstance(payload, dict):
            raise GatewayError("bootstrap response is not an object")
        token = payload.get("token")
        if not isinstance(token, str) or not token:
            raise GatewayError("bootstrap response has no token")
        ws_url = payload.get("ws_url")
        if not isinstance(ws_url, str) or not ws_url:
            ws_url = urlunsplit(("ws" if parts.scheme == "http" else "wss", parts.netloc, parts.path or "/", "", ""))
        return Bootstrap(token, ws_url)

    def _ws_target(self, bootstrap: Bootstrap, parts: urlsplit) -> str:
        target = urlsplit(bootstrap.ws_url)
        if target.scheme not in ("ws", "wss") or not target.hostname:
            raise GatewayError(f"unsupported bootstrap ws_url: {bootstrap.ws_url!r}")
        query = urlencode({"token": bootstrap.token, "client_id": self._client_id})
        return urlunsplit((target.scheme, target.netloc, target.path or "/", query, ""))

    def _await_event(
        self, socket: WebSocket, deadline: float, wanted: str, *, field: str | None = None
    ) -> str:
        for _ in range(MAX_FRAMES):
            try:
                raw = socket.recv_text(deadline)
            except (OSError, TimeoutError, WebSocketError) as exc:
                raise GatewayError(f"gateway read failed ({type(exc).__name__})") from exc
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            kind = event.get("event")
            if kind == "error":
                detail = event.get("detail") or event.get("reason") or "error"
                raise GatewayError(f"gateway rejected the message: {detail}")
            if kind != wanted:
                continue
            if field is None:
                return ""
            value = event.get(field)
            if not isinstance(value, str) or not value:
                raise GatewayError(f"gateway {wanted} event has no {field}")
            return value
        raise GatewayError(f"gateway never sent {wanted}")
