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
    """Take one recursive snapshot per recovery group.

    ``group_datasets`` maps a group id to the parent dataset and every member
    dataset that shares the snapshot. The parent is snapshotted recursively.
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

    def snapshot(self, group_id: str) -> dict[str, str]:
        datasets = self.group_datasets.get(group_id)
        if not datasets:
            raise PublishError(f"no datasets configured for group {group_id!r}")
        stamp = datetime.fromtimestamp(self.clock(), tz=timezone.utc).strftime(
            "%Y%m%dT%H%M%SZ"
        )
        name = f"{self.snapshot_prefix}-{stamp}"
        parent = datasets[0]
        result = self.runner.run(Command(("zfs", "snapshot", "-r", f"{parent}@{name}")))
        if result.returncode != 0:
            raise PublishError(
                f"zfs snapshot failed for {group_id!r}: {result.stderr.strip()[:400]}"
            )
        return {dataset: f"{dataset}@{name}" for dataset in datasets}

    def resume(self, group_id: str) -> None:
        self._run_all(self.resume_commands.get(group_id, ()))
