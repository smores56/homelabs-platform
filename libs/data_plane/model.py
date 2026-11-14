"""Portable contract types for the Nix-free data plane.

These types are deliberately service-independent: a source contract says what
data a source owns, who is authoritative for each field, which capabilities it
exposes, and which recovery group protects it. Nothing here imports Kubernetes,
Nix, or a provider SDK.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Iterable, Iterator

from .errors import ContractError


class Visibility(str, Enum):
    PERSONAL = "personal"
    GENERAL = "general"
    INFRASTRUCTURE = "infrastructure"


class Authority(str, Enum):
    PORTABLE_FILE = "portable_file"
    APPLICATION = "application"
    PROVIDER = "provider"
    HUMAN = "human"


class EffectClass(str, Enum):
    READ = "read"
    SEARCH = "search"
    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"
    BULK = "bulk"
    IRREVERSIBLE = "irreversible"
    RECLASSIFY = "reclassify"


class IntentState(str, Enum):
    PROPOSED = "proposed"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    APPROVED = "approved"
    REJECTED = "rejected"
    APPLYING = "applying"
    APPLIED = "applied"
    FAILED = "failed"
    OUTCOME_UNKNOWN = "outcome_unknown"
    EXPIRED = "expired"
    SUPERSEDED = "superseded"


# Effects that must never execute without a human confirmation boundary.
CONFIRMATION_EFFECTS = frozenset(
    {
        EffectClass.DELETE,
        EffectClass.BULK,
        EffectClass.IRREVERSIBLE,
        EffectClass.RECLASSIFY,
    }
)

_READ_EFFECTS = frozenset({EffectClass.READ, EffectClass.SEARCH})


@dataclass(frozen=True)
class FidelityLimit:
    """A provider or source field that cannot be preserved faithfully."""

    field: str
    reason: str

    def validate(self) -> None:
        if not self.field.strip():
            raise ContractError("fidelity limit has an empty field")
        if not self.reason.strip():
            raise ContractError(f"fidelity limit {self.field!r} has no reason")


@dataclass(frozen=True)
class FieldAuthority:
    """Who owns one field. Authority is per field, never per database."""

    field: str
    authority: Authority
    exportable: bool = True
    note: str = ""

    def validate(self) -> None:
        if not self.field.strip():
            raise ContractError("field authority has an empty field")
        if not isinstance(self.authority, Authority):
            raise ContractError(f"field {self.field!r} has no Authority")


@dataclass(frozen=True)
class Capability:
    """One typed operation a source exposes."""

    name: str
    effect: EffectClass
    requires_confirmation: bool = False
    reversible: bool = True
    description: str = ""

    def validate(self) -> None:
        if not self.name.strip():
            raise ContractError("capability has an empty name")
        if not isinstance(self.effect, EffectClass):
            raise ContractError(f"capability {self.name!r} has no EffectClass")
        if self.effect in CONFIRMATION_EFFECTS and not self.requires_confirmation:
            raise ContractError(
                f"capability {self.name!r} is {self.effect.value} and must require confirmation"
            )
        if self.effect in _READ_EFFECTS and self.requires_confirmation:
            raise ContractError(
                f"capability {self.name!r} is {self.effect.value} and must not require confirmation"
            )


@dataclass(frozen=True)
class SourceContract:
    """Everything the data plane needs to know about one source."""

    source_id: str
    visibility: Visibility
    version: int
    recovery_group: str
    stable_id_fields: tuple[str, ...]
    capabilities: tuple[Capability, ...]
    authority: tuple[FieldAuthority, ...] = ()
    revision_field: str | None = None
    tombstone_field: str | None = None
    attachment_hash_fields: tuple[str, ...] = ()
    fidelity_limits: tuple[FidelityLimit, ...] = ()
    epoch: int = 1
    notes: str = ""

    def validate(self) -> None:
        if not self.source_id.strip():
            raise ContractError("source_id is empty")
        if not isinstance(self.visibility, Visibility):
            raise ContractError(f"source {self.source_id!r} has no Visibility")
        if self.version < 1:
            raise ContractError(f"source {self.source_id!r} version must be >= 1")
        if self.epoch < 1:
            raise ContractError(f"source {self.source_id!r} epoch must be >= 1")
        if not self.recovery_group.strip():
            raise ContractError(f"source {self.source_id!r} has no recovery group")
        if not self.stable_id_fields:
            raise ContractError(f"source {self.source_id!r} has no stable ID fields")
        if not self.capabilities:
            raise ContractError(f"source {self.source_id!r} has no capabilities")

        names = [capability.name for capability in self.capabilities]
        if len(names) != len(set(names)):
            raise ContractError(f"source {self.source_id!r} has duplicate capabilities")
        for capability in self.capabilities:
            capability.validate()

        fields = [item.field for item in self.authority]
        if len(fields) != len(set(fields)):
            raise ContractError(f"source {self.source_id!r} has duplicate field authority")
        for item in self.authority:
            item.validate()
        for item in self.fidelity_limits:
            item.validate()

        has_update = any(
            capability.effect in {EffectClass.UPDATE, EffectClass.IRREVERSIBLE}
            for capability in self.capabilities
        )
        has_delete = any(
            capability.effect is EffectClass.DELETE for capability in self.capabilities
        )
        if has_update and not self.revision_field:
            raise ContractError(
                f"source {self.source_id!r} exposes update without a revision field"
            )
        if has_delete and not self.tombstone_field:
            raise ContractError(
                f"source {self.source_id!r} exposes delete without a tombstone field"
            )

    def capability(self, name: str) -> Capability:
        for capability in self.capabilities:
            if capability.name == name:
                return capability
        raise ContractError(f"source {self.source_id!r} has no capability {name!r}")

    @property
    def writable(self) -> bool:
        return any(
            capability.effect not in _READ_EFFECTS for capability in self.capabilities
        )


@dataclass(frozen=True)
class DatasetMount:
    """One dataset in a recovery group and its idiomatic mount path."""

    name: str
    dataset: str
    mount: str
    writer: str = ""

    def validate(self, parent_dataset: str) -> None:
        if not self.name.strip():
            raise ContractError("dataset mount has an empty name")
        if not self.dataset.strip():
            raise ContractError(f"dataset {self.name!r} has no dataset path")
        if self.dataset != parent_dataset and not self.dataset.startswith(
            parent_dataset.rstrip("/") + "/"
        ):
            raise ContractError(
                f"dataset {self.dataset!r} is not under recovery parent {parent_dataset!r}"
            )
        if not self.mount.startswith("/"):
            raise ContractError(f"dataset {self.name!r} mount must be absolute")
        if ".." in self.mount.split("/"):
            raise ContractError(f"dataset {self.name!r} mount escapes its root")


@dataclass(frozen=True)
class RecoveryGroup:
    """One ZFS parent dataset and every mutable member that shares its point."""

    group_id: str
    pool: str
    parent_dataset: str
    members: tuple[DatasetMount, ...]
    required_config: tuple[str, ...] = ()
    destination_tiers: tuple[str, ...] = ()
    notes: str = ""

    def validate(self) -> None:
        if not self.group_id.strip():
            raise ContractError("recovery group has an empty id")
        if not self.pool.strip():
            raise ContractError(f"recovery group {self.group_id!r} has no pool")
        if not self.parent_dataset.strip():
            raise ContractError(f"recovery group {self.group_id!r} has no parent dataset")
        if not self.members:
            raise ContractError(f"recovery group {self.group_id!r} has no members")
        for member in self.members:
            member.validate(self.parent_dataset)
        mounts = [member.mount for member in self.members]
        if len(mounts) != len(set(mounts)):
            raise ContractError(f"recovery group {self.group_id!r} has duplicate mounts")
        for path in self.required_config:
            if not path.startswith("/"):
                raise ContractError(
                    f"recovery group {self.group_id!r} config path {path!r} is not absolute"
                )


def validate_many(contracts: Iterable[SourceContract]) -> tuple[SourceContract, ...]:
    """Validate a collection and reject duplicate source ids."""

    items = tuple(contracts)
    seen: set[str] = set()
    for contract in items:
        contract.validate()
        if contract.source_id in seen:
            raise ContractError(f"duplicate source id {contract.source_id!r}")
        seen.add(contract.source_id)
    return items


def write_capabilities(contract: SourceContract) -> Iterator[Capability]:
    for capability in contract.capabilities:
        if capability.effect not in _READ_EFFECTS:
            yield capability
