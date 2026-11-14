"""Restore planning selects, verifies, and orders one complete generation."""

from __future__ import annotations

import unittest

from data_plane.backup.generations import GenerationManifest, SnapshotRef
from data_plane.errors import RestoreError, ValidationError
from data_plane.recovery import (
    RestorePlan,
    RestoreStep,
    execute,
    plan_restore,
)

GROUP = "notes"
SCHEMA_VERSION = 1
DESTINATION = "/mnt/restore"
REQUIRED_DESTINATIONS = ("nvme", "hdd")
REQUIRED_DATASETS = ("content", "tasks")
MOUNTS = {"content": "/srv/notes", "tasks": "/srv/notes/tasks"}

SHA_A = "a" * 64


def ref(destination: str = "nvme", **overrides) -> SnapshotRef:
    values = dict(
        destination=destination,
        snapshot_id=f"{destination}:snap-2",
        created_at=2000.0,
        group_id=GROUP,
        dataset="tank/data/notes",
        parent=None,
        verified=True,
    )
    values.update(overrides)
    return SnapshotRef(**values)


def manifest(generation_id: str = "gen-2", **overrides) -> GenerationManifest:
    values = dict(
        version=1,
        generation_id=generation_id,
        group_id=GROUP,
        created_at=2000.0,
        datasets=(
            ("content", "tank/data/notes@gen-2"),
            ("tasks", "tank/data/notes/tasks@gen-2"),
        ),
        sqlite=(("events", 4, 120),),
        content_hashes=(("/etc/data-plane/notes.toml", SHA_A),),
        dependencies=(),
        destinations=(ref("nvme"), ref("hdd")),
        complete=True,
        schema_version=SCHEMA_VERSION,
        release="2026.11",
    )
    values.update(overrides)
    return GenerationManifest(**values)


class Selector:
    """Records the call and returns one fixed manifest, like the shared selector."""

    def __init__(self, chosen: GenerationManifest | None = None) -> None:
        self.chosen = chosen
        self.calls: list[dict] = []

    def __call__(
        self,
        manifests,
        required_destinations,
        required_datasets,
        schema_version,
    ) -> GenerationManifest:
        self.calls.append(
            {
                "manifests": tuple(manifests),
                "required_destinations": tuple(required_destinations),
                "required_datasets": tuple(required_datasets),
                "schema_version": schema_version,
            }
        )
        return self.chosen if self.chosen is not None else tuple(manifests)[0]


def plan(selector: Selector | None = None, **overrides) -> RestorePlan:
    values = dict(
        group_id=GROUP,
        required_destinations=REQUIRED_DESTINATIONS,
        required_datasets=REQUIRED_DATASETS,
        schema_version=SCHEMA_VERSION,
        destination=DESTINATION,
        mounts=MOUNTS,
        selector=selector or Selector(manifest()),
    )
    values.update(overrides)
    return plan_restore([manifest()], **values)


class PlanSelectionTests(unittest.TestCase):
    def test_plan_verifies_and_returns_the_selected_generation(self) -> None:
        selector = Selector(manifest("gen-7"))

        result = plan(selector)

        self.assertEqual(result.generation_id, "gen-7")
        self.assertEqual(result.group_id, GROUP)
        self.assertEqual(result.schema_version, SCHEMA_VERSION)
        self.assertEqual(len(selector.calls), 1)
        self.assertEqual(selector.calls[0]["required_destinations"], REQUIRED_DESTINATIONS)
        self.assertEqual(selector.calls[0]["required_datasets"], REQUIRED_DATASETS)
        self.assertEqual(selector.calls[0]["schema_version"], SCHEMA_VERSION)

    def test_default_selection_prefers_the_newest_complete_generation(self) -> None:
        result = plan_restore(
            [
                manifest("gen-3", created_at=3000.0, complete=False),
                manifest("gen-2", created_at=2000.0),
                manifest("gen-1", created_at=1000.0),
            ],
            group_id=GROUP,
            required_destinations=REQUIRED_DESTINATIONS,
            required_datasets=REQUIRED_DATASETS,
            schema_version=SCHEMA_VERSION,
            destination=DESTINATION,
            mounts=MOUNTS,
        )

        self.assertEqual(result.generation_id, "gen-2")

    def test_all_incomplete_generations_are_rejected(self) -> None:
        with self.assertRaises(RestoreError):
            plan(Selector(manifest("gen-1", complete=False)))

    def test_an_incompatible_schema_is_rejected(self) -> None:
        with self.assertRaises(RestoreError):
            plan(Selector(manifest(schema_version=SCHEMA_VERSION + 1)))

    def test_other_groups_are_left_out_of_the_selection(self) -> None:
        with self.assertRaises(RestoreError):
            plan_restore(
                [manifest("photos-1", group_id="photos")],
                group_id=GROUP,
                required_destinations=REQUIRED_DESTINATIONS,
                required_datasets=REQUIRED_DATASETS,
                schema_version=SCHEMA_VERSION,
                destination=DESTINATION,
            )

    def test_the_selected_generation_must_belong_to_the_group(self) -> None:
        with self.assertRaises(RestoreError):
            plan(Selector(manifest("photos-1", group_id="photos")))

    def test_missing_required_destination_is_rejected(self) -> None:
        with self.assertRaises(RestoreError):
            plan(Selector(manifest(destinations=(ref("nvme"),))))

    def test_destination_without_a_snapshot_is_rejected(self) -> None:
        with self.assertRaises(RestoreError):
            plan(Selector(manifest(destinations=(ref("nvme"), ref("hdd", snapshot_id="")))))

    def test_missing_required_dataset_is_rejected(self) -> None:
        with self.assertRaises(RestoreError):
            plan(Selector(manifest(datasets=(("content", "tank/data/notes@gen-2"),))))

    def test_an_invalid_content_hash_is_rejected(self) -> None:
        with self.assertRaises(RestoreError):
            plan(Selector(manifest(content_hashes=(("/etc/notes.toml", "not-a-hash"),))))

    def test_selector_must_return_a_generation_manifest(self) -> None:
        with self.assertRaises(RestoreError):
            plan(Selector(chosen={"generation_id": "gen-1"}))

    def test_dependency_on_a_dataset_outside_the_restore_set_is_rejected(self) -> None:
        with self.assertRaises(RestoreError):
            plan(
                Selector(manifest(dependencies=("content",))),
                required_datasets=("tasks",),
            )

    def test_a_non_dataset_dependency_is_reported_as_a_warning(self) -> None:
        result = plan(Selector(manifest(dependencies=("config",))))

        self.assertTrue(any("'config'" in warning for warning in result.warnings))

    def test_mounts_must_be_absolute(self) -> None:
        with self.assertRaises(ValidationError):
            plan(mounts={"content": "srv/notes"})

    def test_destination_must_be_absolute(self) -> None:
        with self.assertRaises(ValidationError):
            plan(destination="relative/root")

    def test_required_inputs_must_be_present(self) -> None:
        with self.assertRaises(ValidationError):
            plan(required_datasets=())
        with self.assertRaises(ValidationError):
            plan(required_destinations=())
        with self.assertRaises(ValidationError):
            plan(required_datasets=("content", "content"))


class PlanStepTests(unittest.TestCase):
    def setUp(self) -> None:
        self.result = plan()

    def test_steps_are_ordered_datasets_config_then_sqlite(self) -> None:
        self.assertEqual(
            [step.kind for step in self.result.steps],
            ["dataset", "dataset", "config", "sqlite-restore", "sqlite-checkpoint"],
        )

    def test_parent_dataset_precedes_its_child(self) -> None:
        self.assertEqual(
            [step.target for step in self.result.steps[:2]],
            [f"{DESTINATION}/srv/notes", f"{DESTINATION}/srv/notes/tasks"],
        )

    def test_a_declared_dependency_is_restored_first(self) -> None:
        result = plan(Selector(manifest(dependencies=("tasks",))))

        self.assertEqual(result.steps[0].target, f"{DESTINATION}/srv/notes/tasks")

    def test_every_step_is_a_safe_argv_list(self) -> None:
        commands = self.result.commands()

        self.assertIsInstance(commands, list)
        for command in commands:
            self.assertIsInstance(command, list)
            self.assertTrue(command)
            self.assertTrue(all(isinstance(part, str) for part in command))
            self.assertNotIn("sh", command[0])
            self.assertNotIn("-c", command[:2])
            self.assertFalse(any("|" in part or "&&" in part for part in command))

    def test_dataset_step_streams_the_manifest_snapshot(self) -> None:
        step = self.result.steps[0]

        self.assertEqual(step.source, "tank/data/notes@gen-2")
        self.assertEqual(
            step.argv, ("zfs", "receive", "-F", "-u", f"{DESTINATION}/srv/notes")
        )

    def test_config_step_restores_from_the_first_required_destination(self) -> None:
        step = self.result.steps[2]

        self.assertEqual(step.target, f"{DESTINATION}/etc/data-plane/notes.toml")
        self.assertEqual(step.source, "nvme:snap-2")
        self.assertEqual(
            step.argv,
            (
                "restic",
                "restore",
                "nvme:snap-2",
                "--target",
                DESTINATION,
                "--include",
                "/etc/data-plane/notes.toml",
            ),
        )
        self.assertIn(SHA_A, step.notes)

    def test_sqlite_steps_restore_then_checkpoint_with_epoch_and_position(self) -> None:
        restore, checkpoint = self.result.steps[3:]

        self.assertEqual(
            restore.argv,
            (
                "sqlite3",
                f"{DESTINATION}/var/lib/events/events.db",
                f".restore {DESTINATION}/var/lib/events/events.db.checkpoint",
            ),
        )
        self.assertEqual(
            checkpoint.argv,
            (
                "sqlite3",
                f"{DESTINATION}/var/lib/events/events.db",
                "PRAGMA wal_checkpoint(TRUNCATE);",
            ),
        )
        self.assertIn("epoch 4", restore.notes)
        self.assertIn("position 120", restore.notes)

    def test_sqlite_path_can_be_overridden(self) -> None:
        result = plan(sqlite_paths={"events": "/var/lib/calendar/events.db"})

        self.assertEqual(
            result.steps[3].target, f"{DESTINATION}/var/lib/calendar/events.db"
        )

    def test_dataset_mounts_default_to_their_name(self) -> None:
        result = plan(mounts=None)

        self.assertEqual(result.steps[0].target, f"{DESTINATION}/content")

    def test_all_targets_stay_inside_the_destination(self) -> None:
        for step in self.result.steps:
            with self.subTest(kind=step.kind, target=step.target):
                self.assertTrue(step.target.startswith(DESTINATION + "/"))

    def test_an_optional_destination_is_reported_as_a_warning(self) -> None:
        result = plan(
            Selector(
                manifest(
                    destinations=(ref("nvme"), ref("hdd"), ref("r2", created_at=2100.0))
                )
            )
        )

        self.assertTrue(any("r2" in warning for warning in result.warnings))
        self.assertEqual(len(result.steps), 5)

    def test_a_generation_without_sqlite_or_content_has_only_dataset_steps(self) -> None:
        result = plan(Selector(manifest(sqlite=(), content_hashes=())))

        self.assertEqual([step.kind for step in result.steps], ["dataset", "dataset"])


class ExecuteTests(unittest.TestCase):
    class Backend:
        def __init__(self) -> None:
            self.calls: list[tuple[tuple[str, ...], str]] = []

        def run(self, argv, *, source: str = "") -> None:
            self.calls.append((tuple(argv), source))

    def test_dry_run_is_the_default(self) -> None:
        backend = self.Backend()

        with self.assertRaises(RestoreError):
            execute(plan(), backend)
        self.assertEqual(backend.calls, [])

    def test_execute_refuses_without_a_backend(self) -> None:
        with self.assertRaises(RestoreError):
            execute(plan(), None, apply=True)

    def test_execute_runs_every_step_in_order(self) -> None:
        backend = self.Backend()
        result = plan()

        targets = execute(result, backend, apply=True)

        self.assertEqual(len(backend.calls), len(result.steps))
        self.assertEqual(targets, tuple(step.target for step in result.steps))
        self.assertEqual(backend.calls[0][1], "tank/data/notes@gen-2")

    def test_execute_requires_an_explicit_apply(self) -> None:
        backend = self.Backend()

        for apply in (False, None, 0):
            with self.subTest(apply=apply):
                with self.assertRaises(RestoreError):
                    execute(plan(), backend, apply=apply)
        self.assertEqual(backend.calls, [])


class PlanTypeTests(unittest.TestCase):
    def test_plan_types_are_frozen(self) -> None:
        step = RestoreStep(
            kind="dataset", target="/mnt/restore/x", source="s@1", argv=("zfs",)
        )
        result = RestorePlan(
            group_id=GROUP, generation_id="gen-1", steps=(step,), warnings=()
        )

        with self.assertRaises(AttributeError):
            step.kind = "config"
        with self.assertRaises(AttributeError):
            result.generation_id = "gen-2"
        self.assertEqual(result.commands(), [["zfs"]])

    def test_step_rejects_an_unknown_kind(self) -> None:
        with self.assertRaises(ValidationError):
            RestoreStep(kind="shell", target="/x", source="", argv=("true",))

    def test_step_rejects_an_empty_argv(self) -> None:
        with self.assertRaises(ValidationError):
            RestoreStep(kind="dataset", target="/x", source="s@1", argv=())


if __name__ == "__main__":
    unittest.main()
