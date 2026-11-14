"""Command-line surface for the portable data-plane libraries.

Every command is offline: it reads local fixtures, validates contracts and
manifests, and prints JSON/text. No command touches a cluster, a provider, or a
restic repository.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

from .backup.generations import read_manifest, manifest_hash, select_generation
from .confirmation import NormalizedOperation, operation_hash, render
from .errors import DataPlaneError
from .github.backup import GithubBackupPlan
from .memory.file_memory import FileMemory
from .model import (
    Authority,
    Capability,
    DatasetMount,
    EffectClass,
    FidelityLimit,
    FieldAuthority,
    RecoveryGroup,
    SourceContract,
    Visibility,
    validate_many,
)


def _load_json(path: str | Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _as_list(value: Any, key: str) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, Mapping) and isinstance(value.get(key), list):
        return list(value[key])
    raise DataPlaneError(f"expected a list or a mapping with {key!r}")


def _source(raw: Mapping[str, Any]) -> SourceContract:
    return SourceContract(
        source_id=str(raw["source_id"]),
        visibility=Visibility(str(raw["visibility"])),
        version=int(raw.get("version", 1)),
        recovery_group=str(raw["recovery_group"]),
        stable_id_fields=tuple(str(item) for item in raw.get("stable_id_fields", ())),
        capabilities=tuple(
            Capability(
                name=str(item["name"]),
                effect=EffectClass(str(item["effect"])),
                requires_confirmation=bool(item.get("requires_confirmation", False)),
                reversible=bool(item.get("reversible", True)),
                description=str(item.get("description", "")),
            )
            for item in raw.get("capabilities", ())
        ),
        authority=tuple(
            FieldAuthority(
                field=str(item["field"]),
                authority=Authority(str(item["authority"])),
                exportable=bool(item.get("exportable", True)),
                note=str(item.get("note", "")),
            )
            for item in raw.get("authority", ())
        ),
        revision_field=raw.get("revision_field"),
        tombstone_field=raw.get("tombstone_field"),
        attachment_hash_fields=tuple(
            str(item) for item in raw.get("attachment_hash_fields", ())
        ),
        fidelity_limits=tuple(
            FidelityLimit(field=str(item["field"]), reason=str(item["reason"]))
            for item in raw.get("fidelity_limits", ())
        ),
        epoch=int(raw.get("epoch", 1)),
        notes=str(raw.get("notes", "")),
    )


def _recovery_group(raw: Mapping[str, Any]) -> RecoveryGroup:
    return RecoveryGroup(
        group_id=str(raw["group_id"]),
        pool=str(raw["pool"]),
        parent_dataset=str(raw["parent_dataset"]),
        members=tuple(
            DatasetMount(
                name=str(item["name"]),
                dataset=str(item["dataset"]),
                mount=str(item["mount"]),
                writer=str(item.get("writer", "")),
            )
            for item in raw.get("members", ())
        ),
        required_config=tuple(str(item) for item in raw.get("required_config", ())),
        destination_tiers=tuple(
            str(item) for item in raw.get("destination_tiers", ())
        ),
        notes=str(raw.get("notes", "")),
    )


def _cmd_validate_catalog(args: argparse.Namespace) -> int:
    contracts = validate_many(_source(raw) for raw in _as_list(_load_json(args.path), "sources"))
    print(json.dumps({"sources": len(contracts), "ids": [item.source_id for item in contracts]}))
    return 0


def _cmd_validate_recovery(args: argparse.Namespace) -> int:
    groups = []
    for raw in _as_list(_load_json(args.path), "groups"):
        group = _recovery_group(raw)
        group.validate()
        groups.append(group)
    print(json.dumps({"groups": len(groups), "ids": [item.group_id for item in groups]}))
    return 0


def _cmd_render_confirmation(args: argparse.Namespace) -> int:
    operation = NormalizedOperation.from_normalized(_load_json(args.path))
    print(f"hash: {operation_hash(operation)}")
    print(render(operation))
    return 0


def _cmd_manifest_hash(args: argparse.Namespace) -> int:
    manifest = read_manifest(args.path)
    print(
        json.dumps(
            {
                "generation_id": manifest.generation_id,
                "group_id": manifest.group_id,
                "complete": manifest.complete,
                "manifest_hash": manifest_hash(manifest),
            }
        )
    )
    return 0


def _cmd_select_generation(args: argparse.Namespace) -> int:
    manifests = [read_manifest(path) for path in args.paths]
    selected = select_generation(
        manifests,
        required_destinations=args.destinations,
        required_datasets=args.datasets,
        schema_version=args.schema,
    )
    print(
        json.dumps(
            {
                "generation_id": selected.generation_id,
                "group_id": selected.group_id,
                "created_at": selected.created_at,
                "destinations": [ref.destination for ref in selected.destinations],
            }
        )
    )
    return 0


def _cmd_memory_search(args: argparse.Namespace) -> int:
    memory = FileMemory(args.root, None if args.profile == "all" else args.profile)
    records = memory.search(
        args.query, tags=tuple(args.tag or ()), include_deleted=args.include_deleted
    )
    print(json.dumps([record.as_dict() for record in records], indent=2))
    return 0


def _cmd_github_plan(args: argparse.Namespace) -> int:
    plan = GithubBackupPlan(
        owner=args.owner,
        repos=tuple(args.repos),
        out_dir=args.out,
        mirror=True,
        bundle=not args.no_bundle,
    )
    print(json.dumps(plan.commands(), indent=2))
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="data_plane", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    catalog = sub.add_parser("validate-catalog", help="validate source contracts")
    catalog.add_argument("path")
    catalog.set_defaults(handler=_cmd_validate_catalog)

    recovery = sub.add_parser("validate-recovery", help="validate recovery groups")
    recovery.add_argument("path")
    recovery.set_defaults(handler=_cmd_validate_recovery)

    confirmation = sub.add_parser(
        "render-confirmation", help="hash and render a normalized operation"
    )
    confirmation.add_argument("path")
    confirmation.set_defaults(handler=_cmd_render_confirmation)

    manifest = sub.add_parser("manifest-hash", help="read and hash a generation manifest")
    manifest.add_argument("path")
    manifest.set_defaults(handler=_cmd_manifest_hash)

    select = sub.add_parser("select-generation", help="pick the newest complete generation")
    select.add_argument("paths", nargs="+")
    select.add_argument("--destinations", nargs="+", required=True)
    select.add_argument("--datasets", nargs="+", required=True)
    select.add_argument("--schema", type=int, default=1)
    select.set_defaults(handler=_cmd_select_generation)

    memory = sub.add_parser("memory-search", help="search portable file memory")
    memory.add_argument("root")
    memory.add_argument("profile", choices=("local", "external", "all"))
    memory.add_argument("query")
    memory.add_argument("--tag", action="append")
    memory.add_argument("--include-deleted", action="store_true")
    memory.set_defaults(handler=_cmd_memory_search)

    github = sub.add_parser("github-plan", help="print safe GitHub backup argv")
    github.add_argument("owner")
    github.add_argument("repos", nargs="+")
    github.add_argument("--out", required=True)
    github.add_argument("--no-bundle", action="store_true")
    github.set_defaults(handler=_cmd_github_plan)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return args.handler(args)
    except DataPlaneError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
