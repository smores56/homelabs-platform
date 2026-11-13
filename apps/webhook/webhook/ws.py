"""Minimal RFC 6455 client for the gateway's enqueue socket.

The receiver needs exactly one outbound WebSocket: a masked text frame after
the handshake. There is no client dependency in the image, so the framing lives
here and the tests pin it against the RFC's examples. Pings are answered, close
frames end the session, and a server frame larger than the cap is refused.
"""

from __future__ import annotations

import base64
import hashlib
import os
import socket
import ssl
import time
from collections.abc import Callable
from urllib.parse import urlsplit

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
OPCODE_CONTINUATION = 0x0
OPCODE_TEXT = 0x1
OPCODE_BINARY = 0x2
OPCODE_CLOSE = 0x8
OPCODE_PING = 0x9
OPCODE_PONG = 0xA
MAX_FRAME_BYTES = 8 * 1024 * 1024
MAX_HANDSHAKE_BYTES = 16 * 1024


class WebSocketError(Exception):
    """The peer spoke something other than the WebSocket protocol."""


class WebSocketClosed(WebSocketError):
    """The peer closed the connection."""


def accept_for(key: str) -> str:
    return base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()


def build_frame(opcode: int, payload: bytes, mask: bytes | None) -> bytes:
    """Encode one FIN frame; `mask` is a 4-byte key or None for a server frame."""
    header = bytearray([0x80 | opcode])
    length = len(payload)
    flag = 0x80 if mask is not None else 0x00
    if length < 126:
        header.append(flag | length)
    elif length < 1 << 16:
        header.append(flag | 126)
        header.extend(length.to_bytes(2, "big"))
    else:
        header.append(flag | 127)
        header.extend(length.to_bytes(8, "big"))
    if mask is None:
        return bytes(header) + payload
    if len(mask) != 4:
        raise ValueError("mask must be 4 bytes")
    masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
    return bytes(header) + mask + masked


def parse_frame(buffer: bytes | bytearray) -> tuple[bool, int, bytes, int] | None:
    """Parse one frame from `buffer`; return (fin, opcode, payload, consumed)."""
    if len(buffer) < 2:
        return None
    first, second = buffer[0], buffer[1]
    fin = bool(first & 0x80)
    opcode = first & 0x0F
    length = second & 0x7F
    offset = 2
    if length == 126:
        if len(buffer) < offset + 2:
            return None
        length = int.from_bytes(buffer[offset : offset + 2], "big")
        offset += 2
    elif length == 127:
        if len(buffer) < offset + 8:
            return None
        length = int.from_bytes(buffer[offset : offset + 8], "big")
        offset += 8
    mask = None
    if second & 0x80:
        if len(buffer) < offset + 4:
            return None
        mask = bytes(buffer[offset : offset + 4])
        offset += 4
    if len(buffer) < offset + length:
        return None
    payload = bytes(buffer[offset : offset + length])
    if mask is not None:
        payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
    return fin, opcode, payload, offset + length


class WebSocket:
    def __init__(self, sock: socket.socket, buffer: bytes = b"") -> None:
        self._sock = sock
        self._buffer = bytearray(buffer)

    @classmethod
    def connect(cls, url: str, timeout: float) -> WebSocket:
        parts = urlsplit(url)
        if parts.scheme not in ("ws", "wss") or not parts.hostname:
            raise WebSocketError(f"unsupported WebSocket URL: {url!r}")
        port = parts.port or (443 if parts.scheme == "wss" else 80)
        path = parts.path or "/"
        if parts.query:
            path = f"{path}?{parts.query}"
        sock = socket.create_connection((parts.hostname, port), timeout=timeout)
        if parts.scheme == "wss":
            sock = ssl.create_default_context().wrap_socket(sock, server_hostname=parts.hostname)
        key = base64.b64encode(os.urandom(16)).decode()
        host_header = parts.hostname if parts.port in (None, 80, 443) else f"{parts.hostname}:{port}"
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host_header}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        sock.sendall(request.encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = sock.recv(4096)
            if not chunk:
                raise WebSocketError("gateway closed during the handshake")
            head += chunk
            if len(head) > MAX_HANDSHAKE_BYTES:
                raise WebSocketError("handshake response too large")
        head, _, rest = head.partition(b"\r\n\r\n")
        lines = head.decode("latin-1").split("\r\n")
        if not lines[0].startswith("HTTP/1.1 101"):
            raise WebSocketError(f"handshake refused: {lines[0]}")
        headers = {}
        for line in lines[1:]:
            name, _, value = line.partition(":")
            headers[name.strip().lower()] = value.strip()
        if headers.get("sec-websocket-accept") != accept_for(key):
            raise WebSocketError("handshake accepted the wrong key")
        return cls(sock, rest)

    def send_text(self, text: str) -> None:
        self._sock.sendall(build_frame(OPCODE_TEXT, text.encode(), mask=os.urandom(4)))

    def recv_text(self, deadline: float) -> str:
        """Return the next complete text message, answering control frames."""
        message = bytearray()
        fragments = False
        while True:
            fin, opcode, payload, consumed = self._read_frame(deadline)
            del self._buffer[:consumed]
            if opcode == OPCODE_PING:
                self._sock.sendall(build_frame(OPCODE_PONG, payload, mask=os.urandom(4)))
                continue
            if opcode == OPCODE_PONG:
                continue
            if opcode == OPCODE_CLOSE:
                self._sock.sendall(build_frame(OPCODE_CLOSE, payload[:2], mask=os.urandom(4)))
                raise WebSocketClosed("gateway closed the connection")
            if opcode in (OPCODE_TEXT, OPCODE_BINARY):
                if fragments:
                    raise WebSocketError("new message before the previous one finished")
                fragments = True
                message.extend(payload)
            elif opcode == OPCODE_CONTINUATION:
                if not fragments:
                    raise WebSocketError("continuation without a message")
                message.extend(payload)
            else:
                raise WebSocketError(f"unexpected opcode {opcode}")
            if fin:
                return message.decode("utf-8", "replace")

    def _read_frame(self, deadline: float) -> tuple[bool, int, bytes, int]:
        while True:
            parsed = parse_frame(self._buffer)
            if parsed is not None:
                return parsed
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("gateway did not answer before the deadline")
            self._sock.settimeout(remaining)
            chunk = self._sock.recv(65536)
            if not chunk:
                raise WebSocketClosed("gateway closed the connection")
            self._buffer.extend(chunk)
            if len(self._buffer) > MAX_FRAME_BYTES:
                raise WebSocketError("server frame exceeds the cap")

    def close(self) -> None:
        try:
            self._sock.sendall(build_frame(OPCODE_CLOSE, b"", mask=os.urandom(4)))
        except OSError:
            pass
        try:
            self._sock.close()
        except OSError:
            pass


def read_exactly(sock: socket.socket, count: int) -> bytes:
    """Read `count` bytes or raise; kept for callers that need raw framing."""
    chunks = bytearray()
    while len(chunks) < count:
        chunk = sock.recv(count - len(chunks))
        if not chunk:
            raise WebSocketClosed("gateway closed the connection")
        chunks.extend(chunk)
    return bytes(chunks)


Connect = Callable[[str, float], WebSocket]
