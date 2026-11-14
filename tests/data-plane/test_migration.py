"""Migration gates, preflight evidence, data-loss verification, and rollback."""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from data_plane.migration import (
    PHASE_NAMES,
    DataLossError,
    GateBlocked,
    PreflightError,
    RollbackError,
    Snapshot,
    advance,
    apply_rollback,
    assert_no_data_loss,
    check_preflight,
    compare,
    compare_evidence,
    next_phase,
    plan_rollback,
)
from data_plane.migration.cli import main
from data_plane.migration.phases import (
    FRESH_BACKUP,
    RESTORE_VERIFIED,
    ROLLBACK_DISK_BOOT_TESTED,
    SSH_PATHS_INDEPENDENT,
)
from data_plane.migration.verify import SnapshotEntry, Tombstone

NOW = 1_800_000_000.0
CONFIRMED_AT = "2026-11-14T05:00:00+00:00"
DESTINATIONS = ("primary", "secondary", "r2")


def backup(
    destination: str,
    group: str = "notes",
    age_hours: float = 1.0,
    **overrides,
) -> dict[str, object]:
    values: dict[str, object] = {
        "group": group,
        "destination": destination,
        "created_at": NOW - age_hours * 3600.0,
        "verified": True,
        "complete": True,
    }
    values.update(overrides)
    return values


def ssh_path(name: str, kind: str = "direct", **overrides) -> dict[str, object]:
    values: dict[str, object] = {
        "name": name,
        "kind": kind,
        "verified": True,
        "independent_of_data_plane": True,
        "verified_at": NOW - 600.0,
    }
    values.update(overrides)
    return values


def evidence(**overrides) -> dict[str, object]:
    values: dict[str, object] = {
        "now": NOW,
        "required_destinations": list(DESTINATIONS),
        "required_groups": ["notes"],
        "backup_max_age_hours": 24,
        "restore_max_age_hours": 24,
        "ssh_max_age_hours": 24,
        "backups": [backup(destination) for destination in DESTINATIONS],
        "restore_verification": {
            "generation_id": "notes-1",
            "storage": "scratch",
            "verified": True,
            "verified_at": NOW - 3600.0,
        },
        "rollback_disk": {
            "device": "/dev/disk/by-id/nvme-Samsung_SSD_970_EVO_1TB",
            "present": True,
            "boot_tested": True,
            "converted": False,
        },
        "ssh_paths": [
            ssh_path("lan", "direct"),
            ssh_path("tunnel", "cloudflared-rescue"),
        ],
        "operator_confirmations": {
            "cutover": {"confirmed": True, "by": "smores", "at": CONFIRMED_AT},
            "retire_old": {"confirmed": True, "by": "smores", "at": CONFIRMED_AT},
        },
    }
    values.update(overrides)
    return values


def snapshot(name: str, entries, tombstones=()) -> Snapshot:
    return Snapshot(
        name=name,
        entries=tuple(SnapshotEntry(item_id, sha256) for item_id, sha256 in entries),
        tombstones=tuple(Tombstone(item_id, audited) for item_id, audited in tombstones),
    )


SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64


def rollback_evidence(**overrides) -> dict[str, object]:
    values: dict[str, object] = {
        "new_writer_units": [
            "homelab-backup-controller.service",
            "homelab-event-service.service",
        ],
        "old_boot_entry": "nixos-old",
        "old_generation": {
            "generation_id": "old-notes-7",
            "manifest": "/root/old-generation.json",
            "verified": True,
        },
        "rescue_ssh_units": ["ssh.service"],
        "ssh_paths": [ssh_path("lan"), ssh_path("tunnel", "cloudflared-rescue")],
        "rollback_disk": {"converted": False},
    }
    values.update(overrides)
    return values


class PhaseTest(unittest.TestCase):
    def test_phases_are_ordered_and_terminal(self):
        self.assertEqual(
            PHASE_NAMES,
            (
                "inventory",
                "protect_old",
                "pilot",
                "scratch_stack",
                "shadow_backfill",
                "cutover",
                "retire_old",
            ),
        )
        self.assertEqual(next_phase("inventory").name, "protect_old")
        self.assertIsNone(next_phase("retire_old"))

    def test_protect_old_needs_every_gate(self):
        with self.assertRaises(GateBlocked) as raised:
            advance("protect_old", {})
        missing = {key for failure in raised.exception.failures for key in failure.missing}
        self.assertEqual(
            missing,
            {
                FRESH_BACKUP,
                RESTORE_VERIFIED,
                ROLLBACK_DISK_BOOT_TESTED,
                SSH_PATHS_INDEPENDENT,
            },
        )

    def test_protect_old_advances_with_preflight_evidence(self):
        report = check_preflight(evidence()).require()
        self.assertEqual(advance("protect_old", report.evidence).name, "pilot")

    def test_cutover_needs_a_confirmation_not_just_evidence(self):
        report = check_preflight(evidence()).require()
        complete = {
            key: value
            for key, value in report.evidence.items()
            if key != "operator_confirmations"
        }
        complete["cutover_rehearsal_passed"] = True
        with self.assertRaises(GateBlocked) as raised:
            advance("cutover", complete)
        self.assertTrue(
            any("confirmation" in failure.detail for failure in raised.exception.failures)
        )
        self.assertEqual(
            advance(
                "cutover",
                dict(complete, operator_confirmations=evidence()["operator_confirmations"]),
            ).name,
            "retire_old",
        )

    def test_preflight_report_feeds_the_cutover_and_retirement_gates(self):
        full = evidence(
            cutover_rehearsal_passed=True,
            retired_writers_stopped=True,
            new_generation_verified=True,
            rollback_no_longer_required=True,
        )
        report = check_preflight(full).require()
        self.assertEqual(advance("cutover", report.evidence).name, "retire_old")
        self.assertIsNone(advance("retire_old", report.evidence))

    def test_retire_old_refuses_without_confirmation(self):
        gate_evidence = {
            "retired_writers_stopped": True,
            "new_generation_verified": True,
            "rollback_no_longer_required": True,
        }
        with self.assertRaises(GateBlocked):
            advance("retire_old", gate_evidence)


class PreflightTest(unittest.TestCase):
    def test_happy_path(self):
        report = check_preflight(evidence())
        self.assertTrue(report.ok, report.to_dict())
        for key in (
            FRESH_BACKUP,
            RESTORE_VERIFIED,
            ROLLBACK_DISK_BOOT_TESTED,
            SSH_PATHS_INDEPENDENT,
            "operator_confirmation",
        ):
            self.assertTrue(report.evidence[key], key)
        self.assertEqual(
            report.checked,
            (
                "backup_freshness",
                "restore_verification",
                "rollback_disk",
                "ssh_paths",
                "operator_confirmation",
            ),
        )
        self.assertIs(report.require(), report)

    def test_stale_backup_fails_every_destination(self):
        report = check_preflight(
            evidence(
                backups=[
                    backup("primary"),
                    backup("secondary", age_hours=30.0),
                    backup("r2"),
                ]
            )
        )
        self.assertFalse(report.ok)
        failures = [item for item in report.failures if item.check == "backup_freshness"]
        self.assertEqual(
            [item.subject for item in failures], ["secondary/notes"]
        )
        self.assertIn("stale", failures[0].detail)

    def test_unverified_backup_does_not_count(self):
        report = check_preflight(
            evidence(
                backups=[
                    backup("primary"),
                    backup("secondary", verified=False),
                    backup("r2"),
                ]
            )
        )
        self.assertFalse(report.ok)
        details = " ".join(item.detail for item in report.failures)
        self.assertIn("not verified", details)

    def test_unverified_restore_fails(self):
        report = check_preflight(
            evidence(
                restore_verification={
                    "generation_id": "notes-1",
                    "storage": "scratch",
                    "verified": False,
                    "verified_at": NOW - 600.0,
                }
            )
        )
        self.assertFalse(report.ok)
        self.assertTrue(
            any(item.check == "restore_verification" for item in report.failures)
        )

    def test_restore_must_be_on_scratch_storage(self):
        report = check_preflight(
            evidence(
                restore_verification={
                    "generation_id": "notes-1",
                    "storage": "production",
                    "verified": True,
                    "verified_at": NOW - 600.0,
                }
            )
        )
        self.assertFalse(report.ok)
        self.assertIn(
            "scratch",
            " ".join(item.detail for item in report.failures),
        )

    def test_missing_second_ssh_path_fails(self):
        report = check_preflight(evidence(ssh_paths=[ssh_path("lan")]))
        self.assertFalse(report.ok)
        self.assertIn(
            "at least two",
            " ".join(
                item.detail
                for item in report.failures
                if item.check == "ssh_paths"
            ),
        )

    def test_data_plane_dependent_ssh_path_does_not_count(self):
        report = check_preflight(
            evidence(
                ssh_paths=[
                    ssh_path("lan"),
                    ssh_path("tunnel", independent_of_data_plane=False),
                ]
            )
        )
        self.assertFalse(report.ok)
        self.assertIn(
            "independent of k3s",
            " ".join(item.detail for item in report.failures),
        )

    def test_rollback_disk_must_be_present_and_boot_tested(self):
        report = check_preflight(
            evidence(rollback_disk={"device": "/dev/nvme1n1", "present": False})
        )
        self.assertFalse(report.ok)
        details = [item for item in report.failures if item.check == "rollback_disk"]
        self.assertEqual(len(details), 2)

    def test_irreversible_phase_without_confirmation_fails(self):
        confirmations = {"cutover": evidence()["operator_confirmations"]["cutover"]}
        report = check_preflight(evidence(operator_confirmations=confirmations))
        self.assertFalse(report.ok)
        subjects = [
            item.subject
            for item in report.failures
            if item.check == "operator_confirmation"
        ]
        self.assertEqual(subjects, ["retire_old"])

    def test_require_raises_with_all_failures(self):
        report = check_preflight(evidence(ssh_paths=[]))
        with self.assertRaises(PreflightError) as raised:
            report.require()
        self.assertIs(raised.exception.failures, report.failures)

    def test_malformed_evidence_is_structured(self):
        report = check_preflight({"now": NOW})
        self.assertFalse(report.ok)
        checks = {item.check for item in report.failures}
        self.assertIn("backup_freshness", checks)
        self.assertIn("restore_verification", checks)
        self.assertIn("ssh_paths", checks)


class VerifyTest(unittest.TestCase):
    def source(self) -> Snapshot:
        return snapshot(
            "source",
            [("a", SHA_A), ("b", SHA_B)],
            [("c", True)],
        )

    def replica(self) -> Snapshot:
        return snapshot(
            "replica",
            [("a", SHA_A), ("b", SHA_B)],
            [("c", True)],
        )

    def test_matching_snapshots_have_no_data_loss(self):
        comparison = assert_no_data_loss(self.source(), self.replica())
        self.assertTrue(comparison.ok)
        self.assertFalse(comparison.count_drift)
        self.assertEqual(comparison.source_count, comparison.replica_count)

    def test_missing_id_fails(self):
        replica = snapshot("replica", [("a", SHA_A)], [("c", True)])
        with self.assertRaises(DataLossError) as raised:
            assert_no_data_loss(self.source(), replica)
        self.assertEqual(raised.exception.comparison.missing_ids, ("b",))

    def test_count_drift_fails(self):
        replica = snapshot(
            "replica",
            [("a", SHA_A), ("b", SHA_B), ("z", SHA_C)],
            [("c", True)],
        )
        comparison = compare(self.source(), replica)
        self.assertTrue(comparison.count_drift)
        self.assertEqual(comparison.extra_ids, ("z",))
        self.assertFalse(comparison.ok)

    def test_hash_mismatch_fails(self):
        replica = snapshot("replica", [("a", SHA_A), ("b", SHA_C)], [("c", True)])
        comparison = compare(self.source(), replica)
        self.assertFalse(comparison.ok)
        self.assertEqual(
            comparison.hash_mismatches,
            (("b", SHA_B, SHA_C),),
        )
        with self.assertRaises(DataLossError):
            assert_no_data_loss(self.source(), replica)

    def test_unaudited_tombstone_fails(self):
        source = snapshot("source", [("a", SHA_A)], [("c", False)])
        replica = snapshot("replica", [("a", SHA_A)], [("c", True)])
        with self.assertRaises(DataLossError) as raised:
            assert_no_data_loss(source, replica)
        self.assertEqual(
            raised.exception.comparison.unaudited_tombstones,
            ("source:c",),
        )

    def test_unpropagated_and_resurrected_tombstones_fail(self):
        source = snapshot("source", [("a", SHA_A)], [("c", True)])
        replica = snapshot("replica", [("a", SHA_A), ("c", SHA_C)], [])
        comparison = compare(source, replica)
        self.assertFalse(comparison.ok)
        self.assertEqual(comparison.missing_tombstones, ("c",))
        self.assertEqual(comparison.resurrected_ids, ("c",))

    def test_invalid_hash_is_not_a_pass(self):
        comparison = compare(
            snapshot("source", [("a", SHA_A)]),
            snapshot("replica", [("a", "not-a-hash")]),
        )
        self.assertFalse(comparison.ok)
        self.assertEqual(comparison.invalid_hashes, ("replica:a",))

    def test_compare_evidence_round_trip(self):
        comparison = compare_evidence(
            {"source": self.source().to_dict(), "replica": self.replica().to_dict()}
        )
        self.assertTrue(comparison.ok)


class RollbackTest(unittest.TestCase):
    def test_plan_is_ordered_and_dry_by_default(self):
        plan = plan_rollback(rollback_evidence())
        self.assertTrue(plan.dry_run)
        self.assertEqual(
            [step.name for step in plan.steps],
            [
                "stop_new_writers",
                "restore_old_boot_target",
                "restore_old_generation",
                "enable_rescue_ssh",
            ],
        )
        self.assertEqual(
            plan.commands()[1],
            ["bootctl", "set-default", "nixos-old"],
        )
        self.assertEqual(
            plan.steps[2].environment,
            (
                ("HOMELAB_GENERATION_MANIFEST", "/root/old-generation.json"),
                ("HOMELAB_RESTORE_CONFIRM", "restore"),
                ("HOMELAB_RESTORE_APPLY", "1"),
            ),
        )
        self.assertTrue(plan.steps[2].irreversible)

    def test_plan_refuses_unverified_generation(self):
        bad = rollback_evidence(
            old_generation={
                "generation_id": "old-notes-7",
                "manifest": "/root/old-generation.json",
                "verified": False,
            }
        )
        with self.assertRaises(RollbackError):
            plan_rollback(bad)

    def test_plan_refuses_after_the_970_is_converted(self):
        with self.assertRaises(RollbackError):
            plan_rollback(rollback_evidence(rollback_disk={"converted": True}))

    def test_apply_needs_non_dry_plan_and_literal_confirmation(self):
        dry = plan_rollback(rollback_evidence())
        with self.assertRaises(RollbackError):
            apply_rollback(dry, object(), "rollback")

        executable = plan_rollback(rollback_evidence(), dry_run=False)
        with self.assertRaises(RollbackError):
            apply_rollback(executable, object(), "yes")

        calls = []

        class Backend:
            def run(self, argv, *, environment=None):
                calls.append((list(argv), dict(environment or {})))

        executed = apply_rollback(executable, Backend(), "rollback")
        self.assertEqual(executed[-1], "enable_rescue_ssh")
        self.assertEqual(
            calls[2][1]["HOMELAB_GENERATION_MANIFEST"],
            "/root/old-generation.json",
        )

    def test_fewer_than_two_verified_ssh_paths_warns(self):
        plan = plan_rollback(rollback_evidence(ssh_paths=[ssh_path("lan")]))
        self.assertTrue(any("fewer than two" in item for item in plan.warnings))


class CliTest(unittest.TestCase):
    def run_cli(self, *argv):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(list(argv))
        return code, stdout.getvalue(), stderr.getvalue()

    def write(self, directory: Path, name: str, payload) -> Path:
        path = directory / name
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_preflight_success_and_failure(self):
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            good = self.write(directory, "good.json", evidence())
            bad = self.write(directory, "bad.json", evidence(ssh_paths=[]))
            code, out, _ = self.run_cli("preflight", str(good))
            self.assertEqual(code, 0)
            self.assertTrue(json.loads(out)["ok"])
            code, out, _ = self.run_cli("preflight", str(bad))
            self.assertEqual(code, 1)
            self.assertFalse(json.loads(out)["ok"])

    def test_verify_reports_data_loss(self):
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            bad = self.write(
                directory,
                "verify.json",
                {
                    "source": snapshot("source", [("a", SHA_A)]).to_dict(),
                    "replica": snapshot("replica", []).to_dict(),
                },
            )
            code, out, _ = self.run_cli("verify", str(bad))
            self.assertEqual(code, 1)
            self.assertEqual(json.loads(out)["missing_ids"], ["a"])

    def test_rollback_plan_is_a_dry_run(self):
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            path = self.write(directory, "rollback.json", rollback_evidence())
            code, out, _ = self.run_cli("rollback-plan", str(path))
            self.assertEqual(code, 0)
            payload = json.loads(out)
            self.assertTrue(payload["dry_run"])
            self.assertEqual(len(payload["steps"]), 4)

    def test_invalid_evidence_is_an_input_error(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "broken.json"
            path.write_text("{", encoding="utf-8")
            code, _, err = self.run_cli("preflight", str(path))
            self.assertEqual(code, 2)
            self.assertIn("not valid JSON", err)


if __name__ == "__main__":
    unittest.main()
