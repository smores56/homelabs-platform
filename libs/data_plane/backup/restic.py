"""Restic destination adapter for the fan-out controller.

The adapter builds argv only; a caller supplies the runner. The default runner
is a dry-run recorder, so importing or testing this module never touches a
repository. A deployment passes a subprocess runner after the operator has
provisioned the repository and password file.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Protocol, Sequence

from ..errors import PublishError
from .generations import SnapshotRef


@dataclass(frozen=True)
class Command:
    """One argv-only command. No shell, ever."""

    argv: tuple[str, ...]
    timeout: int = 3600
    env: tuple[tuple[str, str], ...] = ()

    def environment(self) -> dict[str, str]:
        base = {key: value for key, value in os.environ.items() if key != "RESTIC_PASSWORD"}
        base.update(dict(self.env))
        return base


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


class Runner(Protocol):
    def run(self, command: Command) -> CommandResult: ...


@dataclass
class DryRunRunner:
    """Records commands and returns success without executing anything."""

    commands: list[Command] = field(default_factory=list)

    def run(self, command: Command) -> CommandResult:
        self.commands.append(command)
        return CommandResult(0, "", "")


class SubprocessRunner:
    """Runs an argv list without a shell and with a timeout."""

    def run(self, command: Command) -> CommandResult:
        completed = subprocess.run(
            list(command.argv),
            capture_output=True,
            text=True,
            timeout=command.timeout,
            check=False,
            env=command.environment(),
        )
        return CommandResult(
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )


@dataclass(frozen=True)
class RetentionPolicy:
    daily: int = 14
    weekly: int = 8
    monthly: int = 12
    prune: bool = True

    def validate(self) -> None:
        if min(self.daily, self.weekly, self.monthly) < 0:
            raise PublishError("retention counts cannot be negative")


@dataclass
class ResticDestination:
    """One independently keyed restic repository.

    ``dataset_paths`` maps a dataset name from the snapshotter to the path restic
    should read (usually the ZFS snapshot mount under ``.zfs/snapshot``).
    """

    name: str
    repository: str
    password_file: str
    dataset_paths: Mapping[str, str]
    runner: Runner = field(default_factory=DryRunRunner)
    host: str = "data-plane"
    tag: str = "data-plane"
    check_subset: str = "5%"
    extra_args: tuple[str, ...] = ()
    clock: Callable[[], float] = time.time

    def _base(self) -> list[str]:
        return [
            "restic",
            "--repo",
            self.repository,
            "--password-file",
            self.password_file,
        ]

    def _run(self, argv: Sequence[str], *, timeout: int = 3600) -> CommandResult:
        result = self.runner.run(Command(tuple(argv), timeout=timeout))
        if result.returncode != 0:
            raise PublishError(
                f"destination {self.name!r} command failed with {result.returncode}: "
                f"{result.stderr.strip()[:400]}"
            )
        return result

    def capture(
        self, generation_id: str, snapshot_ids: Mapping[str, str]
    ) -> SnapshotRef:
        if not snapshot_ids:
            raise PublishError(f"destination {self.name!r} got no snapshots")
        paths = []
        for dataset in sorted(snapshot_ids):
            path = self.dataset_paths.get(dataset)
            if not path:
                raise PublishError(
                    f"destination {self.name!r} has no path for dataset {dataset!r}"
                )
            paths.append(path)
        argv = self._base() + [
            "backup",
            "--host",
            self.host,
            "--tag",
            f"{self.tag}:{generation_id}",
            "--json",
            *self.extra_args,
            *paths,
        ]
        result = self._run(argv, timeout=6 * 3600)
        snapshot_id = _snapshot_id(result.stdout) or f"{self.name}:{generation_id}"
        return SnapshotRef(
            destination=self.name,
            snapshot_id=snapshot_id,
            created_at=self.clock(),
            group_id="",
            dataset=sorted(snapshot_ids)[0],
            verified=False,
        )

    def verify(self, ref: SnapshotRef) -> bool:
        result = self._run(self._base() + ["snapshots", "--json"], timeout=600)
        try:
            snapshots = json.loads(result.stdout or "[]")
        except json.JSONDecodeError:
            return False
        for snapshot in snapshots:
            if not isinstance(snapshot, Mapping):
                continue
            identifier = str(snapshot.get("id", ""))
            if identifier == ref.snapshot_id or identifier.startswith(ref.snapshot_id):
                return True
        return False

    def generations(self) -> list[str]:
        result = self._run(
            self._base() + ["snapshots", "--json", "--tag", self.tag], timeout=600
        )
        try:
            snapshots = json.loads(result.stdout or "[]")
        except json.JSONDecodeError:
            return []
        generations: set[str] = set()
        for snapshot in snapshots:
            if not isinstance(snapshot, Mapping):
                continue
            for tag in snapshot.get("tags", ()):
                text = str(tag)
                if text.startswith(self.tag + ":"):
                    generations.add(text.split(":", 1)[1])
        return sorted(generations)

    def retention_command(self, policy: RetentionPolicy) -> Command:
        policy.validate()
        argv = self._base() + [
            "forget",
            "--host",
            self.host,
            "--tag",
            self.tag,
            "--keep-daily",
            str(policy.daily),
            "--keep-weekly",
            str(policy.weekly),
            "--keep-monthly",
            str(policy.monthly),
        ]
        if policy.prune:
            argv.append("--prune")
        return Command(tuple(argv), timeout=6 * 3600)

    def prune(self, policy: RetentionPolicy) -> CommandResult:
        return self._run(list(self.retention_command(policy).argv), timeout=6 * 3600)


def _snapshot_id(stdout: str) -> str:
    """Restic --json emits one JSON object per line; find the snapshot id."""

    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            payload: Any = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, Mapping):
            for key in ("snapshot_id", "id"):
                value = payload.get(key)
                if isinstance(value, str) and value:
                    return value
    return ""
