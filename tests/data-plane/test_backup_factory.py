"""Factory: JSON plan -> real ZFS/restic controller, argv, fail-closed."""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from data_plane.backup.controller import GroupMetadata
from data_plane.backup.factory import (
    BackupConfig,
    BackupConfigError,
    build_controller,
    main,
    run_group,
)
from data_plane.backup.restic import CommandResult

NOW = 1_000.0


def plan(manifest_dir: str, **overrides):
    payload = {
        "schema_version": 1,
        "manifest_dir": manifest_dir,
        "groups": [
            {
                "id": "books",
                "datasets": ["tank/books"],
                "destinations": ["nvme", "hdd"],
                "dataset_paths": {
                    "tank/books": "/srv/books/.zfs/snapshot/{snapshot}"
                },
                "fence": [["systemctl", "stop", "opdshelf.service"]],
                "checkpoint": [
                    ["sqlite3", "/srv/books/catalog.db", "PRAGMA wal_checkpoint(TRUNCATE)"]
                ],
                "resume": [["systemctl", "start", "opdshelf.service"]],
            },
            {
                "id": "notes",
                "datasets": ["tank/notes"],
                "destinations": ["nvme", "r2"],
                "dataset_paths": {
                    "tank/notes": "/srv/notes/.zfs/snapshot/{snapshot}"
                },
                "cadence": "small",
            },
        ],
        "repositories": [
            {
                "name": "nvme",
                "repository": "/var/backups/primary/restic-nvme",
                "password_file": "/etc/homelab/secrets/restic/nvme.password",
                "host": "smortress",
                "tag": "data-plane",
            },
            {
                "name": "hdd",
                "repository": "/var/backups/secondary/restic-hdd",
                "password_file": "/etc/homelab/secrets/restic/hdd.password",
                "host": "smortress",
                "tag": "data-plane",
            },
            {
                "name": "r2",
                "repository": "s3:https://example.invalid/restic-r2",
                "password_file": "/etc/homelab/secrets/restic/r2.password",
                "host": "smortress",
                "tag": "data-plane",
            },
        ],
    }
    payload.update(overrides)
    return payload


class FakeRunner:
    def __init__(self) -> None:
        self.commands: list[tuple[str, ...]] = []

    def run(self, command):
        argv = command.argv
        self.commands.append(argv)
        if argv[:1] == ("zfs",):
            return CommandResult(0, "", "")
        if "backup" in argv:
            return CommandResult(0, '{"snapshot_id":"abc123"}\n')
        if "snapshots" in argv:
            return CommandResult(0, '[{"id":"abc123"}]')
        return CommandResult(0, "", "")

    def argvs(self, word: str) -> list[tuple[str, ...]]:
        return [argv for argv in self.commands if word in argv]

    def copy_commands(self) -> list[tuple[str, ...]]:
        return [argv for argv in self.commands if "copy" in argv]


class StaticMetadata:
    def collect(self, group_id: str):
        return GroupMetadata(sqlite=(("events.sqlite", 4, 10),))


class FactoryRunTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = BackupConfig.from_dict(plan(self.tmp.name))

    def test_one_snapshot_per_group_and_independent_destinations(self):
        runner = FakeRunner()
        controller = build_controller(self.config, runner=runner)
        manifest = run_group(controller, "books", self.config, now=NOW)

        self.assertEqual(manifest.group_id, "books")
        self.assertEqual(
            {ref.destination for ref in manifest.destinations}, {"nvme", "hdd"}
        )
        zfs = runner.argvs("snapshot")
        self.assertEqual(len(zfs), 1)
        self.assertEqual(zfs[0][:3], ("zfs", "snapshot", "-r"))
        self.assertTrue(zfs[0][3].startswith("tank/books@data-plane-"))
        snapshot_name = zfs[0][3].split("@", 1)[1]
        backups = runner.argvs("backup")
        self.assertEqual(len(backups), 2)
        self.assertEqual(
            {argv[argv.index("--repo") + 1] for argv in backups},
            {
                "/var/backups/primary/restic-nvme",
                "/var/backups/secondary/restic-hdd",
            },
        )
        for argv in backups:
            self.assertIn("--password-file", argv)
            self.assertIn("--host", argv)
            self.assertIn("data-plane:books-1000000", argv)
            self.assertIn(f"/srv/books/.zfs/snapshot/{snapshot_name}", argv)
            self.assertNotIn("--password", argv)
        self.assertEqual(runner.copy_commands(), [])

    def test_fence_checkpoint_and_resume_hooks_run_in_order(self):
        runner = FakeRunner()
        controller = build_controller(self.config, runner=runner)
        run_group(controller, "books", self.config, now=NOW)

        hooks = [argv for argv in runner.commands if argv[0] != "zfs" and argv[0] != "restic"]
        self.assertEqual(
            hooks,
            [
                ("systemctl", "stop", "opdshelf.service"),
                (
                    "sqlite3",
                    "/srv/books/catalog.db",
                    "PRAGMA wal_checkpoint(TRUNCATE)",
                ),
                ("systemctl", "start", "opdshelf.service"),
            ],
        )

    def test_groups_use_only_their_declared_destinations(self):
        runner = FakeRunner()
        controller = build_controller(self.config, runner=runner)
        run_group(controller, "notes", self.config, now=NOW)

        backups = runner.argvs("backup")
        repos = {argv[argv.index("--repo") + 1] for argv in backups}
        self.assertEqual(
            repos,
            {
                "/var/backups/primary/restic-nvme",
                "s3:https://example.invalid/restic-r2",
            },
        )

    def test_manifest_is_written_under_manifest_dir(self):
        controller = build_controller(self.config, runner=FakeRunner())
        manifest = run_group(controller, "books", self.config, now=NOW)
        self.assertTrue(
            Path(self.tmp.name, f"{manifest.generation_id}.json").is_file()
        )

    def test_metadata_provider_populates_the_manifest(self):
        controller = build_controller(
            self.config,
            runner=FakeRunner(),
            metadata_provider=StaticMetadata(),
        )
        manifest = run_group(controller, "books", self.config, now=NOW)
        self.assertEqual(manifest.sqlite, (("events.sqlite", 4, 10),))


class RefusalTests(unittest.TestCase):
    def config(self, tmp: str, **overrides) -> BackupConfig:
        return BackupConfig.from_dict(plan(tmp, **overrides))

    def test_group_without_a_destination_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = plan(tmp)
            payload["groups"][0]["destinations"] = []
            with self.assertRaises(BackupConfigError) as caught:
                BackupConfig.from_dict(payload)
            self.assertIn("destinations", str(caught.exception))

    def test_repository_without_a_password_file_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = plan(tmp)
            payload["repositories"][0]["password_file"] = ""
            with self.assertRaises(BackupConfigError) as caught:
                BackupConfig.from_dict(payload)
            self.assertIn("password_file", str(caught.exception))

    def test_missing_dataset_path_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = plan(tmp)
            payload["groups"][0]["dataset_paths"] = {}
            with self.assertRaises(BackupConfigError) as caught:
                BackupConfig.from_dict(payload)
            self.assertIn("no dataset path", str(caught.exception))

    def test_unknown_destination_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = plan(tmp)
            payload["groups"][0]["destinations"] = ["nvme", "glacier"]
            with self.assertRaises(BackupConfigError) as caught:
                BackupConfig.from_dict(payload)
            self.assertIn("glacier", str(caught.exception))

    def test_declared_database_without_metadata_provider_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = plan(tmp)
            payload["groups"][0]["databases"] = ["catalog.db"]
            config = self.config(tmp, groups=payload["groups"])
            with self.assertRaises(BackupConfigError) as caught:
                build_controller(config, runner=FakeRunner())
            self.assertIn("metadata provider", str(caught.exception))

    def test_conflicting_dataset_paths_are_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = plan(tmp)
            payload["groups"].append(
                {
                    "id": "books-copy",
                    "datasets": ["tank/books"],
                    "destinations": ["nvme"],
                    "dataset_paths": {"tank/books": "/elsewhere/{snapshot}"},
                }
            )
            with self.assertRaises(BackupConfigError):
                build_controller(self.config(tmp, groups=payload["groups"]))

    def test_invalid_json_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "backup.json"
            target.write_text("{not json", encoding="utf-8")
            with self.assertRaises(BackupConfigError):
                BackupConfig.load(target)


class ConfigTests(unittest.TestCase):
    def test_load_round_trips(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "backup.json"
            target.write_text(json.dumps(plan(tmp)), encoding="utf-8")
            config = BackupConfig.load(target)
            self.assertEqual(
                [group.id for group in config.groups], ["books", "notes"]
            )
            self.assertEqual(config.manifest_dir, tmp)

    def test_selection_by_group_and_cadence(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = BackupConfig.from_dict(plan(tmp))
            self.assertEqual(
                [group.id for group in config.selected(cadence="small")],
                ["notes"],
            )
            self.assertEqual(
                [group.id for group in config.selected(["books"])], ["books"]
            )
            with self.assertRaises(BackupConfigError):
                config.selected(["missing"])


class MainTests(unittest.TestCase):
    def test_list_groups_needs_no_runner(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "backup.json"
            target.write_text(json.dumps(plan(tmp)), encoding="utf-8")
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = main(["--config", str(target), "--list-groups"])
            self.assertEqual(code, 0)
            self.assertEqual(out.getvalue().split(), ["books", "notes"])

    def test_runs_every_group_and_prints_generation_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "backup.json"
            target.write_text(json.dumps(plan(tmp)), encoding="utf-8")
            runner = FakeRunner()
            controller = build_controller(
                BackupConfig.load(target), runner=runner
            )
            out = io.StringIO()
            with mock.patch(
                "data_plane.backup.factory.build_controller", return_value=controller
            ):
                with contextlib.redirect_stdout(out):
                    code = main(["--config", str(target), "--now", str(NOW)])
            self.assertEqual(code, 0)
            self.assertEqual(
                out.getvalue().split(), ["books-1000000", "notes-1000000"]
            )
            self.assertEqual(len(runner.argvs("snapshot")), 2)

    def test_bad_config_exits_two(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "backup.json"
            target.write_text("{}", encoding="utf-8")
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                code = main(["--config", str(target)])
            self.assertEqual(code, 2)
            self.assertIn("schema_version", err.getvalue())

    def test_one_failed_group_still_backs_up_the_rest_and_exits_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = plan(tmp)
            payload["groups"] = payload["groups"][:1]
            target = Path(tmp) / "backup.json"
            target.write_text(json.dumps(payload), encoding="utf-8")
            config = BackupConfig.load(target)
            with mock.patch(
                "data_plane.backup.factory.run_group",
                side_effect=RuntimeError("zfs busy"),
            ):
                with mock.patch(
                    "data_plane.backup.factory.build_controller",
                    return_value=build_controller(config, runner=FakeRunner()),
                ):
                    err = io.StringIO()
                    with contextlib.redirect_stderr(err):
                        code = main(["--config", str(target)])
            self.assertEqual(code, 1)
            self.assertIn("zfs busy", err.getvalue())


if __name__ == "__main__":
    unittest.main()
