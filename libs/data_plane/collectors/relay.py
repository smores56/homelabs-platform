"""Authenticated, schema-validated ingress for collector records.

Collectors are read-only and untrusted. The relay verifies the body signature,
enforces the schema, size, rate, idempotency, and cursor rules, and only then
hands the record to the sink. A retry returns the first result; a reused key
with another body is a conflict rather than a second write.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from collections import OrderedDict, deque
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from ..errors import (
    AuthorizationError,
    CursorError,
    IdempotencyConflict,
    ValidationError,
)

SIGNATURE_HEADER = "x-relay-signature"
IDEMPOTENCY_HEADER = "x-idempotency-key"
SOURCE_FIELD = "source_id"
TOMBSTONE_FIELD = "tombstone"

_RATE_WINDOW_SECONDS = 1.0
_SIGNATURE_PREFIX = "sha256="
_MAX_JSON_DEPTH = 8
MAX_TRACKED_KEYS = 1024


class RateLimitError(ValidationError):
    """A source exceeded the schema's records-per-second budget."""


@dataclass(frozen=True)
class RecordSchema:
    """The narrow record shape one collector is allowed to submit."""

    required_fields: tuple[str, ...]
    field_types: dict[str, type]
    max_bytes: int
    max_records_per_second: int

    def validate(self, record: Mapping[str, Any]) -> None:
        if not isinstance(record, dict):
            raise ValidationError("record must be a JSON object")
        allowed = set(self.required_fields) | set(self.field_types)
        unexpected = sorted(set(record) - allowed)
        if unexpected:
            raise ValidationError(
                "record has fields outside the schema: " + ", ".join(unexpected)
            )
        for name in self.required_fields:
            if name not in record:
                raise ValidationError(f"record is missing required field {name!r}")
        for name, expected in self.field_types.items():
            if name not in record:
                continue
            if not isinstance(record[name], expected):
                raise ValidationError(
                    f"field {name!r} must be {expected.__name__}, "
                    f"got {type(record[name]).__name__}"
                )


class Relay:
    """The only path from a collector process into a replica generation."""

    def __init__(
        self,
        secret: bytes | str,
        schema: RecordSchema,
        sink: Callable[[dict[str, Any]], Any],
        clock: Callable[[], float] = time.time,
        *,
        source_id: str,
    ) -> None:
        self._secret = secret.encode("utf-8") if isinstance(secret, str) else secret
        if not self._secret:
            raise ValidationError("relay secret is empty")
        if not isinstance(source_id, str) or not source_id.strip():
            raise ValidationError("relay source id is required")
        self._source_id = source_id.strip()
        self._schema = schema
        self._sink = sink
        self._clock = clock
        self._seen: OrderedDict[str, tuple[bytes, dict[str, Any]]] = OrderedDict()
        self._cursors: dict[str, int] = {}
        self._timestamps: dict[str, deque[float]] = {}

    def sign(self, body: bytes, idempotency_key: str) -> str:
        if not isinstance(body, bytes):
            raise ValidationError("body must be bytes")
        if not idempotency_key.strip():
            raise ValidationError("idempotency key is required")
        message = (
            self._source_id.encode("utf-8")
            + b"\x00"
            + idempotency_key.encode("utf-8")
            + b"\x00"
            + body
        )
        return hmac.new(self._secret, message, hashlib.sha256).hexdigest()

    def submit(
        self,
        headers: Mapping[str, str],
        body: bytes,
        cursor: int | None,
    ) -> dict[str, Any]:
        if not isinstance(headers, Mapping):
            raise ValidationError("headers must be a mapping")
        normalized = {str(name).lower(): str(value) for name, value in headers.items()}
        if not isinstance(body, bytes):
            raise ValidationError("body must be bytes")

        key = normalized.get(IDEMPOTENCY_HEADER, "").strip()
        if not key:
            raise ValidationError("idempotency key is required")

        if len(body) > self._schema.max_bytes:
            raise ValidationError(
                f"body is {len(body)} bytes; limit is {self._schema.max_bytes}"
            )

        signature = normalized.get(SIGNATURE_HEADER, "").strip()
        if not signature:
            raise AuthorizationError("missing relay signature")
        presented = signature.removeprefix(_SIGNATURE_PREFIX)
        if not hmac.compare_digest(
            presented.encode("utf-8"), self.sign(body, key).encode("ascii")
        ):
            raise AuthorizationError("invalid relay signature")

        seen = self._seen.get(key)
        if seen is not None:
            if seen[0] != body:
                raise IdempotencyConflict(
                    f"idempotency key {key!r} was already used with another body"
                )
            return dict(seen[1])

        record = self._decode(body)
        self._schema.validate(record)

        source = record.get(SOURCE_FIELD)
        if not isinstance(source, str) or not source.strip():
            raise ValidationError("record has no stable source id")
        if source != self._source_id:
            # The relay is bound to one source; a body cannot choose its own
            # rate-limit, cursor, or identity namespace.
            raise AuthorizationError(
                f"record source {source!r} is not this relay's source {self._source_id!r}"
            )
        if TOMBSTONE_FIELD not in record or not isinstance(
            record[TOMBSTONE_FIELD], bool
        ):
            raise ValidationError("record has no boolean tombstone flag")

        if cursor is not None:
            self._check_cursor(source, cursor)
        now = self._check_rate(source)

        self._sink(record)

        self._timestamps.setdefault(source, deque()).append(now)
        if cursor is not None:
            self._cursors[source] = cursor
        result = {
            "accepted": True,
            "source_id": source,
            "cursor": cursor,
            "tombstone": record[TOMBSTONE_FIELD],
            "idempotency_key": key,
        }
        self._seen[key] = (body, result)
        self._seen.move_to_end(key)
        while len(self._seen) > MAX_TRACKED_KEYS:
            self._seen.popitem(last=False)
        return dict(result)

    def _decode(self, body: bytes) -> dict[str, Any]:
        try:
            record = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValidationError("body is not valid UTF-8 JSON") from exc
        if not isinstance(record, dict):
            raise ValidationError("record must be a JSON object")
        self._check_depth(record)
        return record

    @staticmethod
    def _check_depth(value: Any, depth: int = 0) -> None:
        if depth > _MAX_JSON_DEPTH:
            raise ValidationError("record is nested too deeply")
        if isinstance(value, dict):
            for item in value.values():
                Relay._check_depth(item, depth + 1)
        elif isinstance(value, list):
            for item in value:
                Relay._check_depth(item, depth + 1)

    def _check_cursor(self, source: str, cursor: int) -> None:
        if isinstance(cursor, bool) or not isinstance(cursor, int):
            raise ValidationError("cursor must be an integer")
        last = self._cursors.get(source, 0)
        if cursor <= last:
            raise CursorError(
                f"cursor {cursor} is not beyond {last} for source {source!r}"
            )
        if cursor != last + 1:
            raise CursorError(
                f"cursor {cursor} skips a gap after {last} for source {source!r}"
            )

    def _check_rate(self, source: str) -> float:
        now = float(self._clock())
        timestamps = self._timestamps.setdefault(
            source, deque(maxlen=max(1, self._schema.max_records_per_second * 2))
        )
        while timestamps and now - timestamps[0] >= _RATE_WINDOW_SECONDS:
            timestamps.popleft()
        if len(timestamps) >= self._schema.max_records_per_second:
            raise RateLimitError(
                f"source {source!r} exceeded "
                f"{self._schema.max_records_per_second} records per second"
            )
        return now
