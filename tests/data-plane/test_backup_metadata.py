"""Checkpoint metadata: populated in the manifest, rejected when incomplete."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from data_plane.backup.controller import (
    FanoutController,
    GroupMetadata,
    MetadataProvider,
)
from data_plane.backup.destinations import InMemoryDestination
from data_plane.backup.generations import read_manifest
from data_plane.errors import ManifestError

NOW = 1_000.0
SNAPSHOT = {"tank/books": "tank/books@data-plane-19700101T000000Z"}


class FakeSnapshotter:
    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []

    def fence(self, group_id: str) -> None:
        self.events.append(("fence", group_id))

    def checkpoint(self, group_id: str) -> None:
        self.events.append(("checkpoint", group_id))

    def snapshot(self, group_id: str) -> dict[str, str]:
        self.events.append(("snapshot", group_id))
        return dict(SNAPSHOT)

    def resume(self, group_id: str) -> None:
        self.events.append(("resume", group_id))


class StaticMetadata:
    def __init__(self, value):
        self.value = value
        self.groups: list[str] = []

    def collect(self, group_id: str):
        self.groups.append(group_id)
        if isinstance(self.value, Exception):
            raise self.value
        return self.value


def build(tmp: str, metadata=None, declared=None) -> FanoutController:
    return FanoutController(
        FakeSnapshotter(),
        [InMemoryDestination("primary", clock=lambda: NOW)],
        Path(tmp) / "manifests",
        clock=lambda: NOW,
        metadata_provider=metadata,
        declared_databases=declared,
    )


class MetadataTests(unittest.TestCase):
    def test_metadata_is_populated_in_the_published_manifest(self):
        metadata = StaticMetadata(
            GroupMetadata(
                sqlite=(("events.sqlite", 7, 42),),
                content_hashes=(("books/index.json", "a" * 64),),
                app_versions=(("opdshelf", "1.4.0"),),
                dependencies=("data-plane-state",),
            )
        )
        with tempfile.TemporaryDirectory() as tmp:
            manifest = build(
                tmp, metadata, declared={"books": ("events.sqlite",)}
            ).run("books", ("primary",), now=NOW)

            self.assertEqual(manifest.sqlite, (("events.sqlite", 7, 42),))
            self.assertEqual(
                manifest.content_hashes, (("books/index.json", "a" * 64),)
            )
            self.assertEqual(manifest.app_versions, (("opdshelf", "1.4.0"),))
            self.assertEqual(manifest.dependencies, ("data-plane-state",))
            self.assertEqual(metadata.groups, ["books"])
            stored = read_manifest(
                Path(tmp) / "manifests" / f"{manifest.generation_id}.json"
            )
            self.assertEqual(stored, manifest)

    def test_declared_database_without_checkpoint_metadata_is_rejected(self):
        metadata = StaticMetadata(GroupMetadata())
        with tempfile.TemporaryDirectory() as tmp:
            controller = build(
                tmp, metadata, declared={"books": ("events.sqlite",)}
            )
            with self.assertRaises(ManifestError) as caught:
                controller.run("books", ("primary",), now=NOW)

            self.assertIn("events.sqlite", str(caught.exception))
            self.assertEqual(
                controller._snapshotter.events,
                [
                    ("fence", "books"),
                    ("checkpoint", "books"),
                    ("resume", "books"),
                ],
            )
            self.assertFalse((Path(tmp) / "manifests").exists())

    def test_declared_database_with_metadata_succeeds(self):
        metadata = StaticMetadata(GroupMetadata(sqlite=(("events.sqlite", 1, 1),)))
        with tempfile.TemporaryDirectory() as tmp:
            manifest = build(
                tmp, metadata, declared={"books": ("events.sqlite",)}
            ).run("books", ("primary",), now=NOW)
            self.assertEqual(manifest.sqlite, (("events.sqlite", 1, 1),))

    def test_declared_database_without_a_provider_is_rejected_before_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            controller = build(tmp, None, declared={"books": ("events.sqlite",)})
            with self.assertRaises(ManifestError):
                controller.run("books", ("primary",), now=NOW)
            self.assertEqual(
                controller._snapshotter.events,
                [
                    ("fence", "books"),
                    ("checkpoint", "books"),
                    ("resume", "books"),
                ],
            )

    def test_provider_mapping_is_accepted(self):
        metadata: MetadataProvider = StaticMetadata(
            {
                "sqlite": [["events.sqlite", "3", "9"]],
                "content_hashes": [["notes/x.md", "b" * 64]],
                "app_versions": [["syncthing", "1.29"]],
                "dependencies": ["data-plane-state"],
            }
        )
        with tempfile.TemporaryDirectory() as tmp:
            manifest = build(
                tmp, metadata, declared={"books": ("events.sqlite",)}
            ).run("books", ("primary",), now=NOW)
            self.assertEqual(manifest.sqlite, (("events.sqlite", 3, 9),))
            self.assertEqual(manifest.dependencies, ("data-plane-state",))

    def test_provider_failure_resumes_writers_and_publishes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            controller = build(tmp, StaticMetadata(RuntimeError("db locked")))
            with self.assertRaises(RuntimeError):
                controller.run("books", ("primary",), now=NOW)
            self.assertEqual(controller._snapshotter.events[-1], ("resume", "books"))
            self.assertFalse((Path(tmp) / "manifests").exists())

    def test_malformed_metadata_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            controller = build(tmp, StaticMetadata({"sqlite": [["only-a-name"]]}))
            with self.assertRaises(ManifestError):
                controller.run("books", ("primary",), now=NOW)


if __name__ == "__main__":
    unittest.main()
