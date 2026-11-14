"""The deploy inventory and the Ansible backup vars cannot drift.

Both are rendered from the same group vars: host dataset names come from
``recovery_inventory``/``zfs_datasets`` in ``all.yml`` and backup policy from
``backup.yml``. This test fails if the checked-in JSON is stale or if the host
plan would be rejected by the factory.
"""

from __future__ import annotations

import importlib.util
import json
import unittest
from pathlib import Path

from data_plane.backup.factory import BackupConfig

ROOT = Path(__file__).resolve().parents[2]
RENDERER = ROOT / "scripts" / "render-recovery-groups.py"
INVENTORY = ROOT / "deploy" / "data-plane" / "config" / "recovery-groups.json"


def load_renderer():
    spec = importlib.util.spec_from_file_location("render_recovery_groups", RENDERER)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


render = load_renderer()
ALL_VARS, BACKUP_VARS = render.load_group_vars()


class InventoryContractTests(unittest.TestCase):
    def setUp(self):
        self.groups = render.build_recovery_groups(ALL_VARS, BACKUP_VARS)
        self.host_plan = render.build_host_plan(ALL_VARS, BACKUP_VARS)

    def test_checked_in_json_is_current(self):
        expected = json.dumps(self.groups, indent=2) + "\n"
        self.assertEqual(INVENTORY.read_text(encoding="utf-8"), expected)

    def test_renderer_check_mode_accepts_the_checked_in_file(self):
        self.assertEqual(render.main(["--check"]), 0)

    def test_group_ids_match_the_ansible_backup_groups_var(self):
        expected = render.expected_group_ids(ALL_VARS, BACKUP_VARS)
        self.assertEqual(
            [group["group_id"] for group in self.groups["groups"]], expected
        )

    def test_every_group_has_a_declared_repository(self):
        names = {repository["name"] for repository in BACKUP_VARS["backup_repositories"]}
        for group in self.groups["groups"]:
            self.assertTrue(group["destinations"], group["group_id"])
            self.assertLessEqual(set(group["destinations"]), names)

    def test_host_plan_is_a_valid_factory_config(self):
        config = BackupConfig.from_dict(self.host_plan)
        self.assertEqual(
            [group.id for group in config.groups],
            [group["group_id"] for group in self.groups["groups"]],
        )
        for group in config.groups:
            for path in group.dataset_paths.values():
                self.assertIn("{snapshot}", path)

    def test_dataset_names_come_from_zfs_datasets(self):
        pending = set(BACKUP_VARS["backup_pending_host_groups"])
        for group in self.groups["groups"]:
            if group["group_id"] in pending:
                continue
            key = group["group_id"].replace("-", "_")
            self.assertIn(key, ALL_VARS["recovery_inventory"])
            expected = [
                row["name"]
                for row in ALL_VARS["zfs_datasets"]
                if row["recovery_group"] == key
            ]
            self.assertEqual(
                sorted(member["dataset"] for member in group["members"]),
                sorted(expected),
            )
            mounts = {row["name"]: row["mountpoint"] for row in ALL_VARS["zfs_datasets"]}
            for member in group["members"]:
                self.assertEqual(member["mount"], mounts[member["dataset"]])

    def test_pending_groups_are_not_in_the_host_inventory_yet(self):
        pending = list(BACKUP_VARS["backup_pending_host_groups"])
        self.assertTrue(pending)
        for group_id in pending:
            self.assertIn(group_id, [group["group_id"] for group in self.groups["groups"]])
            # The moment all.yml classifies it, the renderer refuses the stale
            # pending entry; this documents the migration path.
            self.assertNotIn(group_id.replace("-", "_"), ALL_VARS["recovery_inventory"])

    def test_every_mount_has_a_snapshot_path(self):
        paths = BACKUP_VARS["backup_dataset_paths"]
        for group in self.groups["groups"]:
            for member in group["members"]:
                path = paths[member["dataset"]]
                self.assertTrue(path.startswith("/"))
                self.assertIn("{snapshot}", path)

    def test_no_fixture_or_dry_run_backend_in_the_plan(self):
        text = json.dumps(self.host_plan) + json.dumps(self.groups)
        for forbidden in ("fixture", "InMemoryDestination", "DryRunRunner", "example.invalid"):
            self.assertNotIn(forbidden, text)
        for repository in self.host_plan["repositories"]:
            self.assertNotIn("--password", repository["password_file"])
            self.assertTrue(repository["password_file"].startswith("/"))

    def test_personal_and_general_data_never_share_a_repository(self):
        by_domain: dict[str, set[str]] = {}
        for repository in BACKUP_VARS["backup_repositories"]:
            by_domain.setdefault(repository["domain"], set()).add(repository["name"])
        domains = list(by_domain)
        for index, left in enumerate(domains):
            for right in domains[index + 1 :]:
                self.assertFalse(by_domain[left] & by_domain[right])


if __name__ == "__main__":
    unittest.main()
