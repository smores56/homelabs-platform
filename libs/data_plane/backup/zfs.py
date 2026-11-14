"""ZFS snapshotter adapter for the fan-out controller.

It builds argv-only commands and uses the same runner abstraction as the restic
destination. The default is a dry-run recorder. ``fence``/``resume`` hooks let
the host stop and start application writers around the snapshot; a group with no
hooks is treated as already consistent.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Mapping, Sequence

from ..errors import PublishError
from .restic import Command, CommandResult, DryRunRunner, Runner


@dataclass
class ZfsSnapshotter:
    """Take one recursive snapshot per outermost dataset in a group.

    ``group_datasets`` maps a group id to the parent dataset and every member
    dataset that shares the snapshot. Each outermost dataset is snapshotted
    recursively, so nested members exist without redundant commands and sibling
    members (a group with no single parent tree) are still all captured.
    """

    group_datasets: Mapping[str, tuple[str, ...]]
    runner: Runner = field(default_factory=DryRunRunner)
    clock: Callable[[], float] = time.time
    snapshot_prefix: str = "data-plane"
    fence_commands: Mapping[str, tuple[Command, ...]] = field(default_factory=dict)
    checkpoint_commands: Mapping[str, tuple[Command, ...]] = field(
        default_factory=dict
    )
    resume_commands: Mapping[str, tuple[Command, ...]] = field(default_factory=dict)

    def _run_all(self, commands: Sequence[Command]) -> None:
        for command in commands:
            result: CommandResult = self.runner.run(command)
            if result.returncode != 0:
                raise PublishError(
                    f"snapshot hook failed with {result.returncode}: "
                    f"{result.stderr.strip()[:400]}"
                )

    def fence(self, group_id: str) -> None:
        self._run_all(self.fence_commands.get(group_id, ()))

    def checkpoint(self, group_id: str) -> None:
        self._run_all(self.checkpoint_commands.get(group_id, ()))

    def _roots(self, datasets: Sequence[str]) -> list[str]:
        """Return the outermost datasets; a recursive snapshot covers descendants."""

        unique = list(dict.fromkeys(datasets))
        roots = []
        for dataset in unique:
            covered = any(
                other != dataset and dataset.startswith(other + "/")
                for other in unique
            )
            if not covered:
                roots.append(dataset)
        return roots

    def snapshot(self, group_id: str) -> dict[str, str]:
        datasets = self.group_datasets.get(group_id)
        if not datasets:
            raise PublishError(f"no datasets configured for group {group_id!r}")
        stamp = datetime.fromtimestamp(self.clock(), tz=timezone.utc).strftime(
            "%Y%m%dT%H%M%SZ"
        )
        name = f"{self.snapshot_prefix}-{stamp}"
        # Disjoint member datasets (a group whose members are siblings rather
        # than one parent tree) each need their own recursive snapshot. Writers
        # stay fenced across all of them, so the generation is still one point.
        for root in self._roots(datasets):
            result = self.runner.run(
                Command(("zfs", "snapshot", "-r", f"{root}@{name}"))
            )
            if result.returncode != 0:
                raise PublishError(
                    f"zfs snapshot failed for {group_id!r} ({root}): "
                    f"{result.stderr.strip()[:400]}"
                )
        return {dataset: f"{dataset}@{name}" for dataset in datasets}

    def resume(self, group_id: str) -> None:
        self._run_all(self.resume_commands.get(group_id, ()))
