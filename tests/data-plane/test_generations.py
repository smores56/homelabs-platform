"""Generation manifest integrity, atomic storage, and restore selection tests."""

from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from data_plane.backup.generations import (
    GenerationManifest,
    SnapshotRef,
    manifest_hash,
    read_manifest,
    select_generation,
    write_manifest,
)
from data_plane.errors import ManifestError, RestoreError

DESTINATIONS = ("primary", "secondary")
DATASETS = ("content", "events")
SCHEMA_VERSION = 3


def ref(destination: str = "primary", **overrides) -> SnapshotRef:
    values = dict(
        destination=destination,
        snapshot_id=f"snap-{destination}",
        created_at=1_000.0,
        group_id="books",
        dataset="data/books",
        parent=None,
        verified=True,
    )
    values.update(overrides)
    return SnapshotRef(**values)


def manifest(**overrides) -> GenerationManifest:
    values = dict(
        version=1,
        generation_id="books-1000",
        group_id="books",
        created_at=1_000.0,
        datasets=(("content", "zfs-1"), ("events", "zfs-2")),
        sqlite=(("events", 2, 17),),
        app_versions=(("broker", "1.2.3"),),
        content_hashes=(("/srv/books/a.epub", "ab" * 32),),
        dependencies=("events", "config"),
        destinations=(ref("primary"), ref("secondary")),
        complete=True,
        schema_version=SCHEMA_VERSION,
        release="2026.11",
    )
    values.update(overrides)
    return GenerationManifest(**values)


class ManifestSerializationTests(unittest.TestCase):
    def test_dict_roundtrip(self):
        item = manifest()
        self.assertEqual(GenerationManifest.from_dict(item.to_dict()), item)
        self.assertEqual(json.loads(json.dumps(item.to_dict())), item.to_dict())

    def test_hash_is_stable_and_content_addressed(self):
        item = manifest()
        self.assertEqual(
            manifest_hash(item),
            manifest_hash(GenerationManifest.from_dict(item.to_dict())),
        )
        self.assertNotEqual(
            manifest_hash(item),
            manifest_hash(replace(item, generation_id="books-1001")),
        )
        self.assertNotEqual(
            manifest_hash(item),
            manifest_hash(replace(item, destinations=(ref("primary"),))),
        )

    def test_write_read_roundtrip_is_atomic(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "store" / "books-1000.json"
            written = write_manifest(path, manifest())
            self.assertEqual(read_manifest(path), manifest())
            self.assertEqual(written, path)
            self.assertEqual(
                [entry.name for entry in path.parent.iterdir()],
                ["books-1000.json"],
            )
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["manifest_hash"], manifest_hash(manifest()))

    def test_read_rejects_corrupt_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "broken.json"
            path.write_text("{not json", encoding="utf-8")
            with self.assertRaises(ManifestError):
                read_manifest(path)

    def test_read_rejects_tampered_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "books-1000.json"
            write_manifest(path, manifest())
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["group_id"] = "photos"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(ManifestError):
                read_manifest(path)

    def test_read_rejects_missing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ManifestError):
                read_manifest(Path(tmp) / "absent.json")


class ManifestValidationTests(unittest.TestCase):
    def test_valid_manifest_passes(self):
        manifest().validate(DESTINATIONS, DATASETS, SCHEMA_VERSION)

    def test_incomplete_manifest_rejected(self):
        with self.assertRaises(ManifestError):
            manifest(complete=False).validate(DESTINATIONS, DATASETS, SCHEMA_VERSION)

    def test_missing_destination_rejected(self):
        with self.assertRaises(ManifestError):
            manifest(destinations=(ref("primary"),)).validate(
                DESTINATIONS, DATASETS, SCHEMA_VERSION
            )

    def test_duplicate_destination_rejected(self):
        with self.assertRaises(ManifestError):
            manifest(destinations=(ref("primary"), ref("primary"))).validate(
                DESTINATIONS, DATASETS, SCHEMA_VERSION
            )

    def test_missing_dataset_rejected(self):
        with self.assertRaises(ManifestError):
            manifest(datasets=(("content", "zfs-1"),)).validate(
                DESTINATIONS, DATASETS, SCHEMA_VERSION
            )

    def test_duplicate_dataset_rejected(self):
        with self.assertRaises(ManifestError):
            manifest(datasets=(("content", "zfs-1"), ("content", "zfs-2"))).validate(
                DESTINATIONS, DATASETS, SCHEMA_VERSION
            )

    def test_empty_generation_id_rejected(self):
        with self.assertRaises(ManifestError):
            manifest(generation_id=" ").validate(DESTINATIONS, DATASETS, SCHEMA_VERSION)

    def test_schema_mismatch_rejected(self):
        with self.assertRaises(ManifestError):
            manifest().validate(DESTINATIONS, DATASETS, SCHEMA_VERSION + 1)

    def test_extra_destination_is_allowed(self):
        item = manifest(
            destinations=(ref("primary"), ref("secondary"), ref("archive"))
        )
        item.validate(DESTINATIONS, DATASETS, SCHEMA_VERSION)


class SelectGenerationTests(unittest.TestCase):
    def test_selects_newest_complete_compatible_generation(self):
        older = manifest(generation_id="books-1000", created_at=1_000.0)
        newest = manifest(generation_id="books-3000", created_at=3_000.0)
        selected = select_generation(
            [older, newest], DESTINATIONS, DATASETS, SCHEMA_VERSION, now=4_000.0
        )
        self.assertEqual(selected.generation_id, "books-3000")

    def test_incomplete_newer_generation_is_skipped(self):
        complete = manifest(generation_id="books-1000", created_at=1_000.0)
        partial = manifest(
            generation_id="books-3000", created_at=3_000.0, complete=False
        )
        selected = select_generation(
            [complete, partial], DESTINATIONS, DATASETS, SCHEMA_VERSION
        )
        self.assertEqual(selected.generation_id, "books-1000")

    def test_incompatible_schema_is_skipped(self):
        compatible = manifest(generation_id="books-1000", created_at=1_000.0)
        newer = manifest(
            generation_id="books-3000", created_at=3_000.0, schema_version=2
        )
        selected = select_generation(
            [compatible, newer], DESTINATIONS, DATASETS, SCHEMA_VERSION
        )
        self.assertEqual(selected.generation_id, "books-1000")

    def test_generation_missing_a_required_destination_is_skipped(self):
        complete = manifest(generation_id="books-1000", created_at=1_000.0)
        partial = manifest(
            generation_id="books-3000",
            created_at=3_000.0,
            destinations=(ref("primary"),),
        )
        selected = select_generation(
            [complete, partial], DESTINATIONS, DATASETS, SCHEMA_VERSION
        )
        self.assertEqual(selected.generation_id, "books-1000")

    def test_future_generation_is_ignored_when_now_is_given(self):
        available = manifest(generation_id="books-1000", created_at=1_000.0)
        future = manifest(generation_id="books-9000", created_at=9_000.0)
        selected = select_generation(
            [available, future], DESTINATIONS, DATASETS, SCHEMA_VERSION, now=5_000.0
        )
        self.assertEqual(selected.generation_id, "books-1000")

    def test_restore_error_when_nothing_is_compatible(self):
        with self.assertRaises(RestoreError):
            select_generation(
                [manifest(complete=False)],
                DESTINATIONS,
                DATASETS,
                SCHEMA_VERSION,
            )
        with self.assertRaises(RestoreError):
            select_generation([], DESTINATIONS, DATASETS, SCHEMA_VERSION)


if __name__ == "__main__":
    unittest.main()
