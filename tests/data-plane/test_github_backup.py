"""GitHub backup plans emit safe argv and manifests verify real bundles."""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from data_plane.errors import ContractError, ManifestError, ValidationError
from data_plane.github import GithubBackupPlan, manifest, verify_bundle

GIT = shutil.which("git")


def entry(repo: str = "alpha", **overrides) -> dict:
    values = dict(
        repo=repo,
        commit="0" * 40,
        ref_count=7,
        bundle_size=2048,
        captured_at="2026-11-10T01:00:00+00:00",
    )
    values.update(overrides)
    return values


class PlanArgvTests(unittest.TestCase):
    def setUp(self) -> None:
        self.entered = tempfile.TemporaryDirectory()
        self.addCleanup(self.entered.cleanup)
        self.out_dir = Path(self.entered.name) / "github"

    def plan(self, **overrides) -> GithubBackupPlan:
        values = dict(
            owner="smores56",
            repos=("beta", "alpha"),
            out_dir=str(self.out_dir),
            mirror=True,
            bundle=True,
        )
        values.update(overrides)
        return GithubBackupPlan(**values)

    def test_commands_are_deterministic_safe_argv(self) -> None:
        commands = self.plan().commands()

        self.assertEqual(
            commands,
            [
                [
                    "git",
                    "clone",
                    "--mirror",
                    "https://github.com/smores56/alpha.git",
                    str(self.out_dir / "alpha.git"),
                ],
                [
                    "git",
                    "-C",
                    str(self.out_dir / "alpha.git"),
                    "bundle",
                    "create",
                    str(self.out_dir / "alpha.bundle"),
                    "--all",
                ],
                [
                    "git",
                    "clone",
                    "--mirror",
                    "https://github.com/smores56/beta.git",
                    str(self.out_dir / "beta.git"),
                ],
                [
                    "git",
                    "-C",
                    str(self.out_dir / "beta.git"),
                    "bundle",
                    "create",
                    str(self.out_dir / "beta.bundle"),
                    "--all",
                ],
            ],
        )

    def test_every_command_is_a_list_of_strings(self) -> None:
        for command in self.plan().commands():
            self.assertIsInstance(command, list)
            self.assertTrue(all(isinstance(part, str) for part in command))
            self.assertNotIn("sh", command[0])
            self.assertNotIn("-c", command[:2])

    def test_bundle_only_plans_refuse_to_run(self) -> None:
        with self.assertRaises(ContractError):
            self.plan(mirror=False).commands()

    def test_a_plan_with_no_outputs_is_rejected(self) -> None:
        with self.assertRaises(ContractError):
            self.plan(mirror=False, bundle=False)

    def test_owner_and_repos_are_validated(self) -> None:
        for repos in ((), ("alpha", "alpha"), ("../escape",), ("bad/name",), (".",)):
            with self.subTest(repos=repos):
                with self.assertRaises(ContractError):
                    self.plan(repos=repos)
        with self.assertRaises(ContractError):
            self.plan(owner="")
        with self.assertRaises(ContractError):
            self.plan(owner="smores56/other")

    def test_out_dir_must_be_absolute(self) -> None:
        with self.assertRaises(ContractError):
            self.plan(out_dir="github")

    def test_plan_is_frozen(self) -> None:
        plan = self.plan()
        with self.assertRaises(AttributeError):
            plan.owner = "other"


class ManifestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.entered = tempfile.TemporaryDirectory()
        self.addCleanup(self.entered.cleanup)
        self.path = Path(self.entered.name) / "manifest.json"

    def test_manifest_round_trip(self) -> None:
        written = manifest(self.path, [entry("beta"), entry("alpha")])

        self.assertEqual([item["repo"] for item in written], ["alpha", "beta"])
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["repos"], written)
        self.assertTrue(self.path.read_text(encoding="utf-8").endswith("\n"))

        self.assertEqual(manifest(self.path), written)

    def test_manifest_fills_a_missing_capture_time(self) -> None:
        written = manifest(self.path, [entry(captured_at="")])

        self.assertTrue(written[0]["captured_at"])
        self.assertEqual(manifest(self.path), written)

    def test_manifest_rejects_duplicate_repos(self) -> None:
        with self.assertRaises(ValidationError):
            manifest(self.path, [entry("alpha"), entry("alpha")])
        self.assertFalse(self.path.exists())

    def test_manifest_rejects_a_missing_field(self) -> None:
        payload = entry()
        del payload["bundle_size"]

        with self.assertRaises(ValidationError):
            manifest(self.path, [payload])

    def test_manifest_rejects_negative_counts(self) -> None:
        with self.assertRaises(ValidationError):
            manifest(self.path, [entry(ref_count=-1)])

    def test_manifest_rejects_a_bad_timestamp(self) -> None:
        with self.assertRaises(ValidationError):
            manifest(self.path, [entry(captured_at="yesterday")])

    def test_reading_a_missing_manifest_is_an_error(self) -> None:
        with self.assertRaises(ManifestError):
            manifest(self.path)

    def test_reading_a_malformed_manifest_is_an_error(self) -> None:
        self.path.write_text("{not json", encoding="utf-8")
        with self.assertRaises(ManifestError):
            manifest(self.path)


@unittest.skipUnless(GIT, "git is not installed")
class BundleVerificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.entered = tempfile.TemporaryDirectory()
        self.addCleanup(self.entered.cleanup)
        self.root = Path(self.entered.name)
        self.repo = self.root / "repo"
        self.bundle = self.root / "repo.bundle"

        subprocess.run([GIT, "init", "-q", str(self.repo)], check=True)
        subprocess.run(
            [
                GIT,
                "-C",
                str(self.repo),
                "-c",
                "user.email=test@example.com",
                "-c",
                "user.name=test",
                "commit",
                "-q",
                "--allow-empty",
                "-m",
                "init",
            ],
            check=True,
        )
        subprocess.run(
            [GIT, "-C", str(self.repo), "bundle", "create", str(self.bundle), "--all"],
            check=True,
        )

    def test_a_real_bundle_verifies(self) -> None:
        self.assertTrue(verify_bundle(self.bundle))

    def test_a_corrupt_bundle_does_not_verify(self) -> None:
        corrupt = self.root / "corrupt.bundle"
        corrupt.write_text("not a bundle\n", encoding="utf-8")

        self.assertFalse(verify_bundle(corrupt))

    def test_a_missing_bundle_does_not_verify(self) -> None:
        self.assertFalse(verify_bundle(self.root / "absent.bundle"))

    def test_a_directory_does_not_verify(self) -> None:
        self.assertFalse(verify_bundle(self.repo))

    def test_the_bundle_command_shape_matches_the_plan(self) -> None:
        plan = GithubBackupPlan(
            owner="smores56",
            repos=("repo",),
            out_dir=str(self.root / "mirror"),
            mirror=True,
            bundle=True,
        )
        commands = plan.commands()

        self.assertEqual(commands[0][:3], ["git", "clone", "--mirror"])
        self.assertEqual(commands[1][0:4], ["git", "-C", str(self.root / "mirror" / "repo.git"), "bundle"])


if __name__ == "__main__":
    unittest.main()
