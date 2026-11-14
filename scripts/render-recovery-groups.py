#!/usr/bin/env python3
"""Render the deploy recovery-group inventory from the Ansible backup vars.

The host inventory (``ansible/group_vars/all.yml``) is the only source of ZFS
dataset names; ``ansible/group_vars/backup.yml`` adds backup policy. This script
joins them into ``deploy/data-plane/config/recovery-groups.json`` and can also
build the host backup plan so tests can validate both against one set of vars.

    scripts/render-recovery-groups.py            # write the JSON
    scripts/render-recovery-groups.py --check    # fail when it drifted

Stdlib only: the data-plane test suite runs on a bare ``python3`` with no
PyYAML, so a small, strict YAML-subset reader handles these two files. It
refuses constructs it does not understand instead of guessing.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
ALL_VARS = ROOT / "ansible" / "group_vars" / "all.yml"
BACKUP_VARS = ROOT / "ansible" / "group_vars" / "backup.yml"
TARGET = ROOT / "deploy" / "data-plane" / "config" / "recovery-groups.json"


class YamlSubsetError(ValueError):
    """The file uses YAML this script intentionally does not support."""


def _strip_comment(line: str) -> str:
    out: list[str] = []
    quote = ""
    escaped = False
    for index, char in enumerate(line):
        if quote:
            out.append(char)
            if escaped:
                escaped = False
            elif char == "\\" and quote == '"':
                escaped = True
            elif char == quote:
                quote = ""
            continue
        if char in "\"'":
            quote = char
            out.append(char)
        elif char == "#" and (index == 0 or line[index - 1].isspace()):
            break
        else:
            out.append(char)
    return "".join(out).rstrip()


def _unquote(text: str) -> str:
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        body = text[1:-1]
        if text[0] == "'":
            return body.replace("''", "'")
        return (
            body.replace('\\"', '"')
            .replace("\\n", "\n")
            .replace("\\t", "\t")
            .replace("\\\\", "\\")
        )
    return text


def _scalar(text: str) -> Any:
    text = text.strip()
    if not text:
        return None
    if text[0] in "[{":
        return _flow(text)
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return _unquote(text)
    if text.lower() in {"true", "false"}:
        return text.lower() == "true"
    if text.lower() in {"null", "~"}:
        return None
    if re.fullmatch(r"-?\d+", text):
        return int(text)
    if re.fullmatch(r"-?\d+\.\d+", text):
        return float(text)
    return text


def _split_flow(text: str) -> list[str]:
    body = text[1:-1]
    parts: list[str] = []
    depth = 0
    quote = ""
    current: list[str] = []
    for char in body:
        if quote:
            current.append(char)
            if char == quote:
                quote = ""
            continue
        if char in "\"'":
            quote = char
            current.append(char)
        elif char in "[{":
            depth += 1
            current.append(char)
        elif char in "]}":
            depth -= 1
            current.append(char)
        elif char == "," and depth == 0:
            parts.append("".join(current).strip())
            current = []
        else:
            current.append(char)
    if current:
        parts.append("".join(current).strip())
    return [part for part in parts if part]


def _flow(text: str) -> Any:
    text = text.strip()
    if text.startswith("[") and text.endswith("]"):
        return [_scalar(item) for item in _split_flow(text)]
    if text.startswith("{") and text.endswith("}"):
        result: dict[str, Any] = {}
        for item in _split_flow(text):
            key, _, value = item.partition(":")
            result[_unquote(key.strip())] = _scalar(value)
        return result
    raise YamlSubsetError(f"unsupported flow value: {text[:40]}")


def _looks_like_pair(text: str) -> bool:
    quote = ""
    for index, char in enumerate(text):
        if quote:
            if char == quote:
                quote = ""
            continue
        if char in "\"'":
            quote = char
        elif char == ":" and (index + 1 == len(text) or text[index + 1] == " "):
            return index > 0
        elif char in "[{":
            return False
    return False


def _split_pair(text: str) -> tuple[str, str]:
    quote = ""
    for index, char in enumerate(text):
        if quote:
            if char == quote:
                quote = ""
            continue
        if char in "\"'":
            quote = char
        elif char == ":" and (index + 1 == len(text) or text[index + 1] == " "):
            return _unquote(text[:index].strip()), text[index + 1 :].strip()
    raise YamlSubsetError(f"not a mapping entry: {text[:40]}")


def _read_lines(text: str) -> list[tuple[int, str]]:
    lines: list[tuple[int, str]] = []
    for raw in text.splitlines():
        stripped = _strip_comment(raw)
        if not stripped.strip() or stripped.strip() in {"---", "..."}:
            continue
        indent = len(stripped) - len(stripped.lstrip(" "))
        if "\t" in stripped[:indent]:
            raise YamlSubsetError("tabs are not valid indentation")
        lines.append((indent, stripped[indent:]))
    return lines


def _block_scalar(
    lines: list[tuple[int, str]], index: int, indent: int, marker: str
) -> tuple[str, int]:
    chunks: list[str] = []
    while index < len(lines) and lines[index][0] > indent:
        chunks.append(lines[index][1])
        index += 1
    if marker.startswith(">"):
        value = " ".join(chunk for chunk in chunks if chunk)
    else:
        value = "\n".join(chunks)
    if not marker.endswith("-"):
        value += "\n"
    return value, index


def _parse_block(
    lines: list[tuple[int, str]], index: int, indent: int
) -> tuple[Any, int]:
    if lines[index][1].startswith("- "):
        return _parse_sequence(lines, index, indent)
    return _parse_mapping(lines, index, indent)


def _parse_mapping(
    lines: list[tuple[int, str]], index: int, indent: int
) -> tuple[dict[str, Any], int]:
    result: dict[str, Any] = {}
    while index < len(lines) and lines[index][0] == indent:
        content = lines[index][1]
        if content.startswith("- "):
            raise YamlSubsetError("sequence entry inside a mapping")
        key, rest = _split_pair(content)
        index += 1
        if rest[:1] in {">", "|"}:
            result[key], index = _block_scalar(lines, index, indent, rest)
        elif rest:
            result[key] = _scalar(rest)
        elif index < len(lines) and lines[index][0] > indent:
            result[key], index = _parse_block(lines, index, lines[index][0])
        else:
            result[key] = None
    if index < len(lines) and lines[index][0] > indent:
        raise YamlSubsetError(f"unexpected indentation near {lines[index][1][:40]}")
    return result, index


def _parse_sequence(
    lines: list[tuple[int, str]], index: int, indent: int
) -> tuple[list[Any], int]:
    result: list[Any] = []
    while index < len(lines) and lines[index][0] == indent:
        content = lines[index][1]
        if not content.startswith("- "):
            break
        rest = content[2:].strip()
        index += 1
        if not rest:
            if index < len(lines) and lines[index][0] > indent:
                value, index = _parse_block(lines, index, lines[index][0])
            else:
                value = None
            result.append(value)
            continue
        if _looks_like_pair(rest):
            key, value_text = _split_pair(rest)
            item: dict[str, Any] = {}
            if value_text[:1] in {">", "|"}:
                item[key], index = _block_scalar(lines, index, indent, value_text)
            elif value_text:
                item[key] = _scalar(value_text)
            elif index < len(lines) and lines[index][0] > indent + 1:
                item[key], index = _parse_block(
                    lines, index, lines[index][0]
                )
            else:
                item[key] = None
            while index < len(lines) and lines[index][0] > indent:
                nested_indent = lines[index][0]
                nested, index = _parse_mapping(lines, index, nested_indent)
                item.update(nested)
            result.append(item)
        else:
            result.append(_scalar(rest))
    return result, index


def load_yaml(path: str | Path) -> Any:
    """Load the strict YAML subset used by the Ansible group vars."""

    lines = _read_lines(Path(path).read_text(encoding="utf-8"))
    if not lines:
        return {}
    value, index = _parse_block(lines, 0, lines[0][0])
    if index != len(lines):
        raise YamlSubsetError(f"trailing content near {lines[index][1][:40]}")
    return value


def resolve_templates(value: Any, variables: dict[str, Any]) -> Any:
    """Resolve simple ``{{ var }}`` references, as Ansible would at apply time."""

    if isinstance(value, dict):
        return {key: resolve_templates(item, variables) for key, item in value.items()}
    if isinstance(value, list):
        return [resolve_templates(item, variables) for item in value]
    if isinstance(value, str):
        pattern = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")

        def replace(match: re.Match[str]) -> str:
            name = match.group(1)
            if name not in variables:
                raise YamlSubsetError(f"unknown template variable {name!r}")
            return str(variables[name])

        if "{%" in value:
            return value
        return pattern.sub(replace, value)
    return value


def load_group_vars() -> tuple[dict[str, Any], dict[str, Any]]:
    raw = load_yaml(ALL_VARS)
    all_vars = resolve_templates(raw, raw)
    backup_vars = resolve_templates(load_yaml(BACKUP_VARS), {**all_vars, **raw})
    return all_vars, backup_vars


def canonical_id(name: str) -> str:
    return name.replace("_", "-")


def expected_group_ids(
    all_vars: dict[str, Any], backup_vars: dict[str, Any]
) -> list[str]:
    host = [canonical_id(name) for name in all_vars["recovery_inventory"]]
    return host + list(backup_vars["backup_pending_host_groups"])


def _common_parent(datasets: list[str]) -> str:
    parts = [dataset.split("/") for dataset in datasets]
    shared = [part for part in zip(*parts) if len(set(part)) == 1]
    return "/".join(piece[0] for piece in shared)


def group_details(
    group_id: str, all_vars: dict[str, Any], backup_vars: dict[str, Any]
) -> tuple[dict[str, Any], list[dict[str, Any]], str]:
    """Return (spec, members, parent_dataset) for one group."""

    specs = backup_vars["backup_group_specs"]
    if group_id not in specs:
        raise SystemExit(f"backup_group_specs is missing group {group_id!r}")
    spec = specs[group_id]
    pending = list(backup_vars["backup_pending_host_groups"])
    inventory_key = group_id.replace("-", "_")
    inventory = all_vars["recovery_inventory"]
    if group_id in pending:
        if inventory_key in inventory:
            raise SystemExit(
                f"{group_id!r} is pending but recovery_inventory already "
                "classifies it; remove it from backup_pending_host_groups"
            )
        if "members" not in spec:
            raise SystemExit(f"pending group {group_id!r} has no members")
        members = [dict(member) for member in spec["members"]]
        parent = spec.get("parent_dataset") or _common_parent(
            [member["dataset"] for member in members]
        )
    else:
        if inventory_key not in inventory:
            raise SystemExit(
                f"{group_id!r} is not in recovery_inventory and is not pending"
            )
        rows = [
            row
            for row in all_vars["zfs_datasets"]
            if row.get("recovery_group") == inventory_key
        ]
        if not rows:
            raise SystemExit(
                f"recovery_inventory group {inventory_key!r} has no zfs_datasets rows"
            )
        writer = spec.get("writer")
        if not writer:
            raise SystemExit(f"group {group_id!r} has no writer")
        members = [
            {
                "name": row["name"].split("/")[-1],
                "dataset": row["name"],
                "mount": row["mountpoint"],
                "writer": writer,
            }
            for row in rows
        ]
        parent = spec.get("parent_dataset") or _common_parent(
            [member["dataset"] for member in members]
        )

    if inventory_key in inventory:
        visibility = inventory[inventory_key]["visibility"]
        declared = spec.get("visibility")
        if declared is not None and declared != visibility:
            raise SystemExit(
                f"group {group_id!r} visibility {declared!r} disagrees with "
                f"recovery_inventory ({visibility!r})"
            )
    else:
        visibility = spec.get("visibility")
    if not visibility:
        raise SystemExit(f"group {group_id!r} has no visibility")
    spec = dict(spec)
    spec["visibility"] = visibility
    return spec, members, parent


def group_destinations(spec: dict[str, Any]) -> list[str]:
    tiers = spec.get("destination_tiers")
    if not tiers:
        raise SystemExit(f"group {spec.get('id', '?')!r} has no destination tiers")
    return [f"{tier}-{spec['visibility']}" for tier in tiers]


def validate(
    all_vars: dict[str, Any], backup_vars: dict[str, Any]
) -> list[str]:
    """Validate the shared plan; return the ordered group ids."""

    order = expected_group_ids(all_vars, backup_vars)
    if len(order) != len(set(order)):
        raise SystemExit("backup group ids are not unique")
    specs = backup_vars["backup_group_specs"]
    extra = sorted(set(specs) - set(order))
    if extra:
        raise SystemExit(
            "backup_group_specs declares unknown groups: " + ", ".join(extra)
        )
    repositories = backup_vars["backup_repositories"]
    names = [repository["name"] for repository in repositories]
    if len(names) != len(set(names)):
        raise SystemExit("backup_repositories repeat a name")
    destinations = set(names)
    declared = backup_vars.get("backup_destinations")
    if isinstance(declared, list) and set(declared) != destinations:
        raise SystemExit(
            "backup_destinations must be exactly the repository names"
        )
    paths = backup_vars["backup_dataset_paths"]
    seen_datasets: set[str] = set()
    for group_id in order:
        spec, members, _ = group_details(group_id, all_vars, backup_vars)
        if not members:
            raise SystemExit(f"group {group_id!r} has no members")
        for destination in group_destinations(spec):
            if destination not in destinations:
                raise SystemExit(
                    f"group {group_id!r} names unknown destination "
                    f"{destination!r}"
                )
        mounts = [member["mount"] for member in members]
        if len(mounts) != len(set(mounts)):
            raise SystemExit(f"group {group_id!r} repeats a mount")
        for member in members:
            dataset = member["dataset"]
            if dataset in seen_datasets:
                raise SystemExit(f"dataset {dataset!r} is claimed twice")
            seen_datasets.add(dataset)
            path = paths.get(dataset)
            if not path or not path.startswith("/"):
                raise SystemExit(f"dataset {dataset!r} has no absolute path")
            if "{snapshot}" not in path:
                raise SystemExit(
                    f"dataset {dataset!r} path must use {{snapshot}}"
                )
    for repository in repositories:
        if not repository.get("password_file", "").startswith("/"):
            raise SystemExit(
                f"repository {repository['name']!r} needs a password file path"
            )
    missing_paths = sorted(set(paths) - seen_datasets)
    if missing_paths:
        raise SystemExit(
            "backup_dataset_paths declares unused datasets: "
            + ", ".join(missing_paths)
        )
    return order


def build_recovery_groups(
    all_vars: dict[str, Any], backup_vars: dict[str, Any]
) -> dict[str, Any]:
    order = validate(all_vars, backup_vars)
    groups = []
    for group_id in order:
        spec, members, parent = group_details(group_id, all_vars, backup_vars)
        groups.append(
            {
                "group_id": group_id,
                "pool": parent.split("/")[0],
                "parent_dataset": parent,
                "members": members,
                "required_config": ["/etc/data-plane/recovery-groups.json"],
                "destination_tiers": list(spec["destination_tiers"]),
                "destinations": group_destinations(spec),
                "cadence": spec["cadence"],
                "databases": list(spec.get("databases", [])),
                "notes": spec.get("notes", ""),
            }
        )
    return {"schema_version": 1, "groups": groups}


def build_host_plan(
    all_vars: dict[str, Any], backup_vars: dict[str, Any]
) -> dict[str, Any]:
    """The same plan as the host oneshot reads from /etc/homelab/backup.json."""

    order = validate(all_vars, backup_vars)
    paths = backup_vars["backup_dataset_paths"]
    groups = []
    for group_id in order:
        spec, members, _ = group_details(group_id, all_vars, backup_vars)
        datasets = [member["dataset"] for member in members]
        groups.append(
            {
                "id": group_id,
                "datasets": datasets,
                "destinations": group_destinations(spec),
                "dataset_paths": {dataset: paths[dataset] for dataset in datasets},
                "fence": [list(argv) for argv in spec.get("fence", [])],
                "checkpoint": [
                    list(argv) for argv in spec.get("checkpoint", [])
                ],
                "resume": [list(argv) for argv in spec.get("resume", [])],
                "databases": list(spec.get("databases", [])),
                "cadence": spec["cadence"],
            }
        )
    return {
        "schema_version": 1,
        "manifest_dir": backup_vars["backup_manifest_dir"],
        "groups": groups,
        "repositories": [
            {
                key: repository[key]
                for key in ("name", "repository", "password_file", "host", "tag")
            }
            for repository in backup_vars["backup_repositories"]
        ],
    }


def rendered_text() -> str:
    all_vars, backup_vars = load_group_vars()
    payload = build_recovery_groups(all_vars, backup_vars)
    return json.dumps(payload, indent=2) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail if the checked-in file differs instead of writing it",
    )
    args = parser.parse_args(argv)
    try:
        text = rendered_text()
    except (OSError, YamlSubsetError) as exc:
        print(f"render-recovery-groups: {exc}", file=sys.stderr)
        return 1
    if args.check:
        try:
            current = TARGET.read_text(encoding="utf-8")
        except OSError as exc:
            print(f"render-recovery-groups: {exc}", file=sys.stderr)
            return 1
        if current != text:
            print(
                f"{TARGET.relative_to(ROOT)} is stale; run "
                "scripts/render-recovery-groups.py",
                file=sys.stderr,
            )
            return 1
        return 0
    TARGET.write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
