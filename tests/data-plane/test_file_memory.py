"""File memory is portable, profile-isolated, tombstone-aware, and offline."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from data_plane.errors import ContractError, ValidationError
from data_plane.memory import FileMemory, MemoryRecord

CAPTURED_AT = "2026-11-01T00:00:00+00:00"
CREATED_AT = "2026-11-01T00:00:00+00:00"
UPDATED_AT = "2026-11-02T00:00:00+00:00"


def provenance(revision: str = "3") -> dict[str, str]:
    return {"source_id": "notes/1", "revision": revision, "captured_at": CAPTURED_AT}


def record(record_id: str = "note-1", **overrides) -> MemoryRecord:
    values = dict(
        id=record_id,
        profile="local",
        source="notes",
        content="hello world",
        tags=("note",),
        provenance=provenance(),
        created_at=CREATED_AT,
        updated_at=UPDATED_AT,
    )
    values.update(overrides)
    return MemoryRecord(**values)


class FileMemoryTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.entered = tempfile.TemporaryDirectory()
        self.addCleanup(self.entered.cleanup)
        self.root = Path(self.entered.name)


class MemoryRecordTests(unittest.TestCase):
    def test_record_fills_timestamps_and_content_hash(self) -> None:
        item = MemoryRecord(
            id="note-1",
            profile="local",
            source="notes",
            content="hello world",
            tags=["note"],
            provenance=provenance(),
        )

        self.assertEqual(item.tags, ("note",))
        self.assertEqual(item.sha256, hashlib.sha256(b"hello world").hexdigest())
        self.assertTrue(item.created_at)
        self.assertEqual(item.created_at, item.updated_at)
        self.assertFalse(item.deleted)

    def test_record_rejects_a_wrong_hash(self) -> None:
        with self.assertRaises(ValidationError):
            record(sha256="0" * 64)

    def test_record_rejects_incomplete_provenance(self) -> None:
        with self.assertRaises(ValidationError):
            record(provenance={"source_id": "notes/1"})

    def test_record_rejects_unknown_profile(self) -> None:
        with self.assertRaises(ValidationError):
            record(profile="work")


class FileMemoryRoundTripTests(FileMemoryTestCase):
    def test_put_get_round_trip(self) -> None:
        memory = FileMemory(self.root, "local")
        item = record()

        stored = memory.put(item)
        loaded = memory.get("note-1")

        self.assertEqual(stored, item)
        self.assertEqual(loaded, item)
        self.assertEqual(loaded.sha256, hashlib.sha256(b"hello world").hexdigest())

        path = self.root / "local" / "note-1.json"
        self.assertTrue(path.is_file())
        payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(payload["id"], "note-1")
        self.assertEqual(payload["profile"], "local")
        self.assertEqual(payload["tags"], ["note"])
        self.assertEqual(payload["provenance"], provenance())

    def test_get_missing_record_returns_none(self) -> None:
        memory = FileMemory(self.root, "local")
        self.assertIsNone(memory.get("absent"))

    def test_memory_is_portable_json(self) -> None:
        FileMemory(self.root, "local").put(record())

        reopened = FileMemory(self.root, "local")

        self.assertEqual(reopened.export(), [record().as_dict()])

    def test_put_requires_a_profile(self) -> None:
        with self.assertRaises(ContractError):
            FileMemory(self.root, profile=None).put(record())

    def test_unknown_profile_is_rejected(self) -> None:
        with self.assertRaises(ContractError):
            FileMemory(self.root, "work")

    def test_put_rejects_a_record_for_another_profile(self) -> None:
        memory = FileMemory(self.root, "local")
        with self.assertRaises(ContractError):
            memory.put(record(profile="external"))
        self.assertFalse((self.root / "local").exists())

    def test_record_id_cannot_escape_the_profile_directory(self) -> None:
        memory = FileMemory(self.root, "local")
        for unsafe in ("../evil", "a/b", "", ".."):
            with self.subTest(record_id=unsafe):
                with self.assertRaises(ValidationError):
                    memory.put(record(record_id=unsafe))


class FileMemoryProfileTests(FileMemoryTestCase):
    def test_profiles_are_isolated(self) -> None:
        local = FileMemory(self.root, "local")
        external = FileMemory(self.root, "external")
        local.put(record(content="local content"))
        external.put(record(profile="external", content="external content"))

        self.assertEqual(local.get("note-1").content, "local content")
        self.assertEqual(external.get("note-1").content, "external content")
        self.assertEqual(len(local.export()), 1)
        self.assertEqual(len(external.export()), 1)
        self.assertEqual(
            [item["content"] for item in FileMemory(self.root).export()],
            ["external content", "local content"],
        )

    def test_administrative_get_refuses_an_ambiguous_id(self) -> None:
        FileMemory(self.root, "local").put(record())
        FileMemory(self.root, "external").put(record(profile="external"))

        with self.assertRaises(ContractError):
            FileMemory(self.root).get("note-1")


class FileMemoryDeleteTests(FileMemoryTestCase):
    def test_delete_writes_a_tombstone_and_keeps_the_file(self) -> None:
        memory = FileMemory(self.root, "local")
        memory.put(record())

        tombstone = memory.delete("note-1")

        path = self.root / "local" / "note-1.json"
        self.assertTrue(path.is_file())
        self.assertTrue(tombstone.deleted)
        self.assertEqual(tombstone.content, "")
        self.assertEqual(tombstone.provenance, provenance())
        self.assertTrue(json.loads(path.read_text(encoding="utf-8"))["deleted"])

    def test_tombstones_are_excluded_by_default(self) -> None:
        memory = FileMemory(self.root, "local")
        memory.put(record())
        memory.delete("note-1")

        self.assertEqual(memory.search(""), ())
        self.assertEqual(len(memory.search("", include_deleted=True)), 1)
        self.assertEqual(len(memory.export()), 1)
        self.assertTrue(memory.export()[0]["deleted"])

    def test_delete_of_a_missing_record_is_rejected(self) -> None:
        memory = FileMemory(self.root, "local")
        with self.assertRaises(ValidationError):
            memory.delete("absent")


class FileMemorySearchTests(FileMemoryTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.memory = FileMemory(self.root, "local")
        self.memory.put(record("note-1", content="Hello World", tags=("note", "daily")))
        self.memory.put(
            record("note-2", content="unrelated text", tags=("note",), updated_at=UPDATED_AT)
        )
        self.memory.put(
            record("note-3", content="other", tags=("archive",), updated_at=CREATED_AT)
        )

    def test_text_search_is_case_insensitive(self) -> None:
        self.assertEqual([item.id for item in self.memory.search("HELLO")], ["note-1"])

    def test_text_search_covers_tags(self) -> None:
        self.assertEqual(
            sorted(item.id for item in self.memory.search("archive")), ["note-3"]
        )

    def test_tag_filter_requires_every_tag(self) -> None:
        self.assertEqual(
            [item.id for item in self.memory.search("", tags=("note", "daily"))],
            ["note-1"],
        )
        self.assertEqual(len(self.memory.search("", tags=("note",))), 2)
        self.assertEqual(self.memory.search("", tags=("note", "archive")), ())

    def test_text_and_tag_filters_combine(self) -> None:
        self.assertEqual(
            [item.id for item in self.memory.search("hello", tags=("daily",))],
            ["note-1"],
        )

    def test_results_are_newest_first_then_by_id(self) -> None:
        self.memory.put(record("note-0", content="same age"))
        self.assertEqual(
            [item.id for item in self.memory.search("")],
            ["note-0", "note-1", "note-2", "note-3"],
        )


class FileMemoryImportTests(FileMemoryTestCase):
    def test_export_is_deterministic(self) -> None:
        memory = FileMemory(self.root, "local")
        memory.put(record("note-b"))
        memory.put(record("note-a"))
        FileMemory(self.root, "external").put(record("note-0", profile="external"))

        self.assertEqual(
            [item["id"] for item in FileMemory(self.root).export()],
            ["note-0", "note-a", "note-b"],
        )
        self.assertEqual(memory.export(), memory.export())

    def test_import_merges_and_keeps_the_newest_revision(self) -> None:
        source = FileMemory(self.root / "source", "local")
        source.put(record("note-1", content="newer", updated_at="2026-11-05T00:00:00+00:00"))
        source.put(record("note-2"))

        target = FileMemory(self.root / "target", "local")
        target.put(record("note-1", content="older", updated_at=UPDATED_AT))

        written = target.import_records(source.export())

        self.assertEqual([item.id for item in written], ["note-1", "note-2"])
        self.assertEqual(target.get("note-1").content, "newer")
        self.assertEqual(target.get("note-2").content, "hello world")

    def test_import_ignores_an_older_revision(self) -> None:
        target = FileMemory(self.root, "local")
        target.put(record("note-1", content="newer", updated_at="2026-11-05T00:00:00+00:00"))

        written = target.import_records([record(content="older", updated_at=UPDATED_AT).as_dict()])

        self.assertEqual(written, ())
        self.assertEqual(target.get("note-1").content, "newer")

    def test_a_tombstone_wins_a_timestamp_tie(self) -> None:
        target = FileMemory(self.root, "local")
        target.put(record())

        written = target.import_records([record(deleted=True, content="").as_dict()])

        self.assertEqual([item.id for item in written], ["note-1"])
        self.assertTrue(target.get("note-1").deleted)

    def test_import_refuses_a_cross_profile_record(self) -> None:
        memory = FileMemory(self.root, "local")
        foreign = record(profile="external").as_dict()

        with self.assertRaises(ContractError):
            memory.import_records([foreign])
        self.assertEqual(memory.export(), [])

    def test_import_refuses_malformed_records(self) -> None:
        memory = FileMemory(self.root, "local")
        payload = record().as_dict()
        del payload["sha256"]

        with self.assertRaises(ValidationError):
            memory.import_records([payload])
        self.assertEqual(memory.export(), [])

    def test_import_is_idempotent(self) -> None:
        memory = FileMemory(self.root, "local")
        memory.put(record())

        written = memory.import_records(memory.export())

        self.assertEqual(written, ())
        self.assertEqual(memory.export(), [record().as_dict()])


if __name__ == "__main__":
    unittest.main()
