"""Destination abstraction and filesystem/in-memory test destinations.

Each destination captures the generation's snapshot independently. No
destination copies objects from another destination: that is what makes the
fan-out a real second copy rather than a mirror of one repository.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Protocol

from ..errors import PublishError
from .generations import SnapshotRef


class Destination(Protocol):
    """A restic repository that can independently capture one generation."""

    name: str

    def capture(
        self, generation_id: str, snapshot_ids: Mapping[str, str]
    ) -> SnapshotRef:
        """Back up the frozen snapshot into this destination's own repository."""

    def verify(self, ref: SnapshotRef) -> bool:
        """Prove the captured snapshot is present and readable here."""

    def generations(self) -> list[str]:
        """Generation ids this destination holds."""


def _primary_dataset(snapshot_ids: Mapping[str, str]) -> str:
    return sorted(snapshot_ids)[0] if snapshot_ids else ""


@dataclass
class InMemoryDestination:
    """Test destination that records captures without touching disk or network."""

    name: str
    clock: Any = field(default=time.time)
    fail: bool = False
    verify_ok: bool = True

    def __post_init__(self) -> None:
        self.captures: list[dict[str, str]] = []
        self._generations: list[str] = []
        self._refs: list[SnapshotRef] = []

    def capture(
        self, generation_id: str, snapshot_ids: Mapping[str, str]
    ) -> SnapshotRef:
        if self.fail:
            raise PublishError(f"destination {self.name!r} cannot capture")
        if generation_id in self._generations:
            raise PublishError(
                f"destination {self.name!r} already holds generation {generation_id!r}"
            )
        snapshot = dict(snapshot_ids)
        reference = SnapshotRef(
            destination=self.name,
            snapshot_id=f"{self.name}:{generation_id}",
            created_at=self.clock(),
            group_id="",
            dataset=_primary_dataset(snapshot),
            verified=self.verify_ok,
        )
        self.captures.append(snapshot)
        self._generations.append(generation_id)
        self._refs.append(reference)
        return reference

    def verify(self, ref: SnapshotRef) -> bool:
        if not self.verify_ok or ref.destination != self.name:
            return False
        return any(
            item.snapshot_id == ref.snapshot_id
            for item in self._refs
            if item.verified
        )

    def generations(self) -> list[str]:
        return list(self._generations)


@dataclass
class DirectoryDestination:
    """Test destination that stores one metadata file per generation.

    It never copies payload bytes: the file records which snapshot was captured
    so tests can assert independent captures without executing restic.
    """

    root: str | os.PathLike[str]
    name: str
    clock: Any = field(default=time.time)
    fail: bool = False

    def __post_init__(self) -> None:
        self._root = Path(self.root)

    def _path(self, generation_id: str) -> Path:
        return self._root / f"{generation_id}.json"

    def capture(
        self, generation_id: str, snapshot_ids: Mapping[str, str]
    ) -> SnapshotRef:
        if self.fail:
            raise PublishError(f"destination {self.name!r} cannot capture")
        target = self._path(generation_id)
        if target.exists():
            raise PublishError(
                f"destination {self.name!r} already holds generation {generation_id!r}"
            )
        created_at = self.clock()
        payload = {
            "generation_id": generation_id,
            "snapshot_ids": dict(sorted(snapshot_ids.items())),
            "created_at": created_at,
        }
        try:
            self._root.mkdir(parents=True, exist_ok=True)
            handle, temporary = tempfile.mkstemp(
                prefix=f".{target.name}.", suffix=".tmp", dir=self._root
            )
            try:
                with os.fdopen(handle, "w", encoding="utf-8") as file:
                    json.dump(payload, file, sort_keys=True)
                    file.flush()
                    os.fsync(file.fileno())
                os.replace(temporary, target)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
        except OSError as exc:
            raise PublishError(
                f"destination {self.name!r} cannot capture {generation_id!r}: {exc}"
            ) from exc
        return SnapshotRef(
            destination=self.name,
            snapshot_id=f"{self.name}:{generation_id}",
            created_at=created_at,
            group_id="",
            dataset=_primary_dataset(snapshot_ids),
            verified=True,
        )

    def verify(self, ref: SnapshotRef) -> bool:
        if ref.destination != self.name:
            return False
        prefix = f"{self.name}:"
        if not ref.snapshot_id.startswith(prefix):
            return False
        generation_id = ref.snapshot_id[len(prefix) :]
        target = self._path(generation_id)
        try:
            payload = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        return payload.get("generation_id") == generation_id

    def generations(self) -> list[str]:
        if not self._root.exists():
            return []
        return sorted(path.stem for path in self._root.glob("*.json"))
