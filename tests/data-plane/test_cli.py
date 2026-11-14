"""Offline CLI tests: contracts, confirmation rendering, manifests, memory."""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from data_plane.backup.generations import (
    GenerationManifest,
    SnapshotRef,
    write_manifest,
)
from data_plane.cli import main
from data_plane.memory.file_memory import FileMemory, MemoryRecord


def run(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(list(argv))
    return code, out.getvalue(), err.getvalue()


CATALOG = {
    "sources": [
        {
            "source_id": "books",
            "visibility": "general",
            "version": 1,
            "recovery_group": "books",
            "stable_id_fields": ["id"],
            "capabilities": [
                {"name": "read", "effect": "read"},
                {"name": "create", "effect": "create"},
            ],
        }
    ]
}

OPERATION = {
    "intent_id": "intent-1",
    "source_id": "books",
    "capability": "delete",
    "targets": ["book-1"],
    "base_revision": "rev-1",
    "field_diff": [["title", "Old", "New"]],
    "affected_count": 1,
    "epoch": 2,
    "recurrence": None,
    "attendee_scope": [],
    "destination": None,
}


def manifest(generation_id: str, complete: bool) -> GenerationManifest:
    return GenerationManifest(
        version=1,
        generation_id=generation_id,
        group_id="books",
        created_at=100.0 if complete else 50.0,
        datasets=(("content", "snap-1"),),
        destinations=(
            SnapshotRef(
                destination="nvme",
                snapshot_id="snap-1",
                created_at=100.0,
                group_id="books",
                dataset="data/books/content",
                verified=True,
            ),
        ),
        complete=complete,
        schema_version=1,
    )


class CliTests(unittest.TestCase):
    def test_validate_catalog(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "catalog.json"
            path.write_text(json.dumps(CATALOG), encoding="utf-8")
            code, out, _ = run("validate-catalog", str(path))
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), {"sources": 1, "ids": ["books"]})

    def test_validate_catalog_rejects_bad_contract(self):
        bad = json.loads(json.dumps(CATALOG))
        bad["sources"][0]["capabilities"].append(
            {"name": "delete", "effect": "delete", "reversible": False}
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "catalog.json"
            path.write_text(json.dumps(bad), encoding="utf-8")
            code, _, err = run("validate-catalog", str(path))
        self.assertEqual(code, 2)
        self.assertIn("must require confirmation", err)

    def test_render_confirmation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "operation.json"
            path.write_text(json.dumps(OPERATION), encoding="utf-8")
            code, out, _ = run("render-confirmation", str(path))
        self.assertEqual(code, 0)
        self.assertIn("Confirmation required", out)
        self.assertIn("affected count: 1", out)
        self.assertIn("hash: ", out)

    def test_manifest_hash_and_select(self):
        with tempfile.TemporaryDirectory() as tmp:
            good = Path(tmp) / "good.json"
            bad = Path(tmp) / "bad.json"
            write_manifest(good, manifest("gen-2", True))
            write_manifest(bad, manifest("gen-1", False))
            code, out, _ = run("manifest-hash", str(good))
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(out)["generation_id"], "gen-2")

            code, out, _ = run(
                "select-generation",
                str(good),
                str(bad),
                "--destinations",
                "nvme",
                "--datasets",
                "content",
                "--schema",
                "1",
            )
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["generation_id"], "gen-2")

    def test_memory_search(self):
        with tempfile.TemporaryDirectory() as tmp:
            FileMemory(tmp, "local").put(
                MemoryRecord(
                    id="m-1",
                    profile="local",
                    source="notes",
                    content="hello",
                    provenance={
                        "source_id": "notes",
                        "revision": "r1",
                        "captured_at": "2026-01-01T00:00:00Z",
                    },
                )
            )
            code, out, _ = run("memory-search", tmp, "local", "hello")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)[0]["id"], "m-1")

    def test_github_plan(self):
        code, out, _ = run(
            "github-plan", "smores56", "books", "--out", "/var/backups/github"
        )
        self.assertEqual(code, 0)
        commands = json.loads(out)
        self.assertTrue(any(command[0] == "git" for command in commands))


if __name__ == "__main__":
    unittest.main()
