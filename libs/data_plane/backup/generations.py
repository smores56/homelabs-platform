"""Backup generation manifests and restore-side generation selection.

A generation is an immutable record of one fenced snapshot captured by every
required destination. Nothing selects a mutable "latest" pointer: restore reads
manifests, validates them against the required destinations, datasets, and
schema, and picks the newest complete one.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from ..errors import ManifestError, RestoreError
from ..identity import canonical_json

MANIFEST_VERSION = 1


@dataclass(frozen=True)
class SnapshotRef:
    """One destination's independent capture of a generation's snapshot."""

    destination: str
    snapshot_id: str
    created_at: float
    group_id: str
    dataset: str
    parent: str | None = None
    verified: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "destination": self.destination,
            "snapshot_id": self.snapshot_id,
            "created_at": self.created_at,
            "group_id": self.group_id,
            "dataset": self.dataset,
            "parent": self.parent,
            "verified": self.verified,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "SnapshotRef":
        try:
            parent = value.get("parent")
            return cls(
                destination=str(value["destination"]),
                snapshot_id=str(value["snapshot_id"]),
                created_at=float(value["created_at"]),
                group_id=str(value["group_id"]),
                dataset=str(value["dataset"]),
                parent=None if parent is None else str(parent),
                verified=bool(value.get("verified", False)),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ManifestError(f"malformed snapshot reference: {exc}") from exc


def _pairs(value: Any, size: int, field: str) -> tuple[tuple[Any, ...], ...]:
    if not isinstance(value, (list, tuple)):
        raise ManifestError(f"manifest field {field!r} is not a list")
    items: list[tuple[Any, ...]] = []
    for item in value:
        if not isinstance(item, (list, tuple)) or len(item) != size:
            raise ManifestError(f"manifest field {field!r} has a malformed entry")
        items.append(tuple(item))
    return tuple(items)


@dataclass(frozen=True)
class GenerationManifest:
    """Everything restore needs to trust and select one backup generation."""

    version: int
    generation_id: str
    group_id: str
    created_at: float
    datasets: tuple[tuple[str, str], ...] = ()
    sqlite: tuple[tuple[str, int, int], ...] = ()
    app_versions: tuple[tuple[str, str], ...] = ()
    content_hashes: tuple[tuple[str, str], ...] = ()
    dependencies: tuple[str, ...] = ()
    destinations: tuple[SnapshotRef, ...] = ()
    complete: bool = False
    schema_version: int = MANIFEST_VERSION
    release: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "generation_id": self.generation_id,
            "group_id": self.group_id,
            "created_at": self.created_at,
            "datasets": [[name, snapshot_id] for name, snapshot_id in self.datasets],
            "sqlite": [
                [name, epoch, position] for name, epoch, position in self.sqlite
            ],
            "app_versions": [[name, version] for name, version in self.app_versions],
            "content_hashes": [
                [path, sha256] for path, sha256 in self.content_hashes
            ],
            "dependencies": list(self.dependencies),
            "destinations": [item.to_dict() for item in self.destinations],
            "complete": self.complete,
            "schema_version": self.schema_version,
            "release": self.release,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "GenerationManifest":
        try:
            datasets = tuple(
                (str(name), str(snapshot_id))
                for name, snapshot_id in _pairs(
                    value.get("datasets", ()), 2, "datasets"
                )
            )
            sqlite = tuple(
                (str(name), int(epoch), int(position))
                for name, epoch, position in _pairs(
                    value.get("sqlite", ()), 3, "sqlite"
                )
            )
            app_versions = tuple(
                (str(name), str(version))
                for name, version in _pairs(
                    value.get("app_versions", ()), 2, "app_versions"
                )
            )
            content_hashes = tuple(
                (str(path), str(sha256))
                for path, sha256 in _pairs(
                    value.get("content_hashes", ()), 2, "content_hashes"
                )
            )
            destination_values = value.get("destinations", ())
            if not isinstance(destination_values, (list, tuple)):
                raise ManifestError("manifest field 'destinations' is not a list")
            destinations = tuple(
                SnapshotRef.from_dict(item) for item in destination_values
            )
            dependency_values = value.get("dependencies", ())
            if not isinstance(dependency_values, (list, tuple)):
                raise ManifestError("manifest field 'dependencies' is not a list")
            return cls(
                version=int(value["version"]),
                generation_id=str(value["generation_id"]),
                group_id=str(value["group_id"]),
                created_at=float(value["created_at"]),
                datasets=datasets,
                sqlite=sqlite,
                app_versions=app_versions,
                content_hashes=content_hashes,
                dependencies=tuple(str(item) for item in dependency_values),
                destinations=destinations,
                complete=bool(value.get("complete", False)),
                schema_version=int(value["schema_version"]),
                release=str(value.get("release", "")),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ManifestError(f"malformed manifest: {exc}") from exc

    def validate(
        self,
        required_destinations: Iterable[str] = (),
        required_datasets: Iterable[str] = (),
        schema_version: int = MANIFEST_VERSION,
    ) -> None:
        """Reject a generation restore cannot trust.

        Every required destination must have captured the generation exactly
        once; extra destinations are allowed because retention tiers can add
        copies without making a generation less restorable.
        """

        if self.version < 1:
            raise ManifestError(f"generation {self.generation_id!r} has no version")
        if not self.generation_id.strip():
            raise ManifestError("generation id is empty")
        if not self.group_id.strip():
            raise ManifestError(f"generation {self.generation_id!r} has no group")
        if not self.complete:
            raise ManifestError(f"generation {self.generation_id!r} is incomplete")
        if self.schema_version != schema_version:
            raise ManifestError(
                f"generation {self.generation_id!r} has schema "
                f"{self.schema_version}, expected {schema_version}"
            )

        names = [item.destination for item in self.destinations]
        if not names:
            raise ManifestError(
                f"generation {self.generation_id!r} has no destinations"
            )
        if any(not name.strip() for name in names):
            raise ManifestError(
                f"generation {self.generation_id!r} has an empty destination"
            )
        if len(names) != len(set(names)):
            raise ManifestError(
                f"generation {self.generation_id!r} has duplicate destinations"
            )
        if any(not item.snapshot_id.strip() for item in self.destinations):
            raise ManifestError(
                f"generation {self.generation_id!r} has an empty snapshot id"
            )
        missing_destinations = set(required_destinations) - set(names)
        if missing_destinations:
            raise ManifestError(
                f"generation {self.generation_id!r} is missing destinations: "
                + ", ".join(sorted(missing_destinations))
            )

        dataset_names = [name for name, _ in self.datasets]
        if any(not name.strip() for name in dataset_names):
            raise ManifestError(
                f"generation {self.generation_id!r} has an empty dataset"
            )
        if len(dataset_names) != len(set(dataset_names)):
            raise ManifestError(
                f"generation {self.generation_id!r} has duplicate datasets"
            )
        missing_datasets = set(required_datasets) - set(dataset_names)
        if missing_datasets:
            raise ManifestError(
                f"generation {self.generation_id!r} is missing datasets: "
                + ", ".join(sorted(missing_datasets))
            )


def manifest_hash(manifest: GenerationManifest) -> str:
    """Content address of a manifest, independent of how it is stored."""

    return hashlib.sha256(canonical_json(manifest.to_dict())).hexdigest()


def write_manifest(path: str | os.PathLike[str], manifest: GenerationManifest) -> Path:
    """Write a manifest through a temporary file, fsync, and atomic rename."""

    target = Path(path)
    if target.exists():
        raise ManifestError(f"manifest {target} already exists; refusing to overwrite")
    payload = dict(manifest.to_dict())
    payload["manifest_hash"] = manifest_hash(manifest)
    data = canonical_json(payload) + b"\n"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(
            prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
        )
    except OSError as exc:
        raise ManifestError(f"cannot stage manifest {target}: {exc}") from exc
    try:
        with os.fdopen(handle, "wb") as file:
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, target)
    except OSError as exc:
        raise ManifestError(f"cannot write manifest {target}: {exc}") from exc
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    try:
        directory = os.open(target.parent, os.O_RDONLY)
    except OSError:
        pass
    else:
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    return target


def read_manifest(path: str | os.PathLike[str]) -> GenerationManifest:
    """Read and integrity-check a manifest written by ``write_manifest``."""

    target = Path(path)
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ManifestError(f"cannot read manifest {target}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ManifestError(f"manifest {target} is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ManifestError(f"manifest {target} is not an object")
    claimed = payload.pop("manifest_hash", None)
    if claimed is None:
        raise ManifestError(f"manifest {target} has no manifest_hash")
    manifest = GenerationManifest.from_dict(payload)
    if claimed != manifest_hash(manifest):
        raise ManifestError(f"manifest {target} fails its content hash")
    return manifest


def select_generation(
    manifests: Iterable[GenerationManifest],
    required_destinations: Iterable[str],
    required_datasets: Iterable[str],
    schema_version: int,
    now: float | None = None,
) -> GenerationManifest:
    """Pick the newest complete, compatible generation.

    Incomplete, incompatible, and future-dated generations are skipped rather
    than trusted; the caller gets a ``RestoreError`` when nothing qualifies.
    """

    destinations = tuple(required_destinations)
    datasets = tuple(required_datasets)
    candidates: list[GenerationManifest] = []
    rejected: list[str] = []
    for manifest in manifests:
        if now is not None and manifest.created_at > now:
            rejected.append(f"{manifest.generation_id}: created in the future")
            continue
        try:
            manifest.validate(destinations, datasets, schema_version)
        except ManifestError as exc:
            rejected.append(str(exc))
            continue
        candidates.append(manifest)
    if not candidates:
        detail = "; ".join(rejected) if rejected else "no manifests supplied"
        raise RestoreError(f"no complete compatible generation: {detail}")
    return max(candidates, key=lambda item: (item.created_at, item.generation_id))
