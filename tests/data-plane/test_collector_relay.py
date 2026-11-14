"""Collector relay authentication, schema, rate, idempotency, and cursor tests."""

from __future__ import annotations

import json
import unittest

from data_plane.collectors.relay import (
    IDEMPOTENCY_HEADER,
    SIGNATURE_HEADER,
    RateLimitError,
    RecordSchema,
    Relay,
)
from data_plane.errors import (
    AuthorizationError,
    CursorError,
    IdempotencyConflict,
    ValidationError,
)

SECRET = b"relay-test-secret"


def record(**overrides):
    values = dict(source_id="mail", object_id="message-1", tombstone=False)
    values.update(overrides)
    return values


def schema(**overrides):
    values = dict(
        required_fields=("source_id", "object_id", "tombstone"),
        field_types={"source_id": str, "object_id": str, "tombstone": bool},
        max_bytes=1024,
        max_records_per_second=5,
    )
    values.update(overrides)
    return RecordSchema(**values)


class RelayTests(unittest.TestCase):
    def setUp(self):
        self.now = 1_000.0
        self.sunk = []
        self.relay = Relay(
            secret=SECRET,
            schema=schema(),
            sink=self.sunk.append,
            clock=lambda: self.now,
        )

    def headers(self, body, key, *, signature=None):
        signed = self.relay.sign(body, key) if signature is None else signature
        return {
            IDEMPOTENCY_HEADER: key,
            SIGNATURE_HEADER: f"sha256={signed}",
        }

    def submit(self, payload=None, *, key="key-1", cursor=1, headers=None):
        body = json.dumps(record() if payload is None else payload).encode()
        return self.relay.submit(
            headers if headers is not None else self.headers(body, key), body, cursor
        )

    def test_missing_signature_rejected(self):
        body = json.dumps(record()).encode()
        with self.assertRaises(AuthorizationError):
            self.relay.submit({IDEMPOTENCY_HEADER: "key-1"}, body, 1)
        self.assertEqual(self.sunk, [])

    def test_invalid_signature_rejected(self):
        body = json.dumps(record()).encode()
        headers = self.headers(body, "key-1", signature="0" * 64)
        with self.assertRaises(AuthorizationError):
            self.relay.submit(headers, body, 1)
        self.assertEqual(self.sunk, [])

    def test_non_ascii_signature_rejected(self):
        body = json.dumps(record()).encode()
        headers = self.headers(body, "key-1", signature="\u2603" * 32)
        with self.assertRaises(AuthorizationError):
            self.relay.submit(headers, body, 1)

    def test_missing_idempotency_key_rejected(self):
        body = json.dumps(record()).encode()
        headers = {SIGNATURE_HEADER: self.relay.sign(body, "key-1")}
        with self.assertRaises(ValidationError):
            self.relay.submit(headers, body, 1)

    def test_oversized_body_rejected(self):
        relay = Relay(
            secret=SECRET,
            schema=schema(max_bytes=16),
            sink=self.sunk.append,
            clock=lambda: self.now,
        )
        body = json.dumps(record()).encode()
        with self.assertRaises(ValidationError):
            relay.submit(self.headers(body, "key-1"), body, 1)

    def test_invalid_json_rejected(self):
        body = b"not-json"
        headers = self.headers(body, "key-1")
        with self.assertRaises(ValidationError):
            self.relay.submit(headers, body, 1)

    def test_missing_required_field_rejected(self):
        with self.assertRaises(ValidationError):
            self.submit({"source_id": "mail", "tombstone": False})

    def test_wrong_field_type_rejected(self):
        with self.assertRaises(ValidationError):
            self.submit(record(tombstone="no"))

    def test_empty_source_id_rejected(self):
        with self.assertRaises(ValidationError):
            self.submit(record(source_id="   "))

    def test_missing_tombstone_flag_rejected(self):
        relay = Relay(
            secret=SECRET,
            schema=schema(
                required_fields=("source_id", "object_id"),
                field_types={"source_id": str, "object_id": str},
            ),
            sink=self.sunk.append,
            clock=lambda: self.now,
        )
        body = json.dumps({"source_id": "mail", "object_id": "message-1"}).encode()
        with self.assertRaises(ValidationError):
            relay.submit(self.headers(body, "key-1"), body, 1)

    def test_rate_limit_per_source(self):
        relay = Relay(
            secret=SECRET,
            schema=schema(max_records_per_second=2),
            sink=self.sunk.append,
            clock=lambda: self.now,
        )
        for cursor in (1, 2):
            body = json.dumps(record(object_id=f"message-{cursor}")).encode()
            relay.submit(self.headers(body, f"key-{cursor}"), body, cursor)
        body = json.dumps(record(object_id="message-3")).encode()
        with self.assertRaises(RateLimitError):
            relay.submit(self.headers(body, "key-3"), body, 3)
        self.assertEqual(len(self.sunk), 2)

        self.now += 1.0
        body = json.dumps(record(object_id="message-3")).encode()
        relay.submit(self.headers(body, "key-3"), body, 3)
        self.assertEqual(len(self.sunk), 3)

    def test_rate_limit_is_a_validation_error(self):
        self.assertTrue(issubclass(RateLimitError, ValidationError))

    def test_duplicate_returns_prior_result(self):
        first = self.submit()
        second = self.submit()
        self.assertEqual(first, second)
        self.assertEqual(len(self.sunk), 1)

    def test_reused_key_with_different_body_conflicts(self):
        self.submit()
        with self.assertRaises(IdempotencyConflict):
            self.submit(record(object_id="message-2"))
        self.assertEqual(len(self.sunk), 1)

    def test_cursor_gap_rejected(self):
        self.submit()
        with self.assertRaises(CursorError):
            self.submit(record(object_id="message-2"), key="key-2", cursor=3)
        with self.assertRaises(CursorError):
            self.submit(record(object_id="message-2"), key="key-2", cursor=1)
        self.submit(record(object_id="message-2"), key="key-2", cursor=2)
        self.assertEqual(len(self.sunk), 2)

    def test_successful_submit_calls_sink_once(self):
        result = self.submit(record(object_id="message-9"))
        self.assertEqual(self.sunk, [record(object_id="message-9")])
        self.assertEqual(result["source_id"], "mail")
        self.assertEqual(result["cursor"], 1)
        self.assertFalse(result["tombstone"])
        self.assertTrue(result["accepted"])

    def test_tombstone_record_accepted(self):
        result = self.submit(record(tombstone=True))
        self.assertTrue(result["tombstone"])


if __name__ == "__main__":
    unittest.main()
