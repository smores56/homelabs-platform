"""JSON descriptors for every collector record shape.

Each descriptor is plain JSON Schema plus two vendor extensions: ``x-relay``
carries the ingress envelope limits and ``x-contract`` carries identity,
revision, tombstone, attachment-hash, and fidelity fields. Loading one yields
the same :class:`RecordSchema` and :class:`FidelityDeclaration` the runtime
enforces, so a test can validate a sample record against exactly what a
collector must submit.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from ...errors import ContractError, ValidationError
from ...model import FidelityLimit
from ..fidelity import FidelityDeclaration
from ..relay import RecordSchema

SCHEMA_DIR = Path(__file__).resolve().parent

_JSON_TYPES: dict[str, type] = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
    "array": list,
    "object": dict,
}


def _python_type(name: str, schema: Mapping[str, Any]) -> type:
    declared = schema.get("type")
    if declared is None and "const" in schema:
        declared = (
            "boolean"
            if isinstance(schema["const"], bool)
            else "string"
            if isinstance(schema["const"], str)
            else "integer"
            if isinstance(schema["const"], int)
            else None
        )
    if declared not in _JSON_TYPES:
        raise ValidationError(
            f"schema field {name!r} has unsupported JSON type {declared!r}"
        )
    return _JSON_TYPES[declared]


@dataclass(frozen=True)
class SchemaDescriptor:
    """One source's record shape, identity rules, and declared fidelity gaps."""

    name: str
    title: str
    source_id: str
    properties: Mapping[str, Mapping[str, Any]]
    required_fields: tuple[str, ...]
    max_bytes: int
    max_records_per_second: int
    stable_id_fields: tuple[str, ...]
    revision_field: str
    tombstone_field: str
    attachment_hash_fields: tuple[str, ...]
    fidelity_limits: tuple[FidelityLimit, ...]

    @property
    def preserved_fields(self) -> tuple[str, ...]:
        return tuple(self.properties)

    def field_types(self) -> dict[str, type]:
        return {
            name: _python_type(name, schema) for name, schema in self.properties.items()
        }

    def validate(self) -> None:
        if not self.name.strip():
            raise ContractError("collector schema has an empty name")
        if not self.title.strip():
            raise ContractError(f"collector schema {self.name!r} has no title")
        if not self.source_id.strip():
            raise ContractError(f"collector schema {self.name!r} has no source id")
        if not self.properties:
            raise ContractError(f"collector schema {self.name!r} has no fields")
        for field_name, schema in self.properties.items():
            if not isinstance(field_name, str) or not field_name.strip():
                raise ContractError(f"collector schema {self.name!r} has an empty field")
            _python_type(field_name, schema)

        if len(self.required_fields) != len(set(self.required_fields)):
            raise ContractError(
                f"collector schema {self.name!r} has duplicate required fields"
            )
        unknown = sorted(set(self.required_fields) - set(self.properties))
        if unknown:
            raise ContractError(
                f"collector schema {self.name!r} requires undeclared fields {unknown!r}"
            )
        if not self.required_fields:
            raise ContractError(f"collector schema {self.name!r} has no required fields")

        for field_name in ("source_id", self.tombstone_field, self.revision_field):
            if field_name not in self.required_fields:
                raise ContractError(
                    f"collector schema {self.name!r} must require {field_name!r}"
                )
        if self.properties[self.tombstone_field].get("type") != "boolean":
            raise ContractError(
                f"collector schema {self.name!r} tombstone must be a boolean"
            )
        if self.properties[self.revision_field].get("type") != "string":
            raise ContractError(
                f"collector schema {self.name!r} revision must be a string"
            )

        if not self.stable_id_fields:
            raise ContractError(f"collector schema {self.name!r} has no stable id")
        for field_name in self.stable_id_fields:
            if field_name not in self.required_fields:
                raise ContractError(
                    f"collector schema {self.name!r} stable id {field_name!r} "
                    "is not required"
                )
            if self.properties[field_name].get("type") != "string":
                raise ContractError(
                    f"collector schema {self.name!r} stable id {field_name!r} "
                    "must be a string"
                )

        for field_name in self.attachment_hash_fields:
            if field_name not in self.properties:
                raise ContractError(
                    f"collector schema {self.name!r} attachment hash {field_name!r} "
                    "is not declared"
                )

        if isinstance(self.max_bytes, bool) or not isinstance(self.max_bytes, int):
            raise ContractError(f"collector schema {self.name!r} has invalid max_bytes")
        if self.max_bytes < 1:
            raise ContractError(f"collector schema {self.name!r} has invalid max_bytes")
        if (
            isinstance(self.max_records_per_second, bool)
            or not isinstance(self.max_records_per_second, int)
            or self.max_records_per_second < 1
        ):
            raise ContractError(
                f"collector schema {self.name!r} has invalid max_records_per_second"
            )

        FidelityDeclaration(
            source_id=self.source_id,
            preserved_fields=self.preserved_fields,
            unsupported_fields=self.fidelity_limits,
        ).validate()

    def relay_schema(self) -> RecordSchema:
        self.validate()
        return RecordSchema(
            required_fields=self.required_fields,
            field_types=self.field_types(),
            max_bytes=self.max_bytes,
            max_records_per_second=self.max_records_per_second,
        )

    def fidelity_declaration(self) -> FidelityDeclaration:
        self.validate()
        return FidelityDeclaration(
            source_id=self.source_id,
            preserved_fields=self.preserved_fields,
            unsupported_fields=self.fidelity_limits,
        )

    def validate_record(self, record: Mapping[str, Any]) -> None:
        self.relay_schema().validate(record)
        if record.get("source_id") != self.source_id:
            raise ValidationError(
                f"record source {record.get('source_id')!r} is not {self.source_id!r}"
            )
        for field_name in self.stable_id_fields:
            value = record.get(field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValidationError(
                    f"record has no stable id in {field_name!r}"
                )
        revision = record.get(self.revision_field)
        if not isinstance(revision, str) or not revision.strip():
            raise ValidationError("record has no revision")


def schema_names() -> tuple[str, ...]:
    """Every descriptor in this package, sorted by file name."""

    return tuple(sorted(path.stem for path in SCHEMA_DIR.glob("*.json")))


def load_descriptor(name: str) -> SchemaDescriptor:
    if name not in schema_names():
        raise ValidationError(f"unknown collector schema {name!r}")
    path = SCHEMA_DIR / f"{name}.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValidationError(f"collector schema {path} is unreadable: {error}") from error
    if not isinstance(raw, dict):
        raise ValidationError(f"collector schema {path} must be a JSON object")

    properties = raw.get("properties")
    if not isinstance(properties, dict) or not properties:
        raise ValidationError(f"collector schema {path} has no properties")
    if not all(isinstance(value, dict) for value in properties.values()):
        raise ValidationError(f"collector schema {path} has a non-object property")
    required = raw.get("required")
    if not isinstance(required, list) or not required:
        raise ValidationError(f"collector schema {path} has no required fields")
    if not all(isinstance(item, str) and item.strip() for item in required):
        raise ValidationError(f"collector schema {path} has an invalid required field")
    source = properties.get("source_id")
    if not isinstance(source, dict) or not isinstance(source.get("const"), str):
        raise ValidationError(f"collector schema {path} has no source_id const")

    relay = raw.get("x-relay")
    if not isinstance(relay, dict):
        raise ValidationError(f"collector schema {path} has no x-relay block")
    contract = raw.get("x-contract")
    if not isinstance(contract, dict):
        raise ValidationError(f"collector schema {path} has no x-contract block")

    for name in ("revision_field", "tombstone_field"):
        if not isinstance(contract.get(name), str) or not contract[name].strip():
            raise ValidationError(f"collector schema {path} has no {name}")
    for name in ("stable_id_fields", "attachment_hash_fields"):
        values = contract.get(name)
        if not isinstance(values, list) or not all(
            isinstance(item, str) and item.strip() for item in values
        ):
            raise ValidationError(f"collector schema {path} has invalid {name}")

    limits = contract.get("fidelity_limits")
    if not isinstance(limits, list):
        raise ValidationError(f"collector schema {path} has no fidelity limits")
    fidelity_limits: list[FidelityLimit] = []
    for item in limits:
        if not isinstance(item, dict):
            raise ValidationError(f"collector schema {path} has an invalid fidelity limit")
        if not isinstance(item.get("field"), str) or not isinstance(
            item.get("reason"), str
        ):
            raise ValidationError(f"collector schema {path} has an invalid fidelity limit")
        fidelity_limits.append(FidelityLimit(field=item["field"], reason=item["reason"]))

    descriptor = SchemaDescriptor(
        name=name,
        title=str(raw.get("title", "")),
        source_id=source["const"],
        properties={str(key): value for key, value in properties.items()},
        required_fields=tuple(required),
        max_bytes=relay.get("max_bytes"),
        max_records_per_second=relay.get("max_records_per_second"),
        stable_id_fields=tuple(contract.get("stable_id_fields", ())),
        revision_field=str(contract.get("revision_field", "")),
        tombstone_field=str(contract.get("tombstone_field", "")),
        attachment_hash_fields=tuple(contract.get("attachment_hash_fields", ())),
        fidelity_limits=tuple(fidelity_limits),
    )
    descriptor.validate()
    return descriptor


def load_all() -> dict[str, SchemaDescriptor]:
    return {name: load_descriptor(name) for name in schema_names()}


def descriptor_for_source(source_id: str) -> SchemaDescriptor:
    for name in schema_names():
        descriptor = load_descriptor(name)
        if descriptor.source_id == source_id:
            return descriptor
    raise ValidationError(f"no collector schema declares source {source_id!r}")
