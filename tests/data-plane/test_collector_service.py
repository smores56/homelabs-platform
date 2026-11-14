"""Collector fetch, normalization, schema, cursor, and idempotency tests.

The fetcher is a dict in every test, so no socket is opened. The relay is the
real library with an in-memory sink.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for candidate in (str(ROOT), str(ROOT / "libs")):
    if candidate not in sys.path:
        sys.path.insert(0, candidate)

from apps.data_plane import collector_service  # noqa: E402
from data_plane.collectors.schemas import load_descriptor  # noqa: E402

SECRET = b"collector-test-secret"
PROXY = "http://brave-proxy.test:8443"


def brave_row(**overrides) -> dict:
    values = {
        "entryId": "entry-1",
        "revision": "rev-1",
        "url": "https://example.test/page",
        "title": "Example",
        "visitedAt": "2026-11-14T00:00:00Z",
        "transition": "link",
    }
    values.update(overrides)
    return values


def note_row(**overrides) -> dict:
    values = {
        "source_id": "notes.markdown",
        "tombstone": False,
        "note_id": "note-1",
        "revision": "rev-1",
        "path": "/vault/note-1.md",
        "heading": "Note",
    }
    values.update(overrides)
    return values


class CollectorTests(unittest.TestCase):
    def build(self, rows, *, source_id="brave", descriptor=None, fetcher=None):
        descriptor = load_descriptor("brave") if descriptor is None else descriptor
        return collector_service.Collector(
            source_id=source_id,
            descriptor=descriptor,
            proxy_url=PROXY,
            secret=SECRET,
            fetcher=fetcher if fetcher is not None else (lambda _url: rows),
            clock=lambda: 1_000.0,
        )

    def test_normalizes_with_the_source_adapter(self) -> None:
        collector = self.build([brave_row()])
        results = collector.collect()

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["cursor"], 1)
        self.assertEqual(results[0]["source_id"], "brave.history")
        self.assertEqual(len(collector.accepted), 1)
        accepted = collector.accepted[0]
        self.assertEqual(accepted["entry_id"], "entry-1")
        self.assertEqual(accepted["revision"], "rev-1")
        self.assertEqual(accepted["url"], "https://example.test/page")
        self.assertFalse(accepted["tombstone"])

    def test_raw_records_are_validated_without_an_adapter(self) -> None:
        descriptor = load_descriptor("notes")
        collector = self.build(
            [note_row()], source_id="notes", descriptor=descriptor
        )
        results = collector.collect()

        self.assertEqual(results[0]["source_id"], "notes.markdown")
        self.assertEqual(results[0]["idempotency_key"], "notes.markdown:note-1:rev-1")
        self.assertEqual(collector.accepted[0]["note_id"], "note-1")

    def test_schema_rejection_leaves_the_relay_untouched(self) -> None:
        bad = brave_row(title=7)  # adapter rejects a non-string title
        collector = self.build([brave_row(), bad])
        with self.assertRaises(collector_service.CollectorError):
            collector.collect()
        self.assertEqual(collector.accepted, [])
        self.assertEqual(collector.cursor, 0)

    def test_raw_record_with_a_missing_stable_id_is_rejected(self) -> None:
        descriptor = load_descriptor("notes")
        row = note_row()
        del row["note_id"]
        collector = self.build(
            [row], source_id="notes", descriptor=descriptor
        )
        with self.assertRaises(collector_service.CollectorError):
            collector.collect()
        self.assertEqual(collector.accepted, [])

    def test_source_schema_mismatch_is_refused_at_construction(self) -> None:
        with self.assertRaises(collector_service.CollectorError):
            self.build(
                [brave_row()], source_id="mail", descriptor=load_descriptor("brave")
            )

    def test_proxy_url_must_be_http_or_https(self) -> None:
        with self.assertRaises(collector_service.CollectorError):
            collector_service.Collector(
                source_id="brave",
                descriptor=load_descriptor("brave"),
                proxy_url="file:///etc/passwd",
                secret=SECRET,
                fetcher=lambda _url: [],
            )
        with self.assertRaises(collector_service.CollectorError):
            collector_service.Collector(
                source_id="brave",
                descriptor=load_descriptor("brave"),
                proxy_url="http://user:pass@proxy.test",
                secret=SECRET,
                fetcher=lambda _url: [],
            )

    def test_fetch_failure_fails_closed(self) -> None:
        def broken(_url: str):
            raise ConnectionError("proxy is down")

        collector = self.build([], fetcher=broken)
        with self.assertRaises(collector_service.CollectorError):
            collector.collect()
        self.assertEqual(collector.accepted, [])
        self.assertEqual(collector.cursor, 0)

    def test_cursor_advances_and_idempotency_is_stable(self) -> None:
        rows = [brave_row(), brave_row(entryId="entry-2", revision="rev-1")]
        collector = self.build(rows)
        first = collector.collect()

        self.assertEqual([result["cursor"] for result in first], [1, 2])
        self.assertEqual(first[0]["idempotency_key"], "brave.history:entry-1:rev-1")
        self.assertEqual(first[1]["idempotency_key"], "brave.history:entry-2:rev-1")
        self.assertEqual(len(collector.accepted), 2)

        # The same rows replay under the same keys and sink once, not twice.
        second = collector.collect()
        self.assertEqual(second[0]["cursor"], 1)
        self.assertEqual(len(collector.accepted), 2)

    def test_a_new_collection_continues_the_cursor(self) -> None:
        collector = self.build([brave_row()])
        collector.collect()
        collector._fetcher = lambda _url: [brave_row(entryId="entry-2")]
        second = collector.collect()
        self.assertEqual(second[0]["cursor"], 2)

    def test_descriptor_can_be_loaded_from_a_mounted_file(self) -> None:
        source = Path(__file__).resolve().parents[2] / (
            "libs/data_plane/collectors/schemas/brave.json"
        )
        raw = json.loads(source.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as tmp:
            mounted = Path(tmp) / "schema.json"
            mounted.write_text(json.dumps(raw), encoding="utf-8")
            descriptor = collector_service.load_descriptor_source(
                str(mounted), name="brave"
            )
            collector = self.build([brave_row()], descriptor=descriptor)
            collector.collect()
        self.assertEqual(collector.accepted[0]["source_id"], "brave.history")

    def test_tuta_source_uses_the_mail_adapter(self) -> None:
        source = Path(__file__).resolve().parents[2] / (
            "libs/data_plane/collectors/schemas/mail.json"
        )
        raw = json.loads(source.read_text(encoding="utf-8"))
        row = {
            "messageId": "message-1",
            "revision": "rev-1",
            "subject": "Hello",
            "sender": "sender@example.test",
            "recipients": ["rcpt@example.test"],
            "receivedAt": "2026-11-14T00:00:00Z",
            "labels": [],
        }
        with tempfile.TemporaryDirectory() as tmp:
            mounted = Path(tmp) / "tuta.json"
            mounted.write_text(json.dumps(raw), encoding="utf-8")
            descriptor = collector_service.load_descriptor_source(
                str(mounted), name="tuta"
            )
            collector = collector_service.Collector(
                source_id="tuta",
                descriptor=descriptor,
                proxy_url=PROXY,
                secret=SECRET,
                fetcher=lambda _url: [row],
                clock=lambda: 1_000.0,
            )
            collector.collect()
        self.assertEqual(collector.accepted[0]["source_id"], "tuta.mail")
        self.assertEqual(collector.accepted[0]["message_id"], "message-1")
        self.assertEqual(collector.accepted[0]["subject"], "Hello")


if __name__ == "__main__":
    unittest.main()
