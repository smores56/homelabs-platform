"""Fenced snapshot fan-out into independent destinations.

The controller owns the ordering that makes a generation trustworthy: fence the
writers, checkpoint, take exactly one snapshot, resume writers, then let every
required destination capture that same snapshot on its own. A manifest is
published only after every required destination captured and verified the
generation.
"""

from __future__ import annotations

import math
import re
import time
from collections.abc import Iterable, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Protocol

from ..errors import ManifestError
from ..model import RecoveryGroup
from .destinations import Destination
from .generations import (
    MANIFEST_VERSION,
    GenerationManifest,
    SnapshotRef,
    write_manifest,
)


class Snapshotter(Protocol):
    """The host-side operations a recovery-group backup needs."""

    def fence(self, group_id: str) -> None:
        """Stop application writers so the snapshot is consistent."""

    def checkpoint(self, group_id: str) -> None:
        """Checkpoint live databases before the filesystem snapshot."""

    def snapshot(self, group_id: str) -> dict[str, str]:
        """Take one snapshot; map dataset name to its snapshot id."""

    def resume(self, group_id: str) -> None:
        """Let writers continue after the snapshot exists."""


def _generation_slug(group_id: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", group_id).strip("-")
    return slug or "group"


class FanoutController:
    """Builds one immutable generation per run and fans it out by capture."""

    def __init__(
        self,
        snapshotter: Snapshotter,
        destinations: Iterable[Destination] | Mapping[str, Destination],
        manifest_store: str | Path,
        clock=time.time,
        *,
        schema_version: int = MANIFEST_VERSION,
        release: str = "",
    ) -> None:
        self._snapshotter = snapshotter
        self._destinations = self._destination_map(destinations)
        self._manifest_store = Path(manifest_store)
        self._clock = clock
        self._schema_version = schema_version
        self._release = release

    @staticmethod
    def _destination_map(
        destinations: Iterable[Destination] | Mapping[str, Destination],
    ) -> dict[str, Destination]:
        if isinstance(destinations, Mapping):
            return dict(destinations)
        mapping: dict[str, Destination] = {}
        for destination in destinations:
            if destination.name in mapping:
                raise ManifestError(
                    f"duplicate destination name {destination.name!r}"
                )
            mapping[destination.name] = destination
        return mapping

    @staticmethod
    def _group_id(group: str | RecoveryGroup) -> str:
        if isinstance(group, RecoveryGroup):
            return group.group_id
        return str(group)

    def run(
        self,
        group: str | RecoveryGroup,
        required_destinations: Iterable[str],
        now: float | None = None,
    ) -> GenerationManifest:
        """Take one snapshot and publish it only if every destination has it."""

        group_id = self._group_id(group)
        if not group_id.strip():
            raise ManifestError("recovery group id is empty")
        required = tuple(required_destinations)
        if not required:
            raise ManifestError(f"group {group_id!r} has no required destinations")
        if len(required) != len(set(required)):
            raise ManifestError(f"group {group_id!r} repeats a required destination")
        selected: list[Destination] = []
        for name in required:
            destination = self._destinations.get(name)
            if destination is None:
                raise ManifestError(
                    f"group {group_id!r} requires unknown destination {name!r}"
                )
            selected.append(destination)

        created_at = float(self._clock() if now is None else now)
        generation_id = (
            f"{_generation_slug(group_id)}-{int(created_at * 1000)}"
        )

        self._snapshotter.fence(group_id)
        try:
            self._snapshotter.checkpoint(group_id)
            snapshot_ids = dict(self._snapshotter.snapshot(group_id))
        finally:
            self._snapshotter.resume(group_id)
        if not snapshot_ids:
            raise ManifestError(
                f"snapshot for group {group_id!r} returned no datasets"
            )

        primary_dataset = sorted(snapshot_ids)[0]
        captured: list[SnapshotRef] = []
        failures: list[str] = []
        for destination in selected:
            try:
                reference = destination.capture(generation_id, dict(snapshot_ids))
            except Exception as exc:  # a destination failure must not abort fan-out
                failures.append(f"{destination.name}: capture failed: {exc}")
                continue
            reference = replace(
                reference,
                group_id=group_id,
                dataset=reference.dataset or primary_dataset,
            )
            try:
                verified = destination.verify(reference)
            except Exception as exc:
                failures.append(f"{destination.name}: verify failed: {exc}")
                continue
            if not verified:
                failures.append(
                    f"{destination.name}: capture could not be verified"
                )
                continue
            captured.append(replace(reference, verified=True))

        if failures:
            partial = ", ".join(
                f"{item.destination}={item.snapshot_id}" for item in captured
            )
            raise ManifestError(
                f"generation {generation_id} for group {group_id!r} is incomplete; "
                f"partial captures: {partial or 'none'}; "
                + "; ".join(failures)
            )

        manifest = GenerationManifest(
            version=MANIFEST_VERSION,
            generation_id=generation_id,
            group_id=group_id,
            created_at=created_at,
            datasets=tuple(sorted(snapshot_ids.items())),
            destinations=tuple(captured),
            complete=True,
            schema_version=self._schema_version,
            release=self._release,
        )
        manifest.validate(required, snapshot_ids, self._schema_version)
        write_manifest(self._manifest_store / f"{generation_id}.json", manifest)
        return manifest

    @staticmethod
    def _by_group(
        manifests: Mapping[str, Iterable[GenerationManifest]]
        | Iterable[GenerationManifest],
    ) -> Mapping[str, Iterable[GenerationManifest]]:
        if isinstance(manifests, Mapping):
            return manifests
        grouped: dict[str, list[GenerationManifest]] = {}
        for manifest in manifests:
            grouped.setdefault(manifest.group_id, []).append(manifest)
        return grouped

    def freshness(
        self,
        group_ids: Iterable[str],
        manifests: Mapping[str, Iterable[GenerationManifest]]
        | Iterable[GenerationManifest],
        now: float | None = None,
        required_destinations: Iterable[str] | None = None,
    ) -> dict[str, dict[str, float]]:
        """Return per-destination lag in seconds; missing copies are infinite.

        Lag is measured from the destination's own capture time, not from when
        the generation event happened, so a slow destination shows its true
        staleness.
        """

        moment = float(self._clock() if now is None else now)
        grouped = self._by_group(manifests)
        result: dict[str, dict[str, float]] = {}
        for group_id in group_ids:
            newest: dict[str, float] = {}
            for manifest in grouped.get(group_id, ()):
                if not manifest.complete:
                    continue
                for reference in manifest.destinations:
                    if not reference.verified:
                        continue
                    current = newest.get(reference.destination)
                    if current is None or reference.created_at > current:
                        newest[reference.destination] = reference.created_at
            names = (
                list(required_destinations)
                if required_destinations is not None
                else sorted(newest)
            )
            result[group_id] = {
                name: (
                    max(0.0, moment - newest[name])
                    if name in newest
                    else math.inf
                )
                for name in names
            }
        return result

    def oldest_unprotected(
        self,
        group_ids: Iterable[str],
        manifests: Mapping[str, Iterable[GenerationManifest]]
        | Iterable[GenerationManifest],
        now: float | None = None,
    ) -> dict[str, float]:
        """Seconds since the newest complete generation, infinite when none."""

        moment = float(self._clock() if now is None else now)
        grouped = self._by_group(manifests)
        result: dict[str, float] = {}
        for group_id in group_ids:
            times = [
                manifest.created_at
                for manifest in grouped.get(group_id, ())
                if manifest.complete
                and any(item.verified for item in manifest.destinations)
            ]
            result[group_id] = (
                max(0.0, moment - max(times)) if times else math.inf
            )
        return result
