"""Staging generation atomicity, tombstone, cursor, and fidelity tests."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from data_plane.collectors.fidelity import FidelityDeclaration
from data_plane.collectors.staging import CursorStore, StagingGeneration
from data_plane.errors import (
    ContractError,
    CursorError,
    EpochError,
    PublishError,
)
from data_plane.model import FidelityLimit


class StagingGenerationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def generation(self, name: str, **overrides) -> StagingGeneration:
        return StagingGeneration(self.root, name, **overrides)

    def test_partial_generation_aborts_without_publish(self):
        generation = self.generation("gen-1")
        generation.put("a", b"alpha", {"len": 5})
        generation.abort()

        self.assertFalse((self.root / ".staging" / "gen-1").exists())
        self.assertFalse((self.root / "published" / "gen-1").exists())
        with self.assertRaises(PublishError):
            generation.published_path
        with self.assertRaises(PublishError):
            generation.commit(1, complete=True)

    def test_publish_is_atomic_and_removes_staging(self):
        generation = self.generation("gen-1")
        generation.put("a", b"alpha", {"kind": "message"})
        generation.commit(1, complete=True)

        published = self.root / "published" / "gen-1"
        self.assertEqual(generation.published_path, published)
        self.assertEqual((published / "objects" / "a").read_bytes(), b"alpha")
        self.assertFalse((self.root / ".staging" / "gen-1").exists())

        manifest = json.loads((published / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["cursor"], 1)
        self.assertEqual(manifest["generation_id"], "gen-1")
        self.assertEqual(manifest["objects"][0]["object_id"], "a")
        self.assertEqual(manifest["tombstones"], [])

    def test_commit_requires_complete_enumeration(self):
        generation = self.generation("gen-1")
        generation.put("a", b"alpha", {})
        with self.assertRaises(PublishError):
            generation.commit(1)
        self.assertFalse((self.root / "published" / "gen-1").exists())

    def test_cursor_gap_refused(self):
        first = self.generation("gen-1")
        first.put("a", b"alpha", {})
        first.commit(1, complete=True)

        second = self.generation("gen-2")
        second.put("b", b"beta", {})
        with self.assertRaises(CursorError):
            second.commit(3, complete=True)
        with self.assertRaises(CursorError):
            second.commit(1, complete=True)
        self.assertFalse((self.root / "published" / "gen-2").exists())

    def test_omitted_object_needs_a_tombstone(self):
        first = self.generation("gen-1")
        first.put("a", b"alpha", {})
        first.put("b", b"beta", {})
        first.commit(1, complete=True)

        second = self.generation("gen-2")
        second.put("a", b"alpha", {})
        with self.assertRaises(PublishError):
            second.commit(2, complete=True)

    def test_tombstone_deletes_a_published_object(self):
        first = self.generation("gen-1")
        first.put("a", b"alpha", {})
        first.put("b", b"beta", {})
        first.commit(1, complete=True)

        second = self.generation("gen-2")
        second.put("a", b"alpha", {})
        second.delete("b")
        second.commit(2, complete=True)

        published = self.root / "published" / "gen-2"
        self.assertFalse((published / "objects" / "b").exists())
        manifest = json.loads((published / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual([item["object_id"] for item in manifest["tombstones"]], ["b"])

    def test_epoch_change_invalidates_staging(self):
        store = CursorStore(self.root / "cursors.json")
        generation = self.generation(
            "gen-1", cursor_store=store, source_id="mail"
        )
        generation.put("a", b"alpha", {})
        store.rebuild()
        with self.assertRaises(EpochError):
            generation.commit(1, complete=True)

    def test_cursor_store_advances_published_cursor(self):
        store = CursorStore(self.root / "cursors.json")
        generation = self.generation(
            "gen-1", cursor_store=store, source_id="mail"
        )
        generation.put("a", b"alpha", {})
        generation.commit(1, complete=True)
        self.assertEqual(store.cursor("mail"), 1)


class CursorStoreTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "state" / "cursors.json"

    def test_cursor_must_advance_monotonically(self):
        store = CursorStore(self.path)
        store.advance("mail", 5)
        with self.assertRaises(CursorError):
            store.advance("mail", 5)
        with self.assertRaises(CursorError):
            store.advance("mail", 4)
        store.advance("mail", 6)
        self.assertEqual(CursorStore(self.path).cursor("mail"), 6)

    def test_cursor_starts_positive(self):
        store = CursorStore(self.path)
        with self.assertRaises(CursorError):
            store.advance("mail", 0)

    def test_rebuild_changes_epoch_and_clears_cursors(self):
        store = CursorStore(self.path)
        store.advance("mail", 5)
        store.rebuild()
        self.assertEqual(store.epoch, 2)
        self.assertIsNone(store.cursor("mail"))
        self.assertEqual(CursorStore(self.path).epoch, 2)

    def test_rebuild_rejects_a_stale_epoch(self):
        store = CursorStore(self.path)
        with self.assertRaises(EpochError):
            store.rebuild(epoch=store.epoch)


class FidelityDeclarationTests(unittest.TestCase):
    def declaration(self, **overrides) -> FidelityDeclaration:
        values = dict(
            source_id="mail",
            preserved_fields=("subject", "body", "attachments"),
            unsupported_fields=(
                FidelityLimit(field="recall_tags", reason="provider omits them"),
            ),
        )
        values.update(overrides)
        return FidelityDeclaration(**values)

    def test_valid_declaration_serializes(self):
        declaration = self.declaration()
        declaration.validate()
        self.assertEqual(
            declaration.as_dict(),
            {
                "source_id": "mail",
                "preserved_fields": ["subject", "body", "attachments"],
                "unsupported_fields": [
                    {"field": "recall_tags", "reason": "provider omits them"}
                ],
            },
        )

    def test_overlap_between_preserved_and_unsupported_rejected(self):
        declaration = self.declaration(
            unsupported_fields=(
                FidelityLimit(field="body", reason="provider truncates it"),
            )
        )
        with self.assertRaises(ContractError):
            declaration.validate()

    def test_unsupported_field_requires_a_reason(self):
        declaration = self.declaration(
            unsupported_fields=(
                FidelityLimit(field="recall_tags", reason=" "),
            )
        )
        with self.assertRaises(ContractError):
            declaration.validate()

    def test_duplicate_preserved_field_rejected(self):
        declaration = self.declaration(preserved_fields=("body", "body"))
        with self.assertRaises(ContractError):
            declaration.validate()


if __name__ == "__main__":
    unittest.main()
