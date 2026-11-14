"""Real ZFS/restic adapters: argv shape, verification, and fan-out integration."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from data_plane.backup.controller import FanoutController
from data_plane.backup.restic import (
    CommandResult,
    RetentionPolicy,
    ResticDestination,
)
from data_plane.backup.zfs import ZfsSnapshotter
from data_plane.errors import ManifestError, PublishError

REPOSITORY = "s3:https://example.invalid/restic-backup"


class ScriptedRunner:
    def __init__(self, responses):
        self.responses = responses
        self.commands = []

    def run(self, command):
        self.commands.append(command)
        for predicate, result in self.responses:
            if predicate(command.argv):
                return result
        return CommandResult(0, "", "")


def restic(responses=None):
    return ResticDestination(
        name="nvme",
        repository=REPOSITORY,
        password_file="/run/secrets/restic-password",
        dataset_paths={
            "tank/books": "/srv/books/.zfs/snapshot/gen",
            "tank/books/content": "/srv/books/content/.zfs/snapshot/gen",
        },
        runner=ScriptedRunner(
            responses
            or [
                (lambda argv: "backup" in argv, CommandResult(0, '{"snapshot_id":"abc"}\n')),
                (lambda argv: "snapshots" in argv, CommandResult(0, '[{"id":"abc","tags":["data-plane:gen-1"]}]')),
            ]
        ),
        clock=lambda: 100.0,
    )


class ResticDestinationTests(unittest.TestCase):
    def test_capture_builds_one_backup_with_paths_and_no_copy(self):
        destination = restic()
        reference = destination.capture(
            "gen-1", {"tank/books": "tank/books@snap", "tank/books/content": "tank/books/content@snap"}
        )
        self.assertEqual(reference.snapshot_id, "abc")
        argvs = [command.argv for command in destination.runner.commands]
        self.assertTrue(any("backup" in argv for argv in argvs))
        self.assertFalse(any("copy" in argv for argv in argvs))
        backup = next(argv for argv in argvs if "backup" in argv)
        self.assertIn("--password-file", backup)
        self.assertIn("data-plane:gen-1", backup)
        self.assertIn("/srv/books/.zfs/snapshot/gen", backup)
        self.assertIn("/srv/books/content/.zfs/snapshot/gen", backup)

    def test_capture_requires_a_path_for_every_dataset(self):
        destination = restic()
        with self.assertRaises(PublishError):
            destination.capture("gen-1", {"tank/unknown": "tank/unknown@snap"})

    def test_verify_and_generations(self):
        destination = restic()
        reference = destination.capture("gen-1", {"tank/books": "tank/books@snap"})
        self.assertTrue(destination.verify(reference))
        self.assertEqual(destination.generations(), ["gen-1"])

    def test_verify_false_when_snapshot_missing(self):
        destination = restic(
            responses=[
                (lambda argv: "snapshots" in argv, CommandResult(0, '[{"id":"other"}]'))
            ]
        )
        reference = destination.capture("gen-1", {"tank/books": "tank/books@snap"})
        self.assertFalse(destination.verify(reference))

    def test_failure_raises_publish_error(self):
        destination = restic(
            responses=[(lambda argv: "backup" in argv, CommandResult(1, "", "boom"))]
        )
        with self.assertRaises(PublishError):
            destination.capture("gen-1", {"tank/books": "tank/books@snap"})

    def test_retention_is_host_and_tag_scoped(self):
        command = restic().retention_command(RetentionPolicy())
        self.assertIn("forget", command.argv)
        self.assertIn("--host", command.argv)
        self.assertIn("--tag", command.argv)
        self.assertIn("--keep-daily", command.argv)
        self.assertIn("14", command.argv)
        self.assertIn("--prune", command.argv)


class ZfsSnapshotterTests(unittest.TestCase):
    def test_snapshot_is_recursive_and_returns_every_member(self):
        runner = ScriptedRunner([])
        snapshotter = ZfsSnapshotter(
            group_datasets={"books": ("tank/books", "tank/books/content")},
            runner=runner,
            clock=lambda: 0.0,
            snapshot_prefix="dp",
        )
        mapping = snapshotter.snapshot("books")
        self.assertEqual(set(mapping), {"tank/books", "tank/books/content"})
        self.assertTrue(all(value.endswith("@dp-19700101T000000Z") for value in mapping.values()))
        self.assertEqual(runner.commands[0].argv[:3], ("zfs", "snapshot", "-r"))

    def test_sibling_members_each_get_a_recursive_snapshot(self):
        runner = ScriptedRunner([])
        snapshotter = ZfsSnapshotter(
            group_datasets={
                "state": (
                    "tank/data/events",
                    "tank/data/manifests",
                    "tank/data/events/wal",
                )
            },
            runner=runner,
            clock=lambda: 0.0,
            snapshot_prefix="dp",
        )
        mapping = snapshotter.snapshot("state")
        self.assertEqual(
            set(mapping),
            {"tank/data/events", "tank/data/manifests", "tank/data/events/wal"},
        )
        self.assertEqual(
            [command.argv for command in runner.commands],
            [
                ("zfs", "snapshot", "-r", "tank/data/events@dp-19700101T000000Z"),
                ("zfs", "snapshot", "-r", "tank/data/manifests@dp-19700101T000000Z"),
            ],
        )

    def test_hooks_run_and_fail_closed(self):
        runner = ScriptedRunner([])
        snapshotter = ZfsSnapshotter(
            group_datasets={"books": ("tank/books",)},
            runner=runner,
            fence_commands={"books": (__import__("data_plane.backup.restic", fromlist=["Command"]).Command(("systemctl", "stop", "books")),)},
            resume_commands={"books": (__import__("data_plane.backup.restic", fromlist=["Command"]).Command(("systemctl", "start", "books")),)},
        )
        snapshotter.fence("books")
        snapshotter.resume("books")
        self.assertEqual([command.argv[0] for command in runner.commands], ["systemctl", "systemctl"])

    def test_missing_group_raises(self):
        with self.assertRaises(PublishError):
            ZfsSnapshotter(group_datasets={}).snapshot("books")


class FanoutIntegrationTests(unittest.TestCase):
    def destination(self, name, snapshot_id, fail=False):
        responses = [
            (
                lambda argv: "backup" in argv,
                CommandResult(1, "", "boom") if fail else CommandResult(0, json.dumps({"snapshot_id": snapshot_id})),
            ),
            (
                lambda argv: "snapshots" in argv,
                CommandResult(0, json.dumps([{"id": snapshot_id}])),
            ),
        ]
        return ResticDestination(
            name=name,
            repository=f"s3:https://example.invalid/{name}",
            password_file="/run/secrets/restic-password",
            dataset_paths={"tank/books": "/srv/books/.zfs/snapshot/gen"},
            runner=ScriptedRunner(responses),
            clock=lambda: 100.0,
        )

    def test_one_snapshot_fans_out_independently(self):
        with tempfile.TemporaryDirectory() as tmp:
            controller = FanoutController(
                snapshotter=ZfsSnapshotter(
                    group_datasets={"books": ("tank/books",)},
                    runner=ScriptedRunner([]),
                    clock=lambda: 100.0,
                ),
                destinations={
                    "nvme": self.destination("nvme", "snap-nvme"),
                    "r2": self.destination("r2", "snap-r2"),
                },
                manifest_store=tmp,
            )
            manifest = controller.run("books", ("nvme", "r2"), now=100.0)
            self.assertEqual(manifest.group_id, "books")
            self.assertEqual({ref.destination for ref in manifest.destinations}, {"nvme", "r2"})
            self.assertTrue(manifest.complete)
            self.assertTrue(Path(tmp, f"{manifest.generation_id}.json").exists())

    def test_partial_fanout_publishes_no_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            controller = FanoutController(
                snapshotter=ZfsSnapshotter(
                    group_datasets={"books": ("tank/books",)},
                    runner=ScriptedRunner([]),
                    clock=lambda: 100.0,
                ),
                destinations={
                    "nvme": self.destination("nvme", "snap-nvme"),
                    "r2": self.destination("r2", "snap-r2", fail=True),
                },
                manifest_store=tmp,
            )
            with self.assertRaises(ManifestError):
                controller.run("books", ("nvme", "r2"), now=100.0)
            self.assertEqual(list(Path(tmp).glob("*.json")), [])


if __name__ == "__main__":
    unittest.main()
