"""Recovery portal generation: selection, escaping, and atomic writes.

Nothing here touches a destination, a Restic repository, a network, or a live
dataset. Manifests are constructed in memory and written to temporary
directories.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from data_plane.backup.generations import (
    GenerationManifest,
    SnapshotRef,
    manifest_hash,
)
from data_plane.errors import ValidationError
from data_plane.recovery.portal import (
    PortalError,
    build_entries,
    render_html,
    write_portal,
)

DESTINATIONS = ("primary", "secondary")
DATASETS = ("content", "events")
SCHEMA_VERSION = 3
NOW = 100_000.0


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
        dependencies=("events",),
        destinations=(ref("primary"), ref("secondary")),
        complete=True,
        schema_version=SCHEMA_VERSION,
        release="2026.11",
    )
    values.update(overrides)
    return GenerationManifest(**values)


def entries_for(*manifests: GenerationManifest, **kwargs):
    return build_entries(
        manifests,
        kwargs.pop("destinations", DESTINATIONS),
        kwargs.pop("datasets", DATASETS),
        kwargs.pop("schema_version", SCHEMA_VERSION),
        **kwargs,
    )


class BuildEntriesTests(unittest.TestCase):
    def test_selects_the_newest_complete_generation(self):
        older = manifest(generation_id="books-1000", created_at=1_000.0)
        newest = manifest(generation_id="books-3000", created_at=3_000.0)
        (entry,) = entries_for(older, newest, now=4_000.0)
        self.assertEqual(entry.group_id, "books")
        self.assertEqual(entry.generation_id, "books-3000")
        self.assertTrue(entry.complete)
        self.assertEqual(entry.manifest_hash, manifest_hash(newest))
        self.assertEqual(entry.destinations, newest.destinations)
        self.assertEqual(entry.datasets, newest.datasets)
        self.assertEqual(entry.sqlite, newest.sqlite)

    def test_incomplete_newer_generation_is_ignored_and_warned(self):
        complete = manifest(generation_id="books-1000", created_at=1_000.0)
        partial = manifest(
            generation_id="books-3000",
            created_at=3_000.0,
            complete=False,
        )
        (entry,) = entries_for(complete, partial)
        self.assertEqual(entry.generation_id, "books-1000")
        self.assertTrue(entry.complete)
        self.assertTrue(any("incomplete" in warning for warning in entry.warnings))
        self.assertTrue(any("stale" in warning for warning in entry.warnings))

    def test_incompatible_schema_is_ignored(self):
        compatible = manifest(generation_id="books-1000", created_at=1_000.0)
        incompatible = manifest(
            generation_id="books-3000",
            created_at=3_000.0,
            schema_version=SCHEMA_VERSION + 1,
        )
        (entry,) = entries_for(compatible, incompatible)
        self.assertEqual(entry.generation_id, "books-1000")
        self.assertTrue(any("schema" in warning for warning in entry.warnings))

    def test_missing_destination_selects_older_and_is_reported(self):
        complete = manifest(generation_id="books-1000", created_at=1_000.0)
        short = manifest(
            generation_id="books-3000",
            created_at=3_000.0,
            destinations=(ref("primary"),),
        )
        (entry,) = entries_for(complete, short)
        self.assertEqual(entry.generation_id, "books-1000")
        combined = " ".join(entry.warnings)
        self.assertIn("missing destinations", combined)
        self.assertIn("secondary", combined)

    def test_missing_dataset_selects_older_and_is_reported(self):
        complete = manifest(generation_id="books-1000", created_at=1_000.0)
        short = manifest(
            generation_id="books-3000",
            created_at=3_000.0,
            datasets=(("content", "zfs-1"),),
        )
        (entry,) = entries_for(complete, short)
        self.assertEqual(entry.generation_id, "books-1000")
        self.assertIn("missing datasets", " ".join(entry.warnings))

    def test_partial_generation_is_never_presented_as_restorable(self):
        partial = manifest(complete=False)
        (entry,) = entries_for(partial)
        self.assertFalse(entry.complete)
        self.assertFalse(entry.restorable)
        self.assertEqual(entry.generation_id, "")
        self.assertEqual(entry.manifest_hash, "")
        self.assertEqual(entry.destinations, ())
        self.assertEqual(entry.datasets, ())
        self.assertEqual(entry.sqlite, ())
        self.assertIn("no complete compatible generation", " ".join(entry.warnings))
        self.assertIn("incomplete", " ".join(entry.warnings))

    def test_one_entry_per_group_in_sorted_order(self):
        books = manifest(generation_id="books-1000", group_id="books")
        photos = manifest(
            generation_id="photos-1000",
            group_id="photos",
            destinations=(ref("primary"), ref("secondary")),
            datasets=(("content", "p-1"), ("events", "p-2")),
        )
        entries = entries_for(books, photos)
        self.assertEqual([entry.group_id for entry in entries], ["books", "photos"])

    def test_future_generation_is_ignored_when_now_is_given(self):
        available = manifest(generation_id="books-1000", created_at=1_000.0)
        future = manifest(generation_id="books-9000", created_at=9_000.0)
        (entry,) = entries_for(available, future, now=5_000.0)
        self.assertEqual(entry.generation_id, "books-1000")
        self.assertIn("future", " ".join(entry.warnings))

    def test_old_selected_generation_gets_a_stale_age_warning(self):
        item = manifest(created_at=1_000.0)
        (entry,) = entries_for(item, now=1_000.0 + 40 * 24 * 60 * 60)
        self.assertTrue(any("stale" in warning for warning in entry.warnings))

    def test_unknown_object_is_rejected(self):
        with self.assertRaises(ValidationError):
            build_entries(
                [{"generation_id": "books-1000"}],
                DESTINATIONS,
                DATASETS,
                SCHEMA_VERSION,
            )

    def test_duplicate_requirements_are_rejected(self):
        with self.assertRaises(ValidationError):
            build_entries(
                [manifest()],
                ("primary", "primary"),
                DATASETS,
                SCHEMA_VERSION,
            )


class RenderTests(unittest.TestCase):
    def test_renders_group_generation_time_copies_datasets_and_hash(self):
        item = manifest()
        (entry,) = entries_for(item)
        page = render_html([entry])
        self.assertIn("books", page)
        self.assertIn("books-1000", page)
        self.assertIn("1970-01-01T00:16:40Z", page)
        self.assertIn("Destination copies", page)
        self.assertIn("snap-primary", page)
        self.assertIn("content", page)
        self.assertIn("zfs-1", page)
        self.assertIn("epoch 2", page)
        self.assertIn(manifest_hash(item), page)

    def test_hostile_group_and_dataset_values_are_escaped(self):
        hostile_group = '<script>alert("group")</script>'
        hostile_dataset = '"><img src=x onerror=alert(1)>'
        item = manifest(
            group_id=hostile_group,
            datasets=(("content", "zfs-1"), (hostile_dataset, "zfs-2")),
        )
        (entry,) = entries_for(item, datasets=("content",))
        page = render_html([entry])
        self.assertNotIn("<script>alert", page)
        self.assertNotIn("<img", page)
        self.assertNotIn('"><img', page)
        self.assertIn("&lt;script&gt;", page)
        self.assertIn("&lt;img", page)
        self.assertIn("&quot;&gt;&lt;img", page)

    def test_hostile_generation_and_warning_values_are_escaped(self):
        hostile = "<b onmouseover=alert(1)>b</b>"
        item = manifest(generation_id=hostile)
        (entry,) = entries_for(item)
        page = render_html([entry])
        self.assertNotIn(hostile, page)
        self.assertIn("&lt;b onmouseover=alert(1)&gt;", page)

    def test_page_has_no_script_or_remote_resource(self):
        (entry,) = entries_for(manifest())
        page = render_html([entry])
        for needle in (
            "http://",
            "https://",
            "<script",
            "<iframe",
            "<link",
            "<img",
            "<form",
            "src=",
            "href=",
            "javascript:",
            "onclick=",
        ):
            self.assertNotIn(needle, page)

    def test_empty_portal_is_still_self_contained(self):
        page = render_html([])
        self.assertIn("No generations have been published yet.", page)
        self.assertNotIn("<script", page)


class WritePortalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "portal"

    def test_writes_page_and_index(self):
        html_path, json_path = write_portal(
            self.root, [manifest()], DESTINATIONS, DATASETS, SCHEMA_VERSION
        )
        self.assertEqual(html_path, self.root / "index.html")
        self.assertEqual(json_path, self.root / "index.json")
        self.assertEqual(
            sorted(path.name for path in self.root.iterdir()),
            ["index.html", "index.json"],
        )
        page = html_path.read_text(encoding="utf-8")
        self.assertIn("books", page)
        payload = json.loads(json_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["schema_version"], SCHEMA_VERSION)
        self.assertEqual(len(payload["entries"]), 1)
        self.assertEqual(payload["entries"][0]["generation_id"], "books-1000")
        self.assertEqual(
            payload["entries"][0]["manifest_hash"], manifest_hash(manifest())
        )
        self.assertEqual(
            payload["entries"][0]["destinations"][0]["destination"], "primary"
        )

    def test_rewrite_replaces_both_files(self):
        write_portal(self.root, [manifest()], DESTINATIONS, DATASETS, SCHEMA_VERSION)
        newer = manifest(generation_id="books-3000", created_at=3_000.0)
        write_portal(self.root, [newer], DESTINATIONS, DATASETS, SCHEMA_VERSION)
        self.assertIn(
            "books-3000", (self.root / "index.html").read_text(encoding="utf-8")
        )
        payload = json.loads((self.root / "index.json").read_text(encoding="utf-8"))
        self.assertEqual(payload["entries"][0]["generation_id"], "books-3000")

    def test_failed_rename_leaves_the_previous_portal_intact(self):
        write_portal(self.root, [manifest()], DESTINATIONS, DATASETS, SCHEMA_VERSION)
        old_html = (self.root / "index.html").read_bytes()
        old_json = (self.root / "index.json").read_bytes()

        newer = manifest(generation_id="books-3000", created_at=3_000.0)
        with mock.patch(
            "data_plane.recovery.portal.os.replace", side_effect=OSError("boom")
        ):
            with self.assertRaises(PortalError):
                write_portal(
                    self.root, [newer], DESTINATIONS, DATASETS, SCHEMA_VERSION
                )

        self.assertEqual((self.root / "index.html").read_bytes(), old_html)
        self.assertEqual((self.root / "index.json").read_bytes(), old_json)
        self.assertEqual(
            sorted(path.name for path in self.root.iterdir()),
            ["index.html", "index.json"],
        )

    def test_bad_manifest_does_not_touch_existing_files(self):
        write_portal(self.root, [manifest()], DESTINATIONS, DATASETS, SCHEMA_VERSION)
        old_html = (self.root / "index.html").read_bytes()
        with self.assertRaises(ValidationError):
            write_portal(
                self.root, [object()], DESTINATIONS, DATASETS, SCHEMA_VERSION
            )
        self.assertEqual((self.root / "index.html").read_bytes(), old_html)


if __name__ == "__main__":
    unittest.main()
