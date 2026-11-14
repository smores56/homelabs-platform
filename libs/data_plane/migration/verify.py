"""Deterministic source/replica comparison for the data-loss gate.

The migration never trusts a row count alone. A replica is safe only when it
has every source ID, the same ID set (no drift), an identical SHA-256 for each
ID, and tombstones that were audited and propagated. Missing IDs, count drift,
hash mismatch, and unaudited tombstones all fail the gate.

Snapshots are plain JSON so a shadow run can export them without a database::

    {
      "name": "source",
      "entries": [{"id": "asset-1", "sha256": "<64 hex>"}],
      "tombstones": [{"id": "asset-2", "audited": true}]
    }
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from ..errors import ValidationError
from .phases import MigrationError

_HEX = frozenset("0123456789abcdef")


class DataLossError(MigrationError):
    """The replica cannot be shown to hold every source record."""

    def __init__(self, comparison: "Comparison") -> None:
        self.comparison = comparison
        details = comparison.failure_details()
        super().__init__(
            f"replica {comparison.replica!r} is not a lossless copy of "
            f"{comparison.source!r}: " + "; ".join(details)
        )


def _sha256(value: Any) -> str:
    text = value.strip().lower() if isinstance(value, str) else ""
    if len(text) != 64 or not set(text) <= _HEX:
        return ""
    return text


@dataclass(frozen=True)
class SnapshotEntry:
    """One stable source ID and the hash of its complete content."""

    item_id: str
    sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.item_id, str) or not self.item_id.strip():
            raise ValidationError("snapshot entry has an empty id")
        if not isinstance(self.sha256, str) or not self.sha256.strip():
            raise ValidationError(
                f"snapshot entry {self.item_id!r} has an empty hash"
            )

    def to_dict(self) -> dict[str, str]:
        return {"id": self.item_id, "sha256": self.sha256}


@dataclass(frozen=True)
class Tombstone:
    """A deletion the source intended to propagate."""

    item_id: str
    audited: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.item_id, str) or not self.item_id.strip():
            raise ValidationError("tombstone has an empty id")

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.item_id, "audited": self.audited}


@dataclass(frozen=True)
class Snapshot:
    """One side of the comparison: every live ID plus every tombstone."""

    name: str
    entries: tuple[SnapshotEntry, ...] = ()
    tombstones: tuple[Tombstone, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "entries", tuple(self.entries))
        object.__setattr__(self, "tombstones", tuple(self.tombstones))
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValidationError("snapshot has an empty name")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "Snapshot":
        if not isinstance(value, Mapping):
            raise ValidationError("snapshot is not an object")
        raw_entries = value.get("entries", ())
        raw_tombstones = value.get("tombstones", ())
        if not isinstance(raw_entries, (list, tuple)):
            raise ValidationError("snapshot entries are not a list")
        if not isinstance(raw_tombstones, (list, tuple)):
            raise ValidationError("snapshot tombstones are not a list")
        entries: list[SnapshotEntry] = []
        for index, item in enumerate(raw_entries):
            if not isinstance(item, Mapping):
                raise ValidationError(f"snapshot entries[{index}] is not an object")
            entries.append(
                SnapshotEntry(
                    item_id=_text(item.get("id", item.get("item_id"))),
                    sha256=_text(item.get("sha256", item.get("hash"))),
                )
            )
        tombstones: list[Tombstone] = []
        for index, item in enumerate(raw_tombstones):
            if not isinstance(item, Mapping):
                raise ValidationError(
                    f"snapshot tombstones[{index}] is not an object"
                )
            tombstones.append(
                Tombstone(
                    item_id=_text(item.get("id", item.get("item_id"))),
                    audited=item.get("audited") is True,
                )
            )
        return cls(
            name=_text(value.get("name")),
            entries=tuple(entries),
            tombstones=tuple(tombstones),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "entries": [entry.to_dict() for entry in self.entries],
            "tombstones": [item.to_dict() for item in self.tombstones],
        }


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


@dataclass(frozen=True)
class Comparison:
    """Structural differences between a source and its replica snapshot."""

    source: str
    replica: str
    source_count: int
    replica_count: int
    missing_ids: tuple[str, ...] = ()
    extra_ids: tuple[str, ...] = ()
    hash_mismatches: tuple[tuple[str, str, str], ...] = ()
    duplicate_ids: tuple[str, ...] = ()
    invalid_hashes: tuple[str, ...] = ()
    unaudited_tombstones: tuple[str, ...] = ()
    missing_tombstones: tuple[str, ...] = ()
    resurrected_ids: tuple[str, ...] = ()
    deleted_in_replica: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in (
            "missing_ids",
            "extra_ids",
            "hash_mismatches",
            "duplicate_ids",
            "invalid_hashes",
            "unaudited_tombstones",
            "missing_tombstones",
            "resurrected_ids",
            "deleted_in_replica",
        ):
            object.__setattr__(self, name, tuple(getattr(self, name)))

    @property
    def count_drift(self) -> bool:
        return self.source_count != self.replica_count

    @property
    def ok(self) -> bool:
        return not any(
            (
                self.count_drift,
                self.missing_ids,
                self.extra_ids,
                self.hash_mismatches,
                self.duplicate_ids,
                self.invalid_hashes,
                self.unaudited_tombstones,
                self.missing_tombstones,
                self.resurrected_ids,
                self.deleted_in_replica,
            )
        )

    def failure_details(self) -> tuple[str, ...]:
        details: list[str] = []
        if self.count_drift:
            details.append(
                f"count drift source={self.source_count} replica={self.replica_count}"
            )
        if self.missing_ids:
            details.append(f"missing IDs: {_brief(self.missing_ids)}")
        if self.extra_ids:
            details.append(f"unexpected IDs: {_brief(self.extra_ids)}")
        if self.hash_mismatches:
            ids = [item[0] for item in self.hash_mismatches]
            details.append(f"hash mismatch: {_brief(ids)}")
        if self.duplicate_ids:
            details.append(f"duplicate IDs: {_brief(self.duplicate_ids)}")
        if self.invalid_hashes:
            details.append(f"invalid hashes: {_brief(self.invalid_hashes)}")
        if self.unaudited_tombstones:
            details.append(f"unaudited tombstones: {_brief(self.unaudited_tombstones)}")
        if self.missing_tombstones:
            details.append(f"unpropagated tombstones: {_brief(self.missing_tombstones)}")
        if self.resurrected_ids:
            details.append(f"resurrected IDs: {_brief(self.resurrected_ids)}")
        if self.deleted_in_replica:
            details.append(
                f"IDs deleted only in replica: {_brief(self.deleted_in_replica)}"
            )
        return tuple(details)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "source": self.source,
            "replica": self.replica,
            "source_count": self.source_count,
            "replica_count": self.replica_count,
            "count_drift": self.count_drift,
            "missing_ids": list(self.missing_ids),
            "extra_ids": list(self.extra_ids),
            "hash_mismatches": [list(item) for item in self.hash_mismatches],
            "duplicate_ids": list(self.duplicate_ids),
            "invalid_hashes": list(self.invalid_hashes),
            "unaudited_tombstones": list(self.unaudited_tombstones),
            "missing_tombstones": list(self.missing_tombstones),
            "resurrected_ids": list(self.resurrected_ids),
            "deleted_in_replica": list(self.deleted_in_replica),
            "details": list(self.failure_details()),
        }


def _brief(items: tuple[str, ...], limit: int = 8) -> str:
    shown = ", ".join(items[:limit])
    return shown + (f" (+{len(items) - limit} more)" if len(items) > limit else "")


def _entry_map(
    snapshot: Snapshot, side: str, duplicates: list[str]
) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for entry in snapshot.entries:
        if entry.item_id in mapping:
            duplicates.append(f"{side}:{entry.item_id}")
            continue
        mapping[entry.item_id] = entry.sha256
    return mapping


def compare(source: Snapshot, replica: Snapshot) -> Comparison:
    """Compare two snapshots field by field, deterministically."""

    duplicates: list[str] = []
    source_entries = _entry_map(source, "source", duplicates)
    replica_entries = _entry_map(replica, "replica", duplicates)

    invalid: list[str] = []
    for side, entries in (("source", source_entries), ("replica", replica_entries)):
        for item_id, value in entries.items():
            if not _sha256(value):
                invalid.append(f"{side}:{item_id}")

    source_ids = set(source_entries)
    replica_ids = set(replica_entries)
    missing = tuple(sorted(source_ids - replica_ids))
    extra = tuple(sorted(replica_ids - source_ids))
    mismatches = tuple(
        (item_id, source_entries[item_id], replica_entries[item_id])
        for item_id in sorted(source_ids & replica_ids)
        if _sha256(source_entries[item_id])
        and _sha256(replica_entries[item_id])
        and source_entries[item_id].lower() != replica_entries[item_id].lower()
    )

    source_tombstones = {item.item_id for item in source.tombstones}
    replica_tombstones = {item.item_id for item in replica.tombstones}
    unaudited = tuple(
        sorted(
            f"{side}:{item.item_id}"
            for side, items in (
                ("source", source.tombstones),
                ("replica", replica.tombstones),
            )
            for item in items
            if not item.audited
        )
    )
    missing_tombstones = tuple(sorted(source_tombstones - replica_tombstones))
    resurrected = tuple(sorted(source_tombstones & replica_ids))
    deleted_in_replica = tuple(sorted(replica_tombstones & source_ids))

    return Comparison(
        source=source.name,
        replica=replica.name,
        source_count=len(source.entries),
        replica_count=len(replica.entries),
        missing_ids=missing,
        extra_ids=extra,
        hash_mismatches=mismatches,
        duplicate_ids=tuple(duplicates),
        invalid_hashes=tuple(invalid),
        unaudited_tombstones=unaudited,
        missing_tombstones=missing_tombstones,
        resurrected_ids=resurrected,
        deleted_in_replica=deleted_in_replica,
    )


def assert_no_data_loss(source: Snapshot, replica: Snapshot) -> Comparison:
    """Return the comparison only when the replica is a lossless copy."""

    comparison = compare(source, replica)
    if not comparison.ok:
        raise DataLossError(comparison)
    return comparison


def compare_evidence(evidence: Mapping[str, Any]) -> Comparison:
    """Build both snapshots from one evidence object and compare them."""

    if not isinstance(evidence, Mapping):
        raise ValidationError("snapshot evidence is not an object")
    if "source" not in evidence or "replica" not in evidence:
        raise ValidationError("evidence needs both 'source' and 'replica'")
    return compare(
        Snapshot.from_dict(evidence["source"]),
        Snapshot.from_dict(evidence["replica"]),
    )
