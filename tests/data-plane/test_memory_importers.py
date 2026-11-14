"""Offline Graphiti and markdown memory importers, with synthetic fixtures."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from data_plane.errors import ContractError, ValidationError
from data_plane.memory import FileMemory
from data_plane.memory.importers import (
    MARKDOWN_SOURCE,
    import_graphiti_export,
    import_markdown_notes,
    markdown_record_id,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"
GRAPHITI_EXPORT = FIXTURES / "graphiti_export.json"
GRAPHITI_EXTERNAL = FIXTURES / "graphiti_export_external.json"
NOTES_VAULT = FIXTURES / "notes_vault"

FIRST_NODE = "graphiti-11111111-1111-4111-8111-111111111111"
SECOND_NODE = "graphiti-22222222-2222-4222-8222-222222222222"
EXPIRED_NODE = "graphiti-33333333-3333-4333-8333-333333333333"


class ImporterTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.memory = FileMemory(self.root, "local")

    def write_export(self, payload: dict) -> Path:
        path = self.root / "tampered-export.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path


class GraphitiImportTests(ImporterTestCase):
    def test_imports_every_node_with_stable_ids_and_provenance(self) -> None:
        written = import_graphiti_export(GRAPHITI_EXPORT, self.memory)

        self.assertEqual(
            [record.id for record in written],
            [FIRST_NODE, SECOND_NODE, EXPIRED_NODE],
        )
        first = self.memory.get(FIRST_NODE)
        self.assertIsNotNone(first)
        self.assertEqual(first.content, "Example Person prefers synthetic tea.")
        self.assertEqual(first.tags, ("Entity", "Preference", "graphiti"))
        self.assertEqual(
            first.provenance,
            {
                "source_id": "graphiti.synthetic",
                "revision": "1",
                "captured_at": "2026-01-02T09:00:00+00:00",
            },
        )
        self.assertEqual(
            first.sha256, hashlib.sha256(first.content.encode("utf-8")).hexdigest()
        )
        self.assertFalse(first.deleted)
        self.assertEqual(first.created_at, "2026-01-01T09:00:00+00:00")
        self.assertEqual(first.updated_at, "2026-01-02T09:00:00+00:00")

    def test_node_level_provenance_overrides_the_export_index(self) -> None:
        import_graphiti_export(GRAPHITI_EXPORT, self.memory)

        second = self.memory.get(SECOND_NODE)
        self.assertEqual(second.provenance["revision"], "2")
        self.assertEqual(second.provenance["captured_at"], "2026-01-04T09:00:00+00:00")

    def test_expired_node_imports_as_a_contentless_tombstone(self) -> None:
        import_graphiti_export(GRAPHITI_EXPORT, self.memory)

        expired = self.memory.get(EXPIRED_NODE)
        self.assertTrue(expired.deleted)
        self.assertEqual(expired.content, "")
        self.assertEqual(expired.provenance["revision"], "3")
        self.assertNotIn(EXPIRED_NODE, [
            record["id"]
            for record in self.memory.export()
            if not record["deleted"]
        ])

    def test_deleted_node_without_text_still_imports_as_a_tombstone(self) -> None:
        payload = json.loads(GRAPHITI_EXPORT.read_text(encoding="utf-8"))
        node = payload["nodes"][2]
        node.pop("name")
        node.pop("summary")
        node["deleted"] = True

        import_graphiti_export(self.write_export(payload), self.memory)

        expired = self.memory.get(EXPIRED_NODE)
        self.assertTrue(expired.deleted)
        self.assertEqual(expired.content, "")

    def test_import_is_deterministic(self) -> None:
        other_root = self.root / "other"
        other = FileMemory(other_root, "local")

        import_graphiti_export(GRAPHITI_EXPORT, self.memory)
        import_graphiti_export(GRAPHITI_EXPORT, other)

        self.assertEqual(self.memory.export(), other.export())

    def test_import_is_idempotent(self) -> None:
        first = import_graphiti_export(GRAPHITI_EXPORT, self.memory)
        second = import_graphiti_export(GRAPHITI_EXPORT, self.memory)

        self.assertEqual(len(first), 3)
        self.assertEqual(second, ())
        self.assertEqual(len(self.memory.export()), 3)

    def test_cross_profile_export_is_refused(self) -> None:
        with self.assertRaises(ContractError):
            import_graphiti_export(GRAPHITI_EXTERNAL, self.memory)

    def test_node_profile_must_match_the_target(self) -> None:
        payload = json.loads(GRAPHITI_EXPORT.read_text(encoding="utf-8"))
        payload["nodes"][0]["profile"] = "external"

        with self.assertRaises(ContractError):
            import_graphiti_export(self.write_export(payload), self.memory)

    def test_memory_without_a_profile_is_refused(self) -> None:
        admin = FileMemory(self.root)
        with self.assertRaises(ContractError):
            import_graphiti_export(GRAPHITI_EXPORT, admin)

    def test_missing_revision_is_refused(self) -> None:
        payload = json.loads(GRAPHITI_EXPORT.read_text(encoding="utf-8"))
        payload["provenance"]["nodes"][
            "11111111-1111-4111-8111-111111111111"
        ] = {"captured_at": "2026-01-02T09:00:00+00:00"}

        with self.assertRaises(ValidationError):
            import_graphiti_export(self.write_export(payload), self.memory)

    def test_missing_provenance_is_refused(self) -> None:
        payload = json.loads(GRAPHITI_EXPORT.read_text(encoding="utf-8"))
        payload.pop("provenance")

        with self.assertRaises(ValidationError):
            import_graphiti_export(self.write_export(payload), self.memory)

    def test_duplicate_node_uuid_is_refused(self) -> None:
        payload = json.loads(GRAPHITI_EXPORT.read_text(encoding="utf-8"))
        payload["nodes"].append(dict(payload["nodes"][0]))

        with self.assertRaises(ValidationError):
            import_graphiti_export(self.write_export(payload), self.memory)

    def test_edge_with_unknown_node_is_refused(self) -> None:
        payload = json.loads(GRAPHITI_EXPORT.read_text(encoding="utf-8"))
        payload["edges"][0]["target_node_uuid"] = "99999999-9999-4999-8999-999999999999"

        with self.assertRaises(ValidationError):
            import_graphiti_export(self.write_export(payload), self.memory)


class MarkdownImportTests(ImporterTestCase):
    def test_imports_frontmatter_tags_and_headings(self) -> None:
        written = import_markdown_notes(NOTES_VAULT, self.memory)
        self.assertEqual(len(written), 3)

        first = self.memory.get(markdown_record_id("example-note.md", "First Section"))
        self.assertIsNotNone(first)
        self.assertEqual(first.source, MARKDOWN_SOURCE)
        self.assertEqual(first.tags, ("synthetic", "example", "obsidian"))
        self.assertIn("Synthetic body for the first section", first.content)
        self.assertEqual(first.provenance["source_id"], MARKDOWN_SOURCE)
        self.assertEqual(
            first.provenance["captured_at"], "2026-03-02T00:00:00+00:00"
        )
        self.assertEqual(first.created_at, "2026-03-01T00:00:00+00:00")
        self.assertEqual(first.updated_at, "2026-03-02T00:00:00+00:00")

        second = self.memory.get(markdown_record_id("example-note.md", "Second Section"))
        self.assertIn("never fetched", second.content)
        self.assertIn("https://example.invalid/fake.png", second.content)

        plain = self.memory.get(markdown_record_id("nested/plain.md", "Plain Heading"))
        self.assertEqual(plain.content, "Plain synthetic body without frontmatter.")
        self.assertEqual(plain.tags, ("obsidian",))
        self.assertTrue(plain.created_at)
        self.assertEqual(plain.created_at, plain.updated_at)

    def test_headings_inside_fenced_blocks_do_not_split_sections(self) -> None:
        import_markdown_notes(NOTES_VAULT, self.memory)
        ids = {record["id"] for record in self.memory.export()}
        self.assertNotIn(
            markdown_record_id("example-note.md", "Not A Heading"), ids
        )

    def test_revision_is_the_file_content_hash(self) -> None:
        import_markdown_notes(NOTES_VAULT, self.memory)
        record = self.memory.get(markdown_record_id("example-note.md", "First Section"))
        digest = hashlib.sha256(
            (NOTES_VAULT / "example-note.md").read_bytes()
        ).hexdigest()

        self.assertEqual(record.provenance["revision"], digest)

    def test_import_is_deterministic(self) -> None:
        other = FileMemory(self.root / "other", "local")
        import_markdown_notes(NOTES_VAULT, self.memory)
        import_markdown_notes(NOTES_VAULT, other)

        self.assertEqual(self.memory.export(), other.export())

    def test_import_is_idempotent(self) -> None:
        first = import_markdown_notes(NOTES_VAULT, self.memory)
        second = import_markdown_notes(NOTES_VAULT, self.memory)

        self.assertEqual(len(first), 3)
        self.assertEqual(second, ())
        self.assertEqual(len(self.memory.export()), 3)

    def test_single_file_import_uses_the_file_as_path(self) -> None:
        written = import_markdown_notes(NOTES_VAULT / "example-note.md", self.memory)
        self.assertEqual(len(written), 2)
        self.assertIsNotNone(
            self.memory.get(markdown_record_id("example-note.md", "First Section"))
        )

    def test_duplicate_heading_is_refused(self) -> None:
        note = self.root / "duplicate.md"
        note.write_text("# Same\n\nFirst.\n\n# Same\n\nSecond.\n", encoding="utf-8")

        with self.assertRaises(ValidationError):
            import_markdown_notes(note, self.memory)

    def test_memory_without_a_profile_is_refused(self) -> None:
        admin = FileMemory(self.root)
        with self.assertRaises(ContractError):
            import_markdown_notes(NOTES_VAULT, admin)

    def test_missing_markdown_path_is_refused(self) -> None:
        with self.assertRaises(ValidationError):
            import_markdown_notes(self.root / "absent", self.memory)


if __name__ == "__main__":
    unittest.main()
