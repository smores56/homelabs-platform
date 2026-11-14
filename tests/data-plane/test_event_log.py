"""Event log idempotency, ordering, cursors, long-poll, and effect tests."""

from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path

from data_plane.errors import (
    CursorError,
    EpochError,
    IdempotencyConflict,
    ValidationError,
)
from data_plane.event_log import Event, EventLog


class EventLogTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "state" / "events.db"
        self.log = EventLog(self.path)
        self.addCleanup(self.log.close)

    def append(
        self,
        kind: str = "book.updated",
        entity: str = "book-1",
        payload=None,
        **kwargs,
    ) -> Event:
        body = {"title": "Dune"} if payload is None else payload
        return self.log.append(kind, entity, body, **kwargs)

    def test_append_returns_position_epoch_and_timestamp(self):
        event = self.append()
        self.assertEqual(event.position, 1)
        self.assertEqual(event.epoch, 1)
        self.assertEqual(event.kind, "book.updated")
        self.assertEqual(event.entity, "book-1")
        self.assertEqual(event.payload, {"title": "Dune"})
        self.assertTrue(event.event_id)
        self.assertTrue(event.created_at.endswith("Z"))

    def test_read_orders_by_position_and_honours_limit(self):
        for index in range(5):
            self.append(entity=f"book-{index}")
        self.assertEqual([event.position for event in self.log.read()], [1, 2, 3, 4, 5])
        self.assertEqual(
            [event.entity for event in self.log.read(after=2, limit=2)],
            ["book-2", "book-3"],
        )
        self.assertEqual(self.log.read(after=99), [])
        self.assertEqual(self.log.latest().position, 5)

    def test_empty_log_has_no_latest(self):
        self.assertIsNone(self.log.latest())
        self.assertEqual(self.log.read(), [])
        self.assertEqual(self.log.epoch, 1)

    def test_same_idempotency_key_and_payload_returns_existing_event(self):
        first = self.append(idempotency_key="op-1")
        again = self.append(idempotency_key="op-1")
        self.assertEqual(first, again)
        self.assertEqual(len(self.log.read()), 1)

    def test_idempotency_key_with_different_payload_conflicts(self):
        self.append(idempotency_key="op-1")
        with self.assertRaises(IdempotencyConflict):
            self.append(payload={"title": "Other"}, idempotency_key="op-1")
        with self.assertRaises(IdempotencyConflict):
            self.append(kind="book.deleted", idempotency_key="op-1")
        self.assertEqual(len(self.log.read()), 1)

    def test_explicit_event_id_is_idempotent_and_conflicts_on_change(self):
        first = self.append(event_id="evt-fixed")
        self.assertEqual(self.append(event_id="evt-fixed"), first)
        with self.assertRaises(IdempotencyConflict):
            self.append(entity="book-2", event_id="evt-fixed")

    def test_cursor_starts_at_zero_and_ack_is_monotonic(self):
        self.append()
        self.append()
        self.assertEqual(self.log.cursor("worker"), (0, 1))
        self.assertEqual(self.log.ack("worker", 1), 1)
        self.assertEqual(self.log.cursor("worker"), (1, 1))
        self.assertEqual(self.log.ack("worker", 1), 1)
        with self.assertRaises(CursorError):
            self.log.ack("worker", 0)
        self.assertEqual(self.log.cursor("worker"), (1, 1))

    def test_ack_past_latest_is_a_gap(self):
        self.append()
        with self.assertRaises(CursorError):
            self.log.ack("worker", 2)
        self.assertEqual(self.log.cursor("worker"), (0, 1))

    def test_rebuild_bumps_epoch_and_invalidates_stale_cursor(self):
        self.append()
        self.log.ack("worker", 1)
        self.assertEqual(self.log.rebuild(2), 2)
        self.assertEqual(self.log.epoch, 2)
        self.assertEqual(self.log.cursor("worker"), (1, 1))
        with self.assertRaises(EpochError):
            self.log.ack("worker", 1)
        self.assertNotEqual(self.append(kind="book.created").epoch, 1)
        self.log.reset_cursor("worker")
        self.assertEqual(self.log.cursor("worker"), (0, 2))
        self.assertEqual(self.log.ack("worker", 2), 2)

    def test_rebuild_refuses_an_unchanged_or_lower_epoch(self):
        self.log.rebuild(3)
        with self.assertRaises(EpochError):
            self.log.rebuild(3)
        with self.assertRaises(EpochError):
            self.log.rebuild(2)

    def test_reopen_preserves_events_and_epoch(self):
        self.append()
        self.log.rebuild(4)
        self.log.close()
        reopened = EventLog(self.path)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.epoch, 4)
        self.assertEqual([event.position for event in reopened.read()], [1])

    def test_requested_epoch_must_match_an_existing_log(self):
        self.log.close()
        with self.assertRaises(EpochError):
            EventLog(self.path, epoch=2)

    def test_new_log_honours_the_requested_epoch(self):
        fresh = EventLog(Path(self._tmp.name) / "fresh.db", epoch=7)
        self.addCleanup(fresh.close)
        self.assertEqual(fresh.epoch, 7)
        self.assertEqual(fresh.append("seed", "source").epoch, 7)

    def test_subscribe_starts_after_the_durable_cursor_without_acking(self):
        self.append()
        second = self.append(kind="book.created")
        self.log.ack("worker", 1)
        events = list(self.log.subscribe("worker", timeout=0.0))
        self.assertEqual([event.position for event in events], [second.position])
        self.assertEqual(self.log.cursor("worker"), (1, 1))

    def test_subscribe_long_polls_for_a_concurrent_append(self):
        received: list[Event] = []

        def consume() -> None:
            for event in self.log.subscribe("worker", timeout=5.0, poll_interval=0.01):
                received.append(event)
                break

        worker = threading.Thread(target=consume, daemon=True)
        worker.start()
        time.sleep(0.2)
        self.append(kind="book.created", entity="book-9")
        worker.join(5.0)
        self.assertFalse(worker.is_alive())
        self.assertEqual([event.entity for event in received], ["book-9"])

    def test_subscribe_times_out_without_events(self):
        self.assertEqual(list(self.log.subscribe("worker", timeout=0.05)), [])

    def test_subscribe_refuses_a_stale_cursor(self):
        self.append()
        self.log.ack("worker", 1)
        self.log.rebuild(2)
        with self.assertRaises(EpochError):
            next(self.log.subscribe("worker", timeout=0.0))

    def test_concurrent_appends_get_unique_positions(self):
        def writer(index: int) -> None:
            for item in range(5):
                self.log.append("book.updated", f"book-{index}-{item}", {"item": item})

        writers = [threading.Thread(target=writer, args=(index,)) for index in range(8)]
        for thread in writers:
            thread.start()
        for thread in writers:
            thread.join(10)
            self.assertFalse(thread.is_alive())
        positions = [event.position for event in self.log.read(limit=100)]
        self.assertEqual(sorted(positions), list(range(1, 41)))

    def test_effects_round_trip_and_update(self):
        self.assertIsNone(self.log.effect("delete:book-1"))
        created = self.log.record_effect("delete:book-1", "applied", {"deleted": True})
        self.assertEqual(created.key, "delete:book-1")
        self.assertEqual(created.status, "applied")
        self.assertEqual(created.result, {"deleted": True})
        updated = self.log.record_effect("delete:book-1", "failed", {"reason": "denied"})
        self.assertEqual(updated.status, "failed")
        self.assertEqual(updated.result, {"reason": "denied"})
        self.assertEqual(updated.created_at, created.created_at)
        self.assertEqual(self.log.effect("delete:book-1"), updated)

    def test_effects_may_omit_the_result_body(self):
        effect = self.log.record_effect("create:book-2", "applied")
        self.assertIsNone(effect.result)
        self.assertEqual(self.log.effect("create:book-2"), effect)

    def test_rejects_empty_fields_and_negative_windows(self):
        with self.assertRaises(ValidationError):
            self.log.append("", "book-1")
        with self.assertRaises(ValidationError):
            self.log.append("book.updated", "  ")
        with self.assertRaises(ValidationError):
            self.log.append("book.updated", "book-1", payload=["not", "a", "mapping"])
        with self.assertRaises(ValidationError):
            self.log.read(limit=-1)
        with self.assertRaises(ValidationError):
            self.log.read(after=-1)
        with self.assertRaises(ValidationError):
            self.log.record_effect("delete:book-1", "")
        with self.assertRaises(ValidationError):
            self.log.ack("worker", -1)


if __name__ == "__main__":
    unittest.main()
