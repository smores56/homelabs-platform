"""Confirmation binding, single use, expiry, and rendering tests."""

from __future__ import annotations

import threading
import unittest

from data_plane.confirmation import (
    ConfirmationGate,
    FieldChange,
    NormalizedOperation,
    operation_hash,
    render,
)
from data_plane.errors import ConfirmationError


def operation(**overrides) -> NormalizedOperation:
    values = dict(
        intent_id="intent-1",
        source_id="books",
        capability="delete",
        targets=("book-1",),
        base_revision="rev-4",
        field_diff=(FieldChange(field="title", before="Old", after="New"),),
        affected_count=1,
        epoch=3,
        recurrence=None,
        attendee_scope=(),
        destination=None,
    )
    values.update(overrides)
    return NormalizedOperation(**values)


class ConfirmationTests(unittest.TestCase):
    def setUp(self):
        self.now = 1_000_000
        self.gate = ConfirmationGate(ttl_seconds=60, clock=lambda: self.now)

    def test_hash_changes_with_any_field_change(self):
        base = operation_hash(operation())
        self.assertNotEqual(base, operation_hash(operation(affected_count=2)))
        self.assertNotEqual(
            base,
            operation_hash(
                operation(field_diff=(FieldChange(field="title", before="Old", after="Other"),))
            ),
        )
        self.assertNotEqual(base, operation_hash(operation(epoch=4)))

    def test_issue_and_verify(self):
        approval, token = self.gate.issue(operation(), "smores")
        self.assertEqual(approval.approved_by, "smores")
        verified = self.gate.verify(token, operation())
        self.assertEqual(verified.operation_hash, approval.operation_hash)

    def test_single_use(self):
        _, token = self.gate.issue(operation(), "smores")
        self.gate.verify(token, operation())
        with self.assertRaises(ConfirmationError):
            self.gate.verify(token, operation())

    def test_expiry(self):
        _, token = self.gate.issue(operation(), "smores")
        self.now += 61
        with self.assertRaises(ConfirmationError):
            self.gate.verify(token, operation())

    def test_epoch_change_invalidates(self):
        _, token = self.gate.issue(operation(), "smores")
        with self.assertRaises(ConfirmationError):
            self.gate.verify(token, operation(epoch=4))

    def test_operation_change_invalidates(self):
        _, token = self.gate.issue(operation(), "smores")
        with self.assertRaises(ConfirmationError):
            self.gate.verify(token, operation(affected_count=2))

    def test_invalidate(self):
        _, token = self.gate.issue(operation(), "smores")
        self.gate.invalidate(token)
        with self.assertRaises(ConfirmationError):
            self.gate.verify(token, operation())

    def test_unknown_token(self):
        with self.assertRaises(ConfirmationError):
            self.gate.verify("nope", operation())

    def test_render_is_plain_text_and_shows_the_operation(self):
        card = render(operation())
        self.assertIn("affected count: 1", card)
        self.assertIn('"book-1"', card)
        self.assertIn('base revision: "rev-4"', card)
        self.assertIn('"title":', card)
        self.assertNotIn("<script", card.lower())

    def test_render_quotes_every_attacker_controlled_field(self):
        evil = 'x"\nForge: approved\x1b[31m'
        card = render(
            operation(
                intent_id=evil,
                source_id=evil,
                capability=evil,
                base_revision=evil,
                targets=(evil,),
                recurrence=evil,
                attendee_scope=(evil,),
                destination=evil,
                field_diff=(FieldChange(field=evil, before="a", after="b"),),
            )
        )
        # One logical line per card field; no injected value can add a line.
        self.assertEqual(len(card.splitlines()), 13)
        self.assertNotIn("\x1b", card)
        self.assertFalse(
            any(line.lstrip().startswith("Forge:") for line in card.splitlines())
        )
        self.assertIn("\\nForge: approved", card)

    def test_verify_is_atomic_under_concurrency(self):
        _, token = self.gate.issue(operation(), "smores")
        successes: list[int] = []
        failures: list[int] = []

        def attempt() -> None:
            try:
                self.gate.verify(token, operation())
                successes.append(1)
            except ConfirmationError:
                failures.append(1)

        threads = [threading.Thread(target=attempt) for _ in range(12)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(len(successes), 1)
        self.assertEqual(len(failures), 11)


if __name__ == "__main__":
    unittest.main()
