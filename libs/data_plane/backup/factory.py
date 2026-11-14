"""Build a real fan-out backup controller from a host JSON configuration.

The host unit is the only place that touches ZFS and the restic repositories.
This module turns the generated ``/etc/homelab/backup.json`` into the adapters
the controller needs, refusing to start when the plan is incomplete:

* every group declares at least one destination,
* every destination names a repository with a password *file*,
* every snapshot dataset has an explicit read path,
* a group that declares a database requires checkpoint metadata.

No command is built with a shell, and no password ever appears on argv.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..errors import DataPlaneError
from .controller import FanoutController, MetadataProvider
from .generations import MANIFEST_VERSION, GenerationManifest
from .restic import Command, ResticDestination, Runner, SubprocessRunner
from .zfs import ZfsSnapshotter


class BackupConfigError(DataPlaneError):
    """The backup plan is missing a required, fail-closed detail."""


def _mapping(value: Any, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise BackupConfigError(f"{field_name} must be an object")
    return value


def _sequence(value: Any, field_name: str) -> Sequence[Any]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise BackupConfigError(f"{field_name} must be a list")
    return value


def _text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BackupConfigError(f"{field_name} must be a non-empty string")
    return value.strip()


def _text_list(value: Any, field_name: str) -> tuple[str, ...]:
    items = tuple(_text(item, f"{field_name}[]") for item in _sequence(value, field_name))
    if not items:
        raise BackupConfigError(f"{field_name} must not be empty")
    if len(items) != len(set(items)):
        raise BackupConfigError(f"{field_name} contains a duplicate")
    return items


def _argv_list(value: Any, field_name: str) -> tuple[tuple[str, ...], ...]:
    commands = []
    for index, entry in enumerate(_sequence(value, field_name)):
        argv = _sequence(entry, f"{field_name}[{index}]")
        if not argv or any(
            not isinstance(item, str) or not item for item in argv
        ):
            raise BackupConfigError(
                f"{field_name}[{index}] must be a non-empty argv list of strings"
            )
        commands.append(tuple(argv))
    return tuple(commands)


@dataclass(frozen=True)
class Repository:
    """One independently keyed restic repository."""

    name: str
    repository: str
    password_file: str
    host: str = "data-plane"
    tag: str = "data-plane"

    def validate(self) -> None:
        _text(self.name, "repository.name")
        _text(self.repository, "repository.repository")
        _text(self.password_file, f"repository {self.name!r}.password_file")
        _text(self.host, f"repository {self.name!r}.host")
        _text(self.tag, f"repository {self.name!r}.tag")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "Repository":
        payload = _mapping(value, "repository")
        repository = cls(
            name=_text(payload.get("name"), "repository.name"),
            repository=_text(payload.get("repository"), "repository.repository"),
            password_file=_text(
                payload.get("password_file"), "repository.password_file"
            ),
            host=_text(payload.get("host", "data-plane"), "repository.host"),
            tag=_text(payload.get("tag", "data-plane"), "repository.tag"),
        )
        repository.validate()
        return repository


@dataclass(frozen=True)
class BackupGroup:
    """One recovery group as the host backup unit sees it."""

    id: str
    datasets: tuple[str, ...]
    destinations: tuple[str, ...]
    dataset_paths: Mapping[str, str]
    fence: tuple[tuple[str, ...], ...] = ()
    checkpoint: tuple[tuple[str, ...], ...] = ()
    resume: tuple[tuple[str, ...], ...] = ()
    databases: tuple[str, ...] = ()
    cadence: str = "media"

    def validate(self) -> None:
        _text(self.id, "group.id")
        if not self.datasets:
            raise BackupConfigError(f"group {self.id!r} has no datasets")
        if not self.destinations:
            raise BackupConfigError(f"group {self.id!r} has no destination")
        if len(self.destinations) != len(set(self.destinations)):
            raise BackupConfigError(
                f"group {self.id!r} repeats a destination"
            )
        for dataset in self.datasets:
            _text(dataset, f"group {self.id!r}.datasets[]")
            path = self.dataset_paths.get(dataset)
            if not isinstance(path, str) or not path.strip():
                raise BackupConfigError(
                    f"group {self.id!r} has no dataset path for {dataset!r}"
                )
            if not Path(path).is_absolute():
                raise BackupConfigError(
                    f"group {self.id!r} path for {dataset!r} is not absolute"
                )
        if len(self.datasets) != len(set(self.datasets)):
            raise BackupConfigError(f"group {self.id!r} repeats a dataset")
        _text(self.cadence, f"group {self.id!r}.cadence")
        for name in self.databases:
            _text(name, f"group {self.id!r}.databases[]")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "BackupGroup":
        payload = _mapping(value, "group")
        paths_value = _mapping(
            payload.get("dataset_paths", {}), "group.dataset_paths"
        )
        paths = {}
        for dataset, path in paths_value.items():
            paths[_text(dataset, "group.dataset_paths key")] = _text(
                path, f"group.dataset_paths[{dataset!r}]"
            )
        group = cls(
            id=_text(payload.get("id"), "group.id"),
            datasets=_text_list(payload.get("datasets"), "group.datasets"),
            destinations=_text_list(
                payload.get("destinations"), "group.destinations"
            ),
            dataset_paths=paths,
            fence=_argv_list(payload.get("fence", []), "group.fence"),
            checkpoint=_argv_list(
                payload.get("checkpoint", []), "group.checkpoint"
            ),
            resume=_argv_list(payload.get("resume", []), "group.resume"),
            databases=tuple(
                _text(item, "group.databases[]")
                for item in _sequence(payload.get("databases", []), "group.databases")
            ),
            cadence=_text(payload.get("cadence", "media"), "group.cadence"),
        )
        group.validate()
        return group


@dataclass(frozen=True)
class BackupConfig:
    """The whole host backup plan, loaded from JSON."""

    schema_version: int
    manifest_dir: str
    groups: tuple[BackupGroup, ...]
    repositories: tuple[Repository, ...]
    manifest_schema_version: int = MANIFEST_VERSION

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        if not isinstance(self.schema_version, int) or self.schema_version < 1:
            raise BackupConfigError("schema_version must be a positive integer")
        if not isinstance(self.manifest_schema_version, int) or (
            self.manifest_schema_version < 1
        ):
            raise BackupConfigError("manifest_schema_version must be positive")
        _text(self.manifest_dir, "manifest_dir")
        if not self.groups:
            raise BackupConfigError("config has no backup groups")
        if not self.repositories:
            raise BackupConfigError("config has no repositories")
        names = [repository.name for repository in self.repositories]
        if len(names) != len(set(names)):
            raise BackupConfigError("repositories repeat a name")
        known = set(names)
        groups = [group.id for group in self.groups]
        if len(groups) != len(set(groups)):
            raise BackupConfigError("groups repeat an id")
        for group in self.groups:
            group.validate()
            unknown = sorted(set(group.destinations) - known)
            if unknown:
                raise BackupConfigError(
                    f"group {group.id!r} names unknown destinations: "
                    + ", ".join(unknown)
                )

    def group(self, group_id: str) -> BackupGroup:
        for group in self.groups:
            if group.id == group_id:
                return group
        raise BackupConfigError(f"unknown backup group {group_id!r}")

    def selected(
        self,
        group_ids: Iterable[str] | None = None,
        cadence: str | None = None,
    ) -> tuple[BackupGroup, ...]:
        chosen = list(self.groups)
        if group_ids:
            wanted = set(group_ids)
            missing = sorted(wanted - {group.id for group in self.groups})
            if missing:
                raise BackupConfigError(
                    "unknown backup groups: " + ", ".join(missing)
                )
            chosen = [group for group in chosen if group.id in wanted]
        if cadence is not None:
            chosen = [group for group in chosen if group.cadence == cadence]
            if not chosen:
                raise BackupConfigError(
                    f"no backup group uses cadence {cadence!r}"
                )
        return tuple(chosen)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "BackupConfig":
        payload = _mapping(value, "config")
        schema_version = payload.get("schema_version")
        if not isinstance(schema_version, int):
            raise BackupConfigError("schema_version must be an integer")
        repository_values = _sequence(
            payload.get("repositories"), "repositories"
        )
        group_values = _sequence(payload.get("groups"), "groups")
        try:
            manifest_schema_version = int(
                payload.get("manifest_schema_version", MANIFEST_VERSION)
            )
        except (TypeError, ValueError) as exc:
            raise BackupConfigError(
                "manifest_schema_version must be an integer"
            ) from exc
        return cls(
            schema_version=schema_version,
            manifest_dir=_text(payload.get("manifest_dir"), "manifest_dir"),
            groups=tuple(
                BackupGroup.from_dict(_mapping(item, "groups[]"))
                for item in group_values
            ),
            repositories=tuple(
                Repository.from_dict(_mapping(item, "repositories[]"))
                for item in repository_values
            ),
            manifest_schema_version=manifest_schema_version,
        )

    @classmethod
    def load(cls, path: str | Path) -> "BackupConfig":
        target = Path(path)
        try:
            payload = json.loads(target.read_text(encoding="utf-8"))
        except OSError as exc:
            raise BackupConfigError(f"cannot read {target}: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise BackupConfigError(f"{target} is not valid JSON: {exc}") from exc
        return cls.from_dict(_mapping(payload, str(target)))


def build_controller(
    config: BackupConfig,
    runner: Runner | None = None,
    metadata_provider: MetadataProvider | None = None,
) -> FanoutController:
    """Build a controller with a real ZFS snapshotter and one restic per repo."""

    config.validate()
    active_runner = SubprocessRunner() if runner is None else runner

    repositories = {repository.name: repository for repository in config.repositories}
    destinations: dict[str, ResticDestination] = {}
    paths: dict[str, str] = {}
    for group in config.groups:
        for dataset, path in group.dataset_paths.items():
            existing = paths.get(dataset)
            if existing is not None and existing != path:
                raise BackupConfigError(
                    f"dataset {dataset!r} has conflicting paths: "
                    f"{existing!r} vs {path!r}"
                )
            paths[dataset] = path
    for name, repository in repositories.items():
        destinations[name] = ResticDestination(
            name=name,
            repository=repository.repository,
            password_file=repository.password_file,
            dataset_paths=dict(paths),
            runner=active_runner,
            host=repository.host,
            tag=repository.tag,
        )

    declared = {group.id: group.databases for group in config.groups}
    with_databases = sorted(
        group.id for group in config.groups if group.databases
    )
    if with_databases and metadata_provider is None:
        raise BackupConfigError(
            "groups declare databases but no metadata provider was supplied: "
            + ", ".join(with_databases)
        )

    snapshotter = ZfsSnapshotter(
        group_datasets={group.id: group.datasets for group in config.groups},
        runner=active_runner,
        fence_commands={
            group.id: tuple(Command(argv) for argv in group.fence)
            for group in config.groups
            if group.fence
        },
        checkpoint_commands={
            group.id: tuple(Command(argv) for argv in group.checkpoint)
            for group in config.groups
            if group.checkpoint
        },
        resume_commands={
            group.id: tuple(Command(argv) for argv in group.resume)
            for group in config.groups
            if group.resume
        },
    )
    controller = FanoutController(
        snapshotter,
        destinations,
        config.manifest_dir,
        schema_version=config.manifest_schema_version,
        metadata_provider=metadata_provider,
        declared_databases=declared,
    )
    return controller


def run_group(
    controller: FanoutController,
    group: str | BackupGroup,
    config: BackupConfig,
    now: float | None = None,
) -> GenerationManifest:
    """Run one group through the controller using its declared destinations."""

    spec = config.group(group) if isinstance(group, str) else group
    return controller.run(spec.id, spec.destinations, now=now)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="snapshotter",
        description="Take fenced ZFS snapshots and fan them out to every restic repository.",
    )
    parser.add_argument(
        "--config",
        default="/etc/homelab/backup.json",
        help="backup plan JSON (default: %(default)s)",
    )
    parser.add_argument(
        "--group",
        action="append",
        dest="groups",
        metavar="ID",
        help="run only this group; repeatable",
    )
    parser.add_argument(
        "--cadence",
        help="run only groups with this cadence (e.g. small, media)",
    )
    parser.add_argument(
        "--list-groups",
        action="store_true",
        help="print the configured group ids and exit",
    )
    parser.add_argument(
        "--now",
        type=float,
        help="fixed Unix time for the generation id (testing)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Host oneshot entrypoint: read --config and run every selected group."""

    args = _parser().parse_args(argv)
    try:
        config = BackupConfig.load(args.config)
    except BackupConfigError as exc:
        print(f"snapshotter: {exc}", file=sys.stderr)
        return 2

    if args.list_groups:
        for group in config.groups:
            print(group.id)
        return 0

    try:
        selected = config.selected(args.groups, args.cadence)
        controller = build_controller(config)
    except BackupConfigError as exc:
        print(f"snapshotter: {exc}", file=sys.stderr)
        return 2

    failures = 0
    for group in selected:
        try:
            manifest = run_group(controller, group, config, now=args.now)
        except Exception as exc:  # keep backing up other groups, then fail
            print(f"snapshotter: group {group.id}: {exc}", file=sys.stderr)
            failures += 1
            continue
        print(manifest.generation_id)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
