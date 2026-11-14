"""Offline importers for memory-shaped exports.

Both importers are pure file readers: no network, no provider SDK, no live
Graphiti connection. A Graphiti export is converted node by node; an
Obsidian-style markdown vault is converted heading by heading. Stable ids and
timestamps come from the input, never from the clock, so the same input imports
identically twice and the second import writes nothing. A cross-profile export
is refused rather than merged into the wrong profile.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from ..errors import ContractError, ValidationError
from .file_memory import MemoryRecord

GRAPHITI_SOURCE = "graphiti.export"
MARKDOWN_SOURCE = "notes.markdown"

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$")
_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")


def _target_profile(memory: Any) -> str:
    profile = getattr(memory, "profile", None)
    if profile is None:
        raise ContractError(
            "memory import needs one profile; construct FileMemory(root, profile)"
        )
    if not callable(getattr(memory, "import_records", None)):
        raise ContractError("memory target does not support import_records")
    return profile


def _read_json(path: str | Path) -> Mapping[str, Any]:
    location = Path(path)
    try:
        payload = json.loads(location.read_text(encoding="utf-8"))
    except OSError as error:
        raise ValidationError(f"export {location} is unreadable: {error}") from error
    except json.JSONDecodeError as error:
        raise ValidationError(f"export {location} is not JSON: {error}") from error
    if not isinstance(payload, Mapping):
        raise ValidationError(f"export {location} must be a JSON object")
    return payload


def _check_profile(value: object, target: str, label: str) -> None:
    if value is None:
        return
    if value != target:
        raise ContractError(
            f"{label} belongs to profile {value!r}; a {target!r} memory refuses "
            "cross-profile imports"
        )


def _stable_id(prefix: str, value: str) -> str:
    if _SAFE_ID.fullmatch(value):
        return f"{prefix}-{value}"
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]
    return f"{prefix}-{digest}"


def _provenance_index(
    raw: object,
) -> tuple[dict[str, Mapping[str, Any]], Mapping[str, Any]]:
    """Index per-node provenance and return export-wide provenance defaults."""

    if raw is None:
        return {}, {}
    if isinstance(raw, Mapping):
        fallback = raw.get("export")
        if not isinstance(fallback, Mapping):
            fallback = {}
        nodes = raw.get("nodes")
        if isinstance(nodes, Mapping):
            entries = nodes
        else:
            entries = {
                key: value
                for key, value in raw.items()
                if key not in {"export", "nodes", "edges"}
            }
        index: dict[str, Mapping[str, Any]] = {}
        for key, value in entries.items():
            if not isinstance(key, str) or not isinstance(value, Mapping):
                raise ValidationError("graphiti provenance entries must be mappings")
            index[key] = value
        return index, fallback
    if isinstance(raw, list):
        index = {}
        for entry in raw:
            if not isinstance(entry, Mapping):
                raise ValidationError("graphiti provenance entries must be mappings")
            key = entry.get("node_uuid") or entry.get("uuid") or entry.get("id")
            if not isinstance(key, str) or not key.strip():
                raise ValidationError("graphiti provenance entry has no node uuid")
            index[key] = entry
        return index, {}
    raise ValidationError("graphiti export provenance must be a mapping or list")


def _node_provenance(
    node: Mapping[str, Any],
    index: Mapping[str, Mapping[str, Any]],
    fallback: Mapping[str, Any],
    node_id: str,
) -> Mapping[str, Any]:
    merged: dict[str, Any] = {
        key: value for key, value in fallback.items() if key != "revision"
    }
    indexed = index.get(node_id)
    if indexed is not None:
        merged.update(indexed)
    local = node.get("provenance")
    if local is not None:
        if not isinstance(local, Mapping):
            raise ValidationError(f"graphiti node {node_id!r} provenance must be a mapping")
        merged.update(local)
    for key in ("source_id", "source", "revision", "captured_at"):
        if key in node:
            merged[key] = node[key]
    return merged


def _is_tombstone(node: Mapping[str, Any]) -> bool:
    for key in ("tombstone", "deleted", "expired"):
        if node.get(key):
            return True
    for key in ("invalid_at", "expired_at", "tombstoned_at"):
        if node.get(key):
            return True
    return False


def _graphiti_record(
    node: object,
    position: int,
    index: Mapping[str, Mapping[str, Any]],
    fallback: Mapping[str, Any],
    profile: str,
) -> tuple[MemoryRecord, str]:
    if not isinstance(node, Mapping):
        raise ValidationError(f"graphiti node {position} must be an object")
    node_id = node.get("uuid") or node.get("id")
    if not isinstance(node_id, str) or not node_id.strip():
        raise ValidationError(f"graphiti node {position} has no uuid")
    _check_profile(node.get("profile"), profile, f"graphiti node {node_id!r}")

    provenance = _node_provenance(node, index, fallback, node_id)
    source_id = provenance.get("source_id") or provenance.get("source")
    if not isinstance(source_id, str) or not source_id.strip():
        source_id = GRAPHITI_SOURCE
    revision = provenance.get("revision")
    if isinstance(revision, bool) or not isinstance(revision, (str, int)):
        raise ValidationError(f"graphiti node {node_id!r} has no revision")
    revision = str(revision)
    if not revision.strip():
        raise ValidationError(f"graphiti node {node_id!r} has no revision")
    captured_at = provenance.get("captured_at") or node.get("created_at")
    if not isinstance(captured_at, str) or not captured_at.strip():
        raise ValidationError(f"graphiti node {node_id!r} has no captured_at")

    content = node.get("content")
    if content is None:
        content = node.get("summary")
    if content is None:
        content = node.get("name")
    tombstone = _is_tombstone(node)
    if not isinstance(content, str) and not tombstone:
        raise ValidationError(f"graphiti node {node_id!r} has no text content")
    if not isinstance(content, str):
        content = ""

    labels = node.get("labels") or node.get("tags") or []
    if isinstance(labels, str):
        labels = [labels]
    if not isinstance(labels, (list, tuple)):
        raise ValidationError(f"graphiti node {node_id!r} labels must be a sequence")
    tags: list[str] = []
    for label in labels:
        if not isinstance(label, str) or not label.strip():
            raise ValidationError(f"graphiti node {node_id!r} has an invalid label")
        tags.append(label)
    tags.append("graphiti")

    tombstone = _is_tombstone(node)
    created_at = node.get("created_at") or captured_at
    if tombstone:
        updated_at = (
            node.get("invalid_at")
            or node.get("tombstoned_at")
            or node.get("expired_at")
            or node.get("updated_at")
            or captured_at
        )
    else:
        updated_at = node.get("updated_at") or captured_at
    for name, value in (("created_at", created_at), ("updated_at", updated_at)):
        if not isinstance(value, str) or not value.strip():
            raise ValidationError(f"graphiti node {node_id!r} has no {name}")

    record = MemoryRecord(
        id=_stable_id("graphiti", node_id),
        profile=profile,
        source=source_id,
        content="" if tombstone else content,
        tags=tuple(tags),
        provenance={
            "source_id": source_id,
            "revision": revision,
            "captured_at": captured_at,
        },
        created_at=created_at,
        updated_at=updated_at,
        deleted=tombstone,
    )
    return record, node_id


def _validate_edges(edges: object, node_ids: set[str]) -> None:
    if edges is None:
        return
    if not isinstance(edges, list):
        raise ValidationError("graphiti export edges must be a list")
    for position, edge in enumerate(edges):
        if not isinstance(edge, Mapping):
            raise ValidationError(f"graphiti edge {position} must be an object")
        for key in ("source_node_uuid", "target_node_uuid"):
            target = edge.get(key)
            if target is None:
                continue
            if not isinstance(target, str) or target not in node_ids:
                raise ValidationError(
                    f"graphiti edge {position} references unknown node {target!r}"
                )


def import_graphiti_export(path: str | Path, memory: Any) -> tuple[MemoryRecord, ...]:
    """Import one synthetic export into a single-profile memory."""

    profile = _target_profile(memory)
    payload = _read_json(path)
    _check_profile(payload.get("profile"), profile, "graphiti export")
    nodes = payload.get("nodes")
    if not isinstance(nodes, list):
        raise ValidationError("graphiti export nodes must be a list")
    if "provenance" not in payload:
        raise ValidationError("graphiti export has no provenance")
    index, fallback = _provenance_index(payload.get("provenance"))
    edges = payload.get("edges", [])

    records: list[MemoryRecord] = []
    node_ids: set[str] = set()
    seen: set[str] = set()
    for position, node in enumerate(nodes):
        record, node_id = _graphiti_record(node, position, index, fallback, profile)
        if record.id in seen:
            raise ValidationError(f"graphiti export has duplicate node {node_id!r}")
        seen.add(record.id)
        node_ids.add(node_id)
        records.append(record)

    _validate_edges(edges, node_ids)
    records.sort(key=lambda record: record.id)
    return memory.import_records(record.as_dict() for record in records)


def markdown_record_id(relative_path: str, heading: str) -> str:
    """A deterministic, flat id from the vault-relative path and heading."""

    digest = hashlib.sha256(
        f"{relative_path}\x00{heading}".encode("utf-8")
    ).hexdigest()[:24]
    slug = re.sub(r"[^a-z0-9]+", "-", heading.casefold()).strip("-")[:32]
    return f"md-{slug or 'document'}-{digest}"


def _split_frontmatter(text: str, source: Path) -> tuple[dict[str, Any], str]:
    text = text.lstrip("\ufeff")
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return {}, text
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            return _parse_frontmatter("".join(lines[1:index])), "".join(lines[index + 1 :])
    raise ValidationError(f"markdown {source} has unterminated frontmatter")


def _scalar(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _parse_frontmatter(raw: str) -> dict[str, Any]:
    values: dict[str, Any] = {}
    key: str | None = None
    list_mode = False
    for line in raw.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        item = re.match(r"^\s+-\s*(.*)$", line)
        if item is not None and key is not None and list_mode:
            values[key].append(_scalar(item.group(1)))
            continue
        pair = re.match(r"^([A-Za-z0-9_.-]+):(.*)$", line)
        if pair is None:
            continue
        key = pair.group(1)
        remainder = pair.group(2).strip()
        if remainder == "":
            values[key] = []
            list_mode = True
        elif remainder.startswith("[") and remainder.endswith("]"):
            values[key] = [
                _scalar(part) for part in remainder[1:-1].split(",") if part.strip()
            ]
            list_mode = False
        else:
            values[key] = _scalar(remainder)
            list_mode = False
    return values


def _frontmatter_timestamp(front: Mapping[str, Any], *names: str) -> str | None:
    for name in names:
        value = front.get(name)
        if isinstance(value, str) and value.strip():
            return value
    return None


def _frontmatter_tags(front: Mapping[str, Any]) -> list[str]:
    raw = front.get("tags", front.get("tag"))
    if raw is None:
        return []
    candidates = [part.strip() for part in raw.split(",")] if isinstance(raw, str) else raw
    if not isinstance(candidates, (list, tuple)):
        return []
    return [tag for tag in candidates if isinstance(tag, str) and tag.strip()]


def _mtime(path: Path) -> str:
    return datetime.fromtimestamp(
        path.stat().st_mtime, timezone.utc
    ).isoformat(timespec="seconds")


def _sections(text: str) -> list[tuple[str, str]]:
    sections: list[tuple[str, str]] = []
    heading = ""
    lines: list[str] = []
    fence: str | None = None

    def flush() -> None:
        content = "\n".join(lines).strip("\n")
        if heading or content.strip():
            sections.append((heading, content))

    for line in text.splitlines():
        stripped = line.lstrip()
        if fence is not None:
            lines.append(line)
            if stripped.startswith(fence):
                fence = None
            continue
        if stripped.startswith("```") or stripped.startswith("~~~"):
            fence = stripped[:3]
            lines.append(line)
            continue
        match = _HEADING.match(line)
        if match is not None:
            flush()
            heading = match.group(2).strip()
            lines = []
        else:
            lines.append(line)
    flush()
    return sections


def import_markdown_notes(
    path: str | Path, memory: Any
) -> tuple[MemoryRecord, ...]:
    """Import an Obsidian-style markdown file or vault, offline."""

    profile = _target_profile(memory)
    root = Path(path)
    if root.is_dir():
        files = sorted(
            item
            for item in root.rglob("*.md")
            if item.is_file() and not item.name.startswith(".")
        )
        base = root
    elif root.is_file():
        files = [root]
        base = root.parent
    else:
        raise ValidationError(f"markdown path {root} does not exist")

    records: list[MemoryRecord] = []
    seen: set[str] = set()
    for file in files:
        relative = file.relative_to(base).as_posix()
        try:
            data = file.read_bytes()
            text = data.decode("utf-8")
        except (OSError, UnicodeDecodeError) as error:
            raise ValidationError(f"markdown {file} is unreadable: {error}") from error
        front, body = _split_frontmatter(text, file)
        revision = hashlib.sha256(data).hexdigest()
        created_at = (
            _frontmatter_timestamp(front, "created", "date") or _mtime(file)
        )
        updated_at = (
            _frontmatter_timestamp(front, "updated", "modified") or created_at
        )
        tags = _frontmatter_tags(front)

        for heading, section in _sections(body):
            record_id = markdown_record_id(relative, heading)
            if record_id in seen:
                raise ValidationError(
                    f"markdown {relative} repeats heading {heading!r}"
                )
            seen.add(record_id)
            records.append(
                MemoryRecord(
                    id=record_id,
                    profile=profile,
                    source=MARKDOWN_SOURCE,
                    content=section.strip("\n") or heading,
                    tags=tuple(tags + ["obsidian"]),
                    provenance={
                        "source_id": MARKDOWN_SOURCE,
                        "revision": revision,
                        "captured_at": updated_at,
                    },
                    created_at=created_at,
                    updated_at=updated_at,
                )
            )

    records.sort(key=lambda record: record.id)
    return memory.import_records(record.as_dict() for record in records)
