"""The enqueue client against a fake nanobot gateway.

The fake speaks the researched contract: `GET /webui/bootstrap` with the
platform token returns a one-time connection token and the WebSocket URL; the
socket sends `ready` with a chat id; the client sends one typed `message`
envelope and expects `message_accepted`. The assertions pin those frames so a
gateway upgrade that changes them fails here rather than in production.
"""

from __future__ import annotations

import json
import socket
import threading
import unittest
from typing import Any

from webhook.gateway import GatewayClient, GatewayError
from webhook.ws import accept_for, build_frame, parse_frame

READY = {"event": "ready", "chat_id": "chat-1", "client_id": "webhook"}
ACCEPTED = {"event": "message_accepted", "chat_id": "chat-1", "turn_id": "delivery-1"}


class FakeGateway:
    """One loopback listener that answers bootstrap HTTP and one WS session."""

    def __init__(
        self,
        *,
        bootstrap_status: int = 200,
        ready: bool = True,
        reply: dict[str, Any] | None = None,
    ) -> None:
        self.bootstrap_status = bootstrap_status
        self.send_ready = ready
        self.reply = ACCEPTED if reply is None else reply
        self.bootstrap_headers: dict[str, str] = {}
        self.ws_query = ""
        self.frames: list[dict[str, Any]] = []
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(4)
        self.port = self._listener.getsockname()[1]
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def __enter__(self) -> FakeGateway:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        self._listener.close()
        self._thread.join(timeout=5)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def _serve(self) -> None:
        self._listener.settimeout(0.1)
        while not self._stop.is_set():
            try:
                connection, _ = self._listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            with connection:
                try:
                    self._handle(connection)
                except OSError:
                    continue

    def _handle(self, connection: socket.socket) -> None:
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = connection.recv(4096)
            if not chunk:
                return
            head += chunk
        lines = head.split(b"\r\n")
        request_line = lines[0].decode()
        headers = {}
        for line in lines[1:]:
            if not line:
                break
            name, _, value = line.decode().partition(":")
            headers[name.strip().lower()] = value.strip()
        target = request_line.split(" ")[1]
        if target.endswith("/webui/bootstrap"):
            self.bootstrap_headers = headers
            if self.bootstrap_status != 200:
                connection.sendall(b"HTTP/1.1 401 Unauthorized\r\nContent-Length: 0\r\n\r\n")
                return
            body = json.dumps({"token": "one-time", "ws_url": f"ws://127.0.0.1:{self.port}/"}).encode()
            connection.sendall(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                + f"Content-Length: {len(body)}\r\n\r\n".encode()
                + body
            )
            return

        self.ws_query = target.partition("?")[2]
        key = headers.get("sec-websocket-key", "")
        connection.sendall(
            b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
            b"Connection: Upgrade\r\n"
            + f"Sec-WebSocket-Accept: {accept_for(key)}\r\n\r\n".encode()
        )
        if not self.send_ready:
            return
        connection.sendall(build_frame(0x1, json.dumps(READY).encode(), mask=None))
        buffer = b""
        while True:
            parsed = parse_frame(buffer)
            if parsed is not None:
                _, opcode, payload, consumed = parsed
                buffer = buffer[consumed:]
                if opcode == 0x1:
                    self.frames.append(json.loads(payload))
                    connection.sendall(build_frame(0x1, json.dumps(self.reply).encode(), mask=None))
                    return
                if opcode == 0x8:
                    return
                continue
            chunk = connection.recv(4096)
            if not chunk:
                return
            buffer += chunk


def bootstrap_authorization(fake: FakeGateway) -> str:
    return fake.bootstrap_headers.get("authorization", "")


class EnqueueTests(unittest.TestCase):
    def test_enqueue_speaks_the_researched_protocol(self) -> None:
        with FakeGateway() as fake:
            GatewayClient(fake.url, "platform-token").enqueue("hello", "delivery-1")
            self.assertEqual(bootstrap_authorization(fake), "Bearer platform-token")
            self.assertIn("token=one-time", fake.ws_query)
            self.assertIn("client_id=webhook", fake.ws_query)
            self.assertEqual(
                fake.frames,
                [
                    {
                        "type": "message",
                        "chat_id": "chat-1",
                        "content": "hello",
                        "webui": True,
                        "turn_id": "delivery-1",
                    }
                ],
            )

    def test_enqueue_omits_turn_id_without_a_delivery(self) -> None:
        with FakeGateway() as fake:
            GatewayClient(fake.url, "platform-token").enqueue("hello")
            self.assertEqual(len(fake.frames), 1)
            self.assertNotIn("turn_id", fake.frames[0])

    def test_enqueue_raises_on_a_gateway_error_event(self) -> None:
        with FakeGateway(reply={"event": "error", "detail": "missing content"}) as fake:
            with self.assertRaises(GatewayError) as raised:
                GatewayClient(fake.url, "platform-token").enqueue("hello")
            self.assertIn("missing content", str(raised.exception))

    def test_enqueue_raises_when_bootstrap_is_rejected(self) -> None:
        with FakeGateway(bootstrap_status=401) as fake:
            with self.assertRaises(GatewayError):
                GatewayClient(fake.url, "platform-token").enqueue("hello")

    def test_enqueue_raises_when_the_gateway_never_says_ready(self) -> None:
        with FakeGateway(ready=False) as fake:
            with self.assertRaises(GatewayError):
                GatewayClient(fake.url, "platform-token", timeout=0.5).enqueue("hello")

    def test_a_non_http_base_url_is_refused(self) -> None:
        with self.assertRaises(GatewayError):
            GatewayClient("ftp://example.invalid", "platform-token").enqueue("hello")


class BaseUrlTests(unittest.TestCase):
    def test_a_path_on_the_base_url_is_not_a_bootstrap_prefix(self) -> None:
        with FakeGateway() as fake:
            GatewayClient(fake.url + "/gw", "platform-token").enqueue("hello")
            self.assertEqual(bootstrap_authorization(fake), "Bearer platform-token")


if __name__ == "__main__":
    unittest.main()
