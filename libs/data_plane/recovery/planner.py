"""Ordered restore plans over one selected and verified backup generation.

Selection delegates to
``data_plane.backup.generations.select_generation``: the shared contract that
skips incomplete, incompatible, and future-dated generations. This module
narrows the pool to the requested recovery group, re-verifies the selected
generation's content hashes and dependencies, and renders argv-only steps that
never pass through a shell, so a plan can be reviewed or handed to a backend
unchanged.

A generation manifest carries no mount paths, so the caller supplies the
recovery group's idiomatic mounts (``mounts``) and, when a database is not at
the default ``/var/lib/<name>/<name>.db``, its live path (``sqlite_paths``).
The online SQLite checkpoint is expected next to the live database as
``<path>.checkpoint`` inside the generation's dataset.

Planning is always a dry run: nothing here touches a destination. Execution is
a separate, explicitly applied call through a caller-supplied backend.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Callable, Iterable, Mapping, Protocol, Sequence

from ..backup.generations import GenerationManifest, select_generation
from ..errors import ManifestError, RestoreError, ValidationError

STEP_KINDS = ("dataset", "config", "sqlite-restore", "sqlite-checkpoint")
SQLITE_CHECKPOINT_SUFFIX = ".checkpoint"
_HEX = frozenset("0123456789abcdef")


@dataclass(frozen=True)
class RestoreStep:
    """One argv-only step. The backend runs ``argv`` exactly as listed."""

    kind: str
    target: str
    source: str
    argv: tuple[str, ...]
    notes: str = ""

    def __post_init__(self) -> None:
        if self.kind not in STEP_KINDS:
            raise ValidationError(
                f"restore step kind must be one of {', '.join(STEP_KINDS)}, "
                f"got {self.kind!r}"
            )
        object.__setattr__(self, "argv", tuple(self.argv))
        if not self.argv:
            raise ValidationError(f"restore step {self.kind!r} has an empty argv")
        for part in self.argv:
            if not isinstance(part, str) or not part:
                raise ValidationError(
                    f"restore step {self.kind!r} argv must hold non-empty strings"
                )
        if not self.target.startswith("/"):
            raise ValidationError(
                f"restore step {self.kind!r} target must be absolute, got {self.target!r}"
            )


@dataclass(frozen=True)
class RestorePlan:
    """A verified, ordered plan. ``schema_version`` is the generation's schema."""

    group_id: str
    generation_id: str
    steps: tuple[RestoreStep, ...]
    warnings: tuple[str, ...] = ()
    schema_version: int = 1

    def __post_init__(self) -> None:
        if not self.group_id.strip():
            raise ValidationError("restore plan has an empty group id")
        if not self.generation_id.strip():
            raise ValidationError("restore plan has an empty generation id")
        if self.schema_version < 1:
            raise ValidationError("restore plan schema_version must be >= 1")
        object.__setattr__(self, "steps", tuple(self.steps))
        object.__setattr__(self, "warnings", tuple(self.warnings))

    def commands(self) -> list[list[str]]:
        """Every step as a plain argv list, for review and audit logs."""

        return [list(step.argv) for step in self.steps]


class RestoreBackend(Protocol):
    """Runs one already-approved step. The caller owns authorization."""

    def run(self, argv: Sequence[str], *, source: str = "") -> None:
        """Execute ``argv``; ``source`` names a stream the step consumes."""


Selector = Callable[
    [Sequence[GenerationManifest], Sequence[str], Sequence[str], int],
    GenerationManifest,
]


def _sha256(value: object, label: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or not set(value) <= _HEX:
        raise RestoreError(f"{label} is not a lowercase hex SHA-256: {value!r}")
    return value


def _absolute(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{label} must be a non-empty string")
    if not value.startswith("/"):
        raise ValidationError(f"{label} must be absolute, got {value!r}")
    return value


def _target(destination: str, path: str) -> str:
    root = os.path.normpath(destination)
    target = os.path.normpath(
        os.path.join(root, _absolute(path, "restore path").lstrip("/"))
    )
    if os.path.commonpath([root, target]) != root:
        raise ValidationError(f"restore path {path!r} escapes {destination!r}")
    return target


def _mount_for(name: str, mounts: Mapping[str, str] | None) -> str:
    mount = f"/{name}" if mounts is None else mounts.get(name, f"/{name}")
    mount = _absolute(mount, f"dataset {name!r} mount")
    if ".." in mount.split("/"):
        raise ValidationError(f"dataset {name!r} mount escapes its root")
    return mount


def _ordered_datasets(
    required: Sequence[str],
    dependencies: Sequence[str],
    mounts: Mapping[str, str] | None,
) -> tuple[str, ...]:
    prerequisites = set(dependencies)
    restore_set = set(required)
    mount_of = {name: _mount_for(name, mounts) for name in required}
    ordered = sorted(
        required,
        key=lambda name: (
            0 if name in prerequisites and name in restore_set else 1,
            mount_of[name].count("/"),
            mount_of[name],
            name,
        ),
    )
    return tuple(ordered)


def _verify_dependencies(
    chosen: GenerationManifest,
    required_datasets: Sequence[str],
    warnings: list[str],
) -> None:
    names = {name for name, _ in chosen.datasets}
    restore_set = set(required_datasets)
    for dependency in chosen.dependencies:
        if not isinstance(dependency, str) or not dependency.strip():
            raise RestoreError(
                f"generation {chosen.generation_id!r} has an empty dependency"
            )
        if dependency in restore_set:
            continue
        if dependency in names:
            raise RestoreError(
                f"generation {chosen.generation_id!r} depends on dataset "
                f"{dependency!r}, which is not in the restore set"
            )
        warnings.append(
            f"dependency {dependency!r} is not a dataset in generation "
            f"{chosen.generation_id!r}; the plan cannot order it"
        )


def _dataset_steps(
    destination: str,
    chosen: GenerationManifest,
    ordered: Sequence[str],
    mounts: Mapping[str, str] | None,
) -> tuple[RestoreStep, ...]:
    snapshots = dict(chosen.datasets)
    steps: list[RestoreStep] = []
    for name in ordered:
        snapshot = snapshots[name]
        if not isinstance(snapshot, str) or not snapshot.strip():
            raise RestoreError(f"dataset {name!r} has an empty snapshot id")
        target = _target(destination, _mount_for(name, mounts))
        steps.append(
            RestoreStep(
                kind="dataset",
                target=target,
                source=snapshot,
                argv=("zfs", "receive", "-F", "-u", target),
                notes=(
                    f"stream `zfs send {snapshot}` into {target}; left unmounted "
                    "until the generation verifies"
                ),
            )
        )
    return tuple(steps)


def _config_steps(
    destination: str,
    chosen: GenerationManifest,
    snapshot_id: str,
) -> tuple[RestoreStep, ...]:
    steps: list[RestoreStep] = []
    seen: set[str] = set()
    for path, checksum in sorted(chosen.content_hashes):
        _absolute(path, "content path")
        if path in seen:
            raise RestoreError(f"generation {chosen.generation_id!r} repeats {path!r}")
        seen.add(path)
        _sha256(checksum, f"content {path!r} sha256")
        steps.append(
            RestoreStep(
                kind="config",
                target=_target(destination, path),
                source=snapshot_id,
                argv=(
                    "restic",
                    "restore",
                    snapshot_id,
                    "--target",
                    destination,
                    "--include",
                    path,
                ),
                notes=(
                    f"restore {path} from the generation snapshot; "
                    f"verify sha256 {checksum}"
                ),
            )
        )
    return tuple(steps)


def _sqlite_path(name: str, paths: Mapping[str, str] | None) -> str:
    if paths is not None and name in paths:
        return _absolute(paths[name], f"sqlite {name!r} path")
    return f"/var/lib/{name}/{name}.db"


def _sqlite_steps(
    destination: str,
    chosen: GenerationManifest,
    paths: Mapping[str, str] | None,
) -> tuple[RestoreStep, ...]:
    steps: list[RestoreStep] = []
    for name, epoch, position in sorted(chosen.sqlite):
        if not isinstance(name, str) or not name.strip():
            raise RestoreError(f"generation {chosen.generation_id!r} has an empty sqlite name")
        target = _target(destination, _sqlite_path(name, paths))
        checkpoint = _target(
            destination, _sqlite_path(name, paths) + SQLITE_CHECKPOINT_SUFFIX
        )
        steps.append(
            RestoreStep(
                kind="sqlite-restore",
                target=target,
                source=checkpoint,
                argv=("sqlite3", target, f".restore {checkpoint}"),
                notes=(
                    f"restore {name} from its online checkpoint "
                    f"(generation {chosen.generation_id}, epoch {epoch}, "
                    f"position {position})"
                ),
            )
        )
        steps.append(
            RestoreStep(
                kind="sqlite-checkpoint",
                target=target,
                source="",
                argv=("sqlite3", target, "PRAGMA wal_checkpoint(TRUNCATE);"),
                notes=(
                    f"fold the restored WAL for {name} before the event service "
                    "opens it"
                ),
            )
        )
    return tuple(steps)


def _select(
    manifests: Sequence[GenerationManifest],
    selector: Selector | None,
    required_destinations: Sequence[str],
    required_datasets: Sequence[str],
    schema_version: int,
) -> GenerationManifest:
    choose = select_generation if selector is None else selector
    try:
        chosen = choose(
            manifests,
            tuple(required_destinations),
            tuple(required_datasets),
            schema_version,
        )
    except RestoreError:
        raise
    except (ManifestError, TypeError, ValueError, KeyError) as error:
        raise RestoreError(f"no restorable generation was selected: {error}") from error
    if not isinstance(chosen, GenerationManifest):
        raise RestoreError(
            f"select_generation returned {type(chosen).__name__}, not a GenerationManifest"
        )
    return chosen


def plan_restore(
    manifests: Iterable[GenerationManifest],
    group_id: str,
    required_destinations: Sequence[str],
    required_datasets: Sequence[str],
    schema_version: int,
    destination: str,
    *,
    mounts: Mapping[str, str] | None = None,
    sqlite_paths: Mapping[str, str] | None = None,
    selector: Selector | None = None,
) -> RestorePlan:
    """Select one complete generation and lay out its restore steps.

    ``destination`` is the absolute restore root; every target stays inside it.
    ``mounts`` maps dataset names to their mount paths and ``sqlite_paths``
    maps database names to their live paths. ``selector`` defaults to the
    shared ``data_plane.backup.generations.select_generation``.
    """

    if not isinstance(group_id, str) or not group_id.strip():
        raise ValidationError("restore group_id must be non-empty")
    required_destinations = tuple(required_destinations)
    required_datasets = tuple(required_datasets)
    if not required_destinations:
        raise ValidationError("restore needs at least one required destination")
    if not required_datasets:
        raise ValidationError("restore needs at least one required dataset")
    if len(set(required_destinations)) != len(required_destinations):
        raise ValidationError("required destinations contain duplicates")
    if len(set(required_datasets)) != len(required_datasets):
        raise ValidationError("required datasets contain duplicates")
    if not isinstance(schema_version, int) or isinstance(schema_version, bool):
        raise ValidationError("restore schema_version must be a positive integer")
    if schema_version < 1:
        raise ValidationError("restore schema_version must be a positive integer")
    destination = os.path.normpath(_absolute(destination, "restore destination"))

    pool: list[GenerationManifest] = []
    for manifest in manifests:
        if not isinstance(manifest, GenerationManifest):
            raise ValidationError(
                f"restore manifest must be a GenerationManifest, got {type(manifest).__name__}"
            )
        if manifest.group_id == group_id:
            pool.append(manifest)
    if not pool:
        raise RestoreError(f"no generation for recovery group {group_id!r}")

    chosen = _select(
        tuple(pool), selector, required_destinations, required_datasets, schema_version
    )
    if chosen.group_id != group_id:
        raise RestoreError(
            f"generation {chosen.generation_id!r} belongs to group "
            f"{chosen.group_id!r}, not {group_id!r}"
        )
    try:
        chosen.validate(required_destinations, required_datasets, schema_version)
    except ManifestError as error:
        raise RestoreError(
            f"generation {chosen.generation_id!r} is not restorable: {error}"
        ) from error
    if not chosen.datasets:
        raise RestoreError(f"generation {chosen.generation_id!r} has no datasets")

    warnings: list[str] = []
    _verify_dependencies(chosen, required_datasets, warnings)
    for reference in chosen.destinations:
        if reference.destination not in required_destinations:
            warnings.append(
                f"destination {reference.destination!r} is available in generation "
                f"{chosen.generation_id!r} but not required"
            )

    destinations = {reference.destination: reference for reference in chosen.destinations}
    snapshot_id = destinations[required_destinations[0]].snapshot_id
    ordered = _ordered_datasets(required_datasets, chosen.dependencies, mounts)

    steps = (
        _dataset_steps(destination, chosen, ordered, mounts)
        + _config_steps(destination, chosen, snapshot_id)
        + _sqlite_steps(destination, chosen, sqlite_paths)
    )
    return RestorePlan(
        group_id=group_id,
        generation_id=chosen.generation_id,
        steps=steps,
        warnings=tuple(warnings),
        schema_version=schema_version,
    )


def execute(
    plan: RestorePlan,
    backend: RestoreBackend | None,
    apply: bool = False,
) -> tuple[str, ...]:
    """Run a plan through ``backend``, only when the caller applies explicitly."""

    if apply is not True:
        raise RestoreError("restore plans are dry runs; pass apply=True to execute one")
    runner = getattr(backend, "run", None)
    if not callable(runner):
        raise RestoreError(
            "restore execution needs a RestoreBackend with run(argv, source=...)"
        )

    executed: list[str] = []
    for step in plan.steps:
        runner(step.argv, source=step.source)
        executed.append(step.target)
    return tuple(executed)
