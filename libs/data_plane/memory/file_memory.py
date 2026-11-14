"""Portable per-profile file memory: one JSON record per file, no network.

The layout is ``<root>/<profile>/<id>.json`` so one profile directory can be
copied or restored on its own. Records are immutable values. A deletion writes
a tombstone that keeps the identifier and provenance but drops the content, so
a replica learns about the delete without retaining the deleted text.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from ..errors import ContractError, ValidationError

PROFILES = ("local", "external")
PROVENANCE_FIELDS = ("source_id", "revision", "captured_at")
_RECORD_KEYS = (
    "id",
    "profile",
    "source",
    "content",
    "tags",
    "provenance",
    "created_at",
    "updated_at",
    "sha256",
)
_JSON_SUFFIX = ".json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _digest(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _is_digest(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _timestamp(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"memory record {field_name} must be an ISO 8601 string")
    try:
        datetime.fromisoformat(value)
    except ValueError as error:
        raise ValidationError(
            f"memory record {field_name} is not ISO 8601: {value!r}"
        ) from error
    return value


def _validate_profile(profile: object, *, field_name: str = "profile") -> str:
    if profile not in PROFILES:
        raise ValidationError(
            f"memory {field_name} must be one of {', '.join(PROFILES)}, got {profile!r}"
        )
    return str(profile)


def _validate_record_id(record_id: object) -> str:
    if not isinstance(record_id, str) or not record_id.strip():
        raise ValidationError("memory record id must be non-empty")
    if record_id in {".", ".."} or "/" in record_id or "\\" in record_id:
        raise ValidationError(f"memory record id {record_id!r} is not a flat filename")
    return record_id


def _validate_tags(tags: object) -> tuple[str, ...]:
    if isinstance(tags, str):
        raise ValidationError("memory record tags must be a sequence, not a string")
    if not isinstance(tags, Iterable):
        raise ValidationError("memory record tags must be a sequence")
    normalized: list[str] = []
    for tag in tags:
        if not isinstance(tag, str) or not tag.strip():
            raise ValidationError(f"memory record tag must be non-empty, got {tag!r}")
        if tag not in normalized:
            normalized.append(tag)
    return tuple(normalized)


def _validate_provenance(provenance: object) -> Mapping[str, str]:
    if not isinstance(provenance, Mapping):
        raise ValidationError("memory record provenance must be a mapping")
    normalized = dict(provenance)
    missing = [name for name in PROVENANCE_FIELDS if name not in normalized]
    if missing:
        raise ValidationError(
            f"memory record provenance is missing {', '.join(missing)}"
        )
    for name, value in normalized.items():
        if not isinstance(name, str) or not isinstance(value, str):
            raise ValidationError("memory provenance keys and values must be strings")
    return normalized


@dataclass(frozen=True)
class MemoryRecord:
    """One portable memory record, addressed by a stable id within a profile."""

    id: str
    profile: str
    source: str
    content: str
    tags: tuple[str, ...] = ()
    provenance: Mapping[str, str] = field(default_factory=dict)
    created_at: str = ""
    updated_at: str = ""
    sha256: str = ""
    deleted: bool = False

    def __post_init__(self) -> None:
        _validate_record_id(self.id)
        _validate_profile(self.profile)
        if not isinstance(self.source, str) or not self.source.strip():
            raise ValidationError("memory record source must be non-empty")
        if not isinstance(self.content, str):
            raise ValidationError("memory record content must be a string")
        if not isinstance(self.deleted, bool):
            raise ValidationError("memory record deleted must be a bool")

        object.__setattr__(self, "tags", _validate_tags(self.tags))
        object.__setattr__(self, "provenance", _validate_provenance(self.provenance))

        created_at = _timestamp(self.created_at or _now(), "created_at")
        updated_at = _timestamp(self.updated_at or created_at, "updated_at")
        object.__setattr__(self, "created_at", created_at)
        object.__setattr__(self, "updated_at", updated_at)

        digest = _digest(self.content)
        if not self.sha256:
            object.__setattr__(self, "sha256", digest)
        elif not _is_digest(self.sha256):
            raise ValidationError(f"memory record sha256 {self.sha256!r} is not a SHA-256")
        elif self.sha256 != digest:
            raise ValidationError(
                f"memory record {self.id!r} sha256 does not match its content"
            )

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> MemoryRecord:
        if not isinstance(payload, Mapping):
            raise ValidationError("memory record must be a mapping")
        missing = [key for key in _RECORD_KEYS if key not in payload]
        if missing:
            raise ValidationError(
                f"memory record is missing {', '.join(sorted(missing))}"
            )
        return cls(
            id=payload["id"],
            profile=payload["profile"],
            source=payload["source"],
            content=payload["content"],
            tags=payload["tags"],
            provenance=payload["provenance"],
            created_at=payload["created_at"],
            updated_at=payload["updated_at"],
            sha256=payload["sha256"],
            deleted=payload.get("deleted", False),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "profile": self.profile,
            "source": self.source,
            "content": self.content,
            "tags": list(self.tags),
            "provenance": dict(self.provenance),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "sha256": self.sha256,
            "deleted": self.deleted,
        }


def _supersedes(incoming: MemoryRecord, existing: MemoryRecord) -> bool:
    """The newest revision wins; a tombstone wins a timestamp tie."""

    if incoming.updated_at != existing.updated_at:
        return incoming.updated_at > existing.updated_at
    return incoming.deleted and not existing.deleted


class FileMemory:
    """File-backed memory for one profile, or an administrative view of all."""

    def __init__(self, root: str | os.PathLike[str], profile: str | None = None) -> None:
        if profile is not None and profile not in PROFILES:
            raise ContractError(
                f"memory profile must be one of {', '.join(PROFILES)}, got {profile!r}"
            )
        self.root = Path(root)
        self.profile = profile

    def profile_dir(self, profile: str) -> Path:
        if profile not in PROFILES:
            raise ContractError(
                f"memory profile must be one of {', '.join(PROFILES)}, got {profile!r}"
            )
        return self.root / profile

    def get(self, record_id: str) -> MemoryRecord | None:
        _validate_record_id(record_id)
        if self.profile is not None:
            return self._read(self.profile, record_id)

        matches = [
            record
            for profile in PROFILES
            if (record := self._read(profile, record_id)) is not None
        ]
        if len(matches) > 1:
            raise ContractError(
                f"memory record {record_id!r} exists in more than one profile"
            )
        return matches[0] if matches else None

    def put(self, record: MemoryRecord) -> MemoryRecord:
        self._require_profile("put")
        if record.profile != self.profile:
            raise ContractError(
                f"memory record {record.id!r} belongs to profile {record.profile!r}, "
                f"not {self.profile!r}"
            )
        _validate_record_id(record.id)
        self._write(record)
        return record

    def delete(self, record_id: str) -> MemoryRecord:
        self._require_profile("delete")
        existing = self.get(record_id)
        if existing is None:
            raise ValidationError(f"memory record {record_id!r} does not exist")

        tombstone = MemoryRecord(
            id=existing.id,
            profile=existing.profile,
            source=existing.source,
            content="",
            tags=existing.tags,
            provenance=existing.provenance,
            created_at=existing.created_at,
            updated_at=_now(),
            deleted=True,
        )
        self._write(tombstone)
        return tombstone

    def search(
        self,
        query: str,
        tags: Sequence[str] | None = None,
        include_deleted: bool = False,
    ) -> tuple[MemoryRecord, ...]:
        if not isinstance(query, str):
            raise ValidationError("memory query must be a string")
        wanted = set(_validate_tags(tuple(tags or ())))
        needle = query.casefold()

        results: list[MemoryRecord] = []
        for profile in self._profiles():
            for record in self._read_all(profile):
                if record.deleted and not include_deleted:
                    continue
                if wanted and not wanted <= set(record.tags):
                    continue
                if needle and needle not in record.content.casefold() and not any(
                    needle in tag.casefold() for tag in record.tags
                ):
                    continue
                results.append(record)

        results.sort(key=lambda record: record.id)
        results.sort(key=lambda record: record.updated_at, reverse=True)
        return tuple(results)

    def export(self) -> list[dict[str, object]]:
        records = [record for profile in self._profiles() for record in self._read_all(profile)]
        records.sort(key=lambda record: (record.profile, record.id))
        return [record.as_dict() for record in records]

    def import_records(
        self, records: Iterable[Mapping[str, object]]
    ) -> tuple[MemoryRecord, ...]:
        self._require_profile("import_records")
        incoming = tuple(MemoryRecord.from_dict(payload) for payload in records)
        for record in incoming:
            if record.profile != self.profile:
                raise ContractError(
                    f"memory record {record.id!r} belongs to profile {record.profile!r}; "
                    f"a {self.profile!r} memory refuses cross-profile imports"
                )

        written: list[MemoryRecord] = []
        for record in incoming:
            existing = self._read(record.profile, record.id)
            if existing is not None and not _supersedes(record, existing):
                continue
            self._write(record)
            written.append(record)
        return tuple(written)

    def _profiles(self) -> tuple[str, ...]:
        return PROFILES if self.profile is None else (self.profile,)

    def _require_profile(self, operation: str) -> None:
        if self.profile is None:
            raise ContractError(
                f"{operation} needs one memory profile; construct FileMemory(root, profile)"
            )

    def _path(self, profile: str, record_id: str) -> Path:
        return self.profile_dir(profile) / f"{record_id}{_JSON_SUFFIX}"

    def _read(self, profile: str, record_id: str) -> MemoryRecord | None:
        path = self._path(profile, record_id)
        if not path.is_file():
            return None
        return self._load(path)

    def _read_all(self, profile: str) -> list[MemoryRecord]:
        directory = self.profile_dir(profile)
        if not directory.is_dir():
            return []
        return [
            self._load(path)
            for path in sorted(directory.glob(f"*{_JSON_SUFFIX}"))
            if not path.name.startswith(".")
        ]

    def _load(self, path: Path) -> MemoryRecord:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise ValidationError(f"memory record {path} is unreadable: {error}") from error
        try:
            return MemoryRecord.from_dict(payload)
        except ValidationError as error:
            raise ValidationError(f"memory record {path} is invalid: {error}") from error

    def _write(self, record: MemoryRecord) -> None:
        directory = self.profile_dir(record.profile)
        directory.mkdir(parents=True, exist_ok=True)
        path = self._path(record.profile, record.id)
        body = json.dumps(record.as_dict(), indent=2, sort_keys=True, ensure_ascii=False)

        descriptor, temporary = tempfile.mkstemp(
            dir=directory, prefix=f".{record.id}.", suffix=".tmp"
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(body + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise
