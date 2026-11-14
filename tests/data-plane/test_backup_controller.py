"""Fan-out controller, destination independence, and freshness tests."""

from __future__ import annotations

import math
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from data_plane.backup.controller import FanoutController
from data_plane.backup.destinations import DirectoryDestination, InMemoryDestination
from data_plane.backup.generations import (
    GenerationManifest,
    SnapshotRef,
    read_manifest,
)
from data_plane.errors import ManifestError

NOW = 1_000.0
SCHEMA_VERSION = 3
SNAPSHOT = {"content": "zfs-1", "events": "zfs-2"}


class FakeSnapshotter:
    """Records the fence/checkpoint/snapshot/resume protocol."""

    def __init__(
        self, snapshot: dict[str, str] | None = None, fail_snapshot: bool = False
    ) -> None:
        self.events: list[tuple[str, str]] = []
        self.snapshot_ids = dict(SNAPSHOT if snapshot is None else snapshot)
        self.fail_snapshot = fail_snapshot

    def fence(self, group_id: str) -> None:
        self.events.append(("fence", group_id))

    def checkpoint(self, group_id: str) -> None:
        self.events.append(("checkpoint", group_id))

    def snapshot(self, group_id: str) -> dict[str, str]:
        self.events.append(("snapshot", group_id))
        if self.fail_snapshot:
            raise RuntimeError("snapshot failed")
        return dict(self.snapshot_ids)

    def resume(self, group_id: str) -> None:
        self.events.append(("resume", group_id))


def controller(
    tmp: str,
    snapshotter: FakeSnapshotter,
    destinations: list,
    **overrides,
) -> FanoutController:
    values = dict(
        clock=lambda: 10_000.0,
        schema_version=SCHEMA_VERSION,
        release="2026.11",
    )
    values.update(overrides)
    return FanoutController(
        snapshotter, destinations, Path(tmp) / "manifests", **values
    )


def ref(destination: str, created_at: float, **overrides) -> SnapshotRef:
    values = dict(
        destination=destination,
        snapshot_id=f"snap-{destination}",
        created_at=created_at,
        group_id="books",
        dataset="data/books",
        parent=None,
        verified=True,
    )
    values.update(overrides)
    return SnapshotRef(**values)


def stored_manifest(group_id: str, **overrides) -> GenerationManifest:
    values = dict(
        version=1,
        generation_id=f"{group_id}-1",
        group_id=group_id,
        created_at=1_000.0,
        datasets=(("content", "zfs-1"),),
        sqlite=(),
        app_versions=(),
        content_hashes=(),
        dependencies=(),
        destinations=(ref("primary", 1_000.0), ref("secondary", 400.0)),
        complete=True,
        schema_version=SCHEMA_VERSION,
        release="2026.11",
    )
    values.update(overrides)
    return GenerationManifest(**values)


class RunTests(unittest.TestCase):
    def test_success_writes_one_complete_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            snapshotter = FakeSnapshotter()
            primary = InMemoryDestination("primary", clock=lambda: NOW)
            secondary = InMemoryDestination("secondary", clock=lambda: NOW)
            manifest = controller(tmp, snapshotter, [primary, secondary]).run(
                "books", ("primary", "secondary"), now=NOW
            )

            self.assertTrue(manifest.complete)
            self.assertEqual(manifest.generation_id, "books-1000000")
            self.assertEqual(manifest.group_id, "books")
            self.assertEqual(
                [item.destination for item in manifest.destinations],
                ["primary", "secondary"],
            )
            self.assertEqual(
                [item.group_id for item in manifest.destinations], ["books", "books"]
            )
            manifest.validate(("primary", "secondary"), tuple(SNAPSHOT), SCHEMA_VERSION)

            files = list((Path(tmp) / "manifests").glob("*.json"))
            self.assertEqual(len(files), 1)
            self.assertEqual(read_manifest(files[0]), manifest)

    def test_one_snapshot_and_resume_before_fanout(self):
        with tempfile.TemporaryDirectory() as tmp:
            snapshotter = FakeSnapshotter()
            primary = InMemoryDestination("primary")
            secondary = InMemoryDestination("secondary")
            controller(tmp, snapshotter, [primary, secondary]).run(
                "books", ("primary", "secondary"), now=NOW
            )
            self.assertEqual(
                snapshotter.events,
                [
                    ("fence", "books"),
                    ("checkpoint", "books"),
                    ("snapshot", "books"),
                    ("resume", "books"),
                ],
            )
            self.assertEqual(len(primary.captures), 1)
            self.assertEqual(len(secondary.captures), 1)

    def test_each_destination_captures_independently(self):
        with tempfile.TemporaryDirectory() as tmp:
            snapshotter = FakeSnapshotter()
            primary = InMemoryDestination("primary")
            secondary = InMemoryDestination("secondary")
            controller(tmp, snapshotter, [primary, secondary]).run(
                "books", ("primary", "secondary"), now=NOW
            )

            self.assertEqual(primary.captures, [SNAPSHOT])
            self.assertEqual(secondary.captures, [SNAPSHOT])
            self.assertIsNot(primary.captures[0], secondary.captures[0])
            primary.captures[0]["content"] = "tampered"
            self.assertEqual(secondary.captures[0]["content"], "zfs-1")
            self.assertEqual(snapshotter.snapshot_ids["content"], "zfs-1")

    def test_failed_destination_resumes_and_publishes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            snapshotter = FakeSnapshotter()
            primary = InMemoryDestination("primary")
            secondary = InMemoryDestination("secondary", fail=True)
            with self.assertRaises(ManifestError) as caught:
                controller(tmp, snapshotter, [primary, secondary]).run(
                    "books", ("primary", "secondary"), now=NOW
                )

            message = str(caught.exception)
            self.assertIn("secondary", message)
            self.assertIn("partial captures: primary=primary:books-1000000", message)
            self.assertEqual(snapshotter.events[-1], ("resume", "books"))
            self.assertFalse((Path(tmp) / "manifests").exists())

    def test_unverified_capture_is_not_published(self):
        with tempfile.TemporaryDirectory() as tmp:
            snapshotter = FakeSnapshotter()
            primary = InMemoryDestination("primary")
            secondary = InMemoryDestination("secondary", verify_ok=False)
            with self.assertRaises(ManifestError):
                controller(tmp, snapshotter, [primary, secondary]).run(
                    "books", ("primary", "secondary"), now=NOW
                )
            self.assertFalse((Path(tmp) / "manifests").exists())

    def test_snapshot_failure_still_resumes(self):
        with tempfile.TemporaryDirectory() as tmp:
            snapshotter = FakeSnapshotter(fail_snapshot=True)
            primary = InMemoryDestination("primary")
            with self.assertRaises(RuntimeError):
                controller(tmp, snapshotter, [primary]).run(
                    "books", ("primary",), now=NOW
                )
            self.assertEqual(snapshotter.events[-1], ("resume", "books"))
            self.assertEqual(primary.captures, [])
            self.assertFalse((Path(tmp) / "manifests").exists())

    def test_unknown_required_destination_fails_before_fencing(self):
        with tempfile.TemporaryDirectory() as tmp:
            snapshotter = FakeSnapshotter()
            primary = InMemoryDestination("primary")
            with self.assertRaises(ManifestError):
                controller(tmp, snapshotter, [primary]).run(
                    "books", ("primary", "offsite"), now=NOW
                )
            self.assertEqual(snapshotter.events, [])


class DirectoryDestinationTests(unittest.TestCase):
    def test_capture_verify_and_generations(self):
        with tempfile.TemporaryDirectory() as tmp:
            destination = DirectoryDestination(
                tmp, "secondary", clock=lambda: NOW
            )
            captured = destination.capture("books-1", SNAPSHOT)

            self.assertEqual(destination.generations(), ["books-1"])
            self.assertTrue(destination.verify(captured))
            self.assertTrue((Path(tmp) / "books-1.json").is_file())
            self.assertFalse(
                destination.verify(replace(captured, snapshot_id="dir:other"))
            )
            self.assertEqual(
                destination.capture("books-2", SNAPSHOT).destination, "secondary"
            )
            self.assertEqual(destination.generations(), ["books-1", "books-2"])


class FreshnessTests(unittest.TestCase):
    def setUp(self):
        self.controller = FanoutController(
            FakeSnapshotter(), [], Path("unused-manifests")
        )
        self.manifests = {"books": [stored_manifest("books")]}

    def test_per_destination_lag(self):
        lag = self.controller.freshness(
            ["books"],
            self.manifests,
            now=10_000.0,
            required_destinations=("primary", "secondary"),
        )
        self.assertEqual(lag["books"]["primary"], 9_000.0)
        self.assertEqual(lag["books"]["secondary"], 9_600.0)

    def test_missing_destination_has_infinite_lag(self):
        lag = self.controller.freshness(
            ["books"],
            self.manifests,
            now=10_000.0,
            required_destinations=("primary", "r2"),
        )
        self.assertTrue(math.isinf(lag["books"]["r2"]))

    def test_group_without_manifests_is_unprotected(self):
        lag = self.controller.freshness(
            ["photos"], {}, now=10_000.0, required_destinations=("primary",)
        )
        self.assertTrue(math.isinf(lag["photos"]["primary"]))
        self.assertTrue(
            math.isinf(
                self.controller.oldest_unprotected(["photos"], {}, now=10_000.0)[
                    "photos"
                ]
            )
        )

    def test_incomplete_generations_do_not_count(self):
        incomplete = stored_manifest(
            "books",
            generation_id="books-2",
            created_at=9_000.0,
            complete=False,
            destinations=(ref("primary", 9_000.0), ref("secondary", 9_000.0)),
        )
        lag = self.controller.freshness(
            ["books"],
            {"books": [*self.manifests["books"], incomplete]},
            now=10_000.0,
            required_destinations=("primary", "secondary"),
        )
        self.assertEqual(lag["books"]["primary"], 9_000.0)

    def test_oldest_unprotected_uses_newest_complete_generation(self):
        self.assertEqual(
            self.controller.oldest_unprotected(
                ["books"], self.manifests, now=10_000.0
            )["books"],
            9_000.0,
        )


if __name__ == "__main__":
    unittest.main()
