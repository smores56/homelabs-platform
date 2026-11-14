"""Explicit declarations of which source fields a replica preserves.

An adapter states what it keeps and what it cannot keep, with a reason for
every gap. Claiming full fidelity is a claim about the declaration, so an
overlap between the two sets is a contract error rather than a preference.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..errors import ContractError
from ..model import FidelityLimit


@dataclass(frozen=True)
class FidelityDeclaration:
    """What one replica preserves and which provider fields it cannot."""

    source_id: str
    preserved_fields: tuple[str, ...]
    unsupported_fields: tuple[FidelityLimit, ...]

    def validate(self) -> None:
        if not self.source_id.strip():
            raise ContractError("fidelity declaration has an empty source id")
        preserved = list(self.preserved_fields)
        if any(not field.strip() for field in preserved):
            raise ContractError(
                f"source {self.source_id!r} has an empty preserved field"
            )
        if len(preserved) != len(set(preserved)):
            raise ContractError(
                f"source {self.source_id!r} has duplicate preserved fields"
            )
        unsupported = [item.field for item in self.unsupported_fields]
        if len(unsupported) != len(set(unsupported)):
            raise ContractError(
                f"source {self.source_id!r} has duplicate unsupported fields"
            )
        for item in self.unsupported_fields:
            item.validate()
        overlap = sorted(set(preserved) & set(unsupported))
        if overlap:
            raise ContractError(
                f"source {self.source_id!r} lists {overlap!r} as both preserved "
                "and unsupported"
            )

    def as_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "source_id": self.source_id,
            "preserved_fields": list(self.preserved_fields),
            "unsupported_fields": [
                {"field": item.field, "reason": item.reason}
                for item in self.unsupported_fields
            ],
        }
