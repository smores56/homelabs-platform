"""HTTP wrapper over :class:`data_plane.collectors.relay.Relay`.

Ingress is the untrusted edge: the body HMAC, idempotency key, schema, size,
rate, and cursor rules are all enforced by the library, not reimplemented here.
The service adds only a transport size cap and a bounded cursor source.

The cursor is read from ``?cursor=`` and falls back to ``X-Cursor``. The record
schema is loaded once from a JSON file at startup so a bad schema fails the
process rather than every later request.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from data_plane.collectors.relay import RecordSchema, Relay

from . import common

CURSOR_HEADER = "x-cursor"

_FIELD_TYPES: dict[str, type] = {
    "str": str,
    "int": int,
    "float": float,
    "bool": bool,
    "list": list,
    "dict": dict,
}


def load_schema(path: str | Path) -> RecordSchema:
    """Load the narrow collector schema from a local JSON file."""

    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("schema file must contain a JSON object")
    required = raw.get("required_fields")
    if not isinstance(required, list) or not required or not all(
        isinstance(item, str) and item.strip() for item in required
    ):
        raise ValueError("required_fields must be a non-empty list of strings")
    raw_types = raw.get("field_types", {})
    if not isinstance(raw_types, dict):
        raise ValueError("field_types must be a JSON object")
    field_types: dict[str, type] = {}
    for name, kind in raw_types.items():
        if not isinstance(name, str) or kind not in _FIELD_TYPES:
            raise ValueError(
                f"field_types entry {name!r} must be one of {sorted(_FIELD_TYPES)}"
            )
        field_types[name] = _FIELD_TYPES[kind]
    max_bytes = raw.get("max_bytes")
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 1:
        raise ValueError("max_bytes must be a positive integer")
    rate = raw.get("max_records_per_second")
    if isinstance(rate, bool) or not isinstance(rate, int) or rate < 1:
        raise ValueError("max_records_per_second must be a positive integer")
    return RecordSchema(
        required_fields=tuple(required),
        field_types=field_types,
        max_bytes=max_bytes,
        max_records_per_second=rate,
    )


@dataclass(frozen=True)
class RelayService:
    """Routes for one authenticated collector relay."""

    relay: Relay
    max_body_bytes: int = common.MAX_BODY_BYTES

    @property
    def routes(self) -> common.Routes:
        return {
            ("POST", "/ingest"): self.ingest,
            ("GET", "/healthz"): self.health,
        }

    def ingest(self, request: Any, query: common.Query) -> tuple[int, Any]:
        body = common.read_body(request, self.max_body_bytes)
        cursor = common.query_int(query, "cursor", default=None, minimum=1)
        if cursor is None:
            header = request.headers.get(CURSOR_HEADER, "").strip()
            if header:
                try:
                    cursor = int(header)
                except ValueError as exc:
                    raise common.HttpError(
                        400, "validation", "X-Cursor must be an integer"
                    ) from exc
                if cursor < 1:
                    raise common.HttpError(400, "validation", "X-Cursor must be positive")
        headers = {name.lower(): value for name, value in request.headers.items()}
        result = self.relay.submit(headers, body, cursor)
        return 202, result

    def health(self, request: Any, query: common.Query) -> tuple[int, Any]:
        return 200, {"status": "ok", "service": "relay"}
