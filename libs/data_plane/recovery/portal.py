"""Static, generation-aware recovery portal.

The portal is a human view over the same manifests restore reads. It never
walks a destination, never calls Restic, and never trusts a mutable "latest"
pointer: for each recovery group it asks
``data_plane.backup.generations.select_generation`` for the newest complete,
compatible generation and renders only that generation's own data. Incomplete,
incompatible, future-dated, and under-destination generations are reported as
warnings, never promoted to the selected row.

Rendering is a single self-contained HTML document with inline CSS, escaped
values, and no script, form, image, link, or remote resource. The generator
also writes ``index.json`` beside the page for scripts and the read-only HTTP
service. Both files are staged in the target directory, fsynced, and renamed
into place, so a reader never sees a half-written page.
"""

from __future__ import annotations

import html
import json
import math
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from ..backup.generations import (
    GenerationManifest,
    SnapshotRef,
    manifest_hash,
    select_generation,
)
from ..errors import DataPlaneError, ManifestError, RestoreError, ValidationError

# A daily backup cadence makes a month without a new restorable generation worth
# surfacing. Callers that pass ``now`` can override this through ``stale_after``.
STALE_AFTER_SECONDS = 30 * 24 * 60 * 60


class PortalError(DataPlaneError):
    """The portal could not be rendered or written."""


@dataclass(frozen=True)
class PortalEntry:
    """One recovery group's restorable row, or an explicit unavailable row."""

    group_id: str
    generation_id: str
    created_at: float
    complete: bool
    manifest_hash: str
    destinations: tuple[SnapshotRef, ...] = ()
    datasets: tuple[tuple[str, str], ...] = ()
    sqlite: tuple[tuple[str, int, int], ...] = ()
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "destinations", tuple(self.destinations))
        object.__setattr__(self, "datasets", tuple(tuple(item) for item in self.datasets))
        object.__setattr__(self, "sqlite", tuple(tuple(item) for item in self.sqlite))
        object.__setattr__(self, "warnings", tuple(self.warnings))

    @property
    def restorable(self) -> bool:
        """True only for a generation the generator actually selected."""

        return self.complete and bool(self.generation_id.strip())

    def to_dict(self) -> dict[str, Any]:
        return {
            "group_id": self.group_id,
            "generation_id": self.generation_id,
            "created_at": self.created_at,
            "complete": self.complete,
            "manifest_hash": self.manifest_hash,
            "destinations": [item.to_dict() for item in self.destinations],
            "datasets": [[name, snapshot] for name, snapshot in self.datasets],
            "sqlite": [[name, epoch, position] for name, epoch, position in self.sqlite],
            "warnings": list(self.warnings),
        }


def _requirements(
    required_destinations: Iterable[str],
    required_datasets: Iterable[str],
    schema_version: int,
) -> tuple[tuple[str, ...], tuple[str, ...], int]:
    destinations = tuple(required_destinations)
    datasets = tuple(required_datasets)
    for label, values in (
        ("required destination", destinations),
        ("required dataset", datasets),
    ):
        if any(not isinstance(value, str) or not value.strip() for value in values):
            raise ValidationError(f"{label}s must be non-empty strings")
        if len(values) != len(set(values)):
            raise ValidationError(f"{label}s contain duplicates")
    if (
        not isinstance(schema_version, int)
        or isinstance(schema_version, bool)
        or schema_version < 1
    ):
        raise ValidationError("portal schema_version must be a positive integer")
    return destinations, datasets, schema_version


def _order(manifest: GenerationManifest) -> tuple[float, str]:
    return (manifest.created_at, manifest.generation_id)


def _ignored_reason(
    manifest: GenerationManifest,
    required_destinations: tuple[str, ...],
    required_datasets: tuple[str, ...],
    schema_version: int,
    now: float | None,
) -> str:
    if now is not None and manifest.created_at > now:
        return "created in the future"
    try:
        manifest.validate(required_destinations, required_datasets, schema_version)
    except ManifestError as exc:
        return str(exc)
    return "not selected"


def _unavailable(
    group_id: str,
    pool: list[GenerationManifest],
    required_destinations: tuple[str, ...],
    required_datasets: tuple[str, ...],
    schema_version: int,
    now: float | None,
) -> PortalEntry:
    warnings = [f"no complete compatible generation exists for group {group_id!r}"]
    for manifest in sorted(pool, key=_order, reverse=True):
        reason = _ignored_reason(
            manifest,
            required_destinations,
            required_datasets,
            schema_version,
            now,
        )
        warnings.append(
            f"generation {manifest.generation_id!r} was ignored: {reason}"
        )
    # Deliberately no generation id, hash, or dataset data: a partial
    # generation is never presented as something restore could select.
    return PortalEntry(
        group_id=group_id,
        generation_id="",
        created_at=0.0,
        complete=False,
        manifest_hash="",
        warnings=tuple(warnings),
    )


def _entry_for_group(
    group_id: str,
    pool: list[GenerationManifest],
    required_destinations: tuple[str, ...],
    required_datasets: tuple[str, ...],
    schema_version: int,
    now: float | None,
    stale_after: float | None,
) -> PortalEntry:
    try:
        chosen = select_generation(
            pool,
            required_destinations,
            required_datasets,
            schema_version,
            now=now,
        )
    except RestoreError:
        return _unavailable(
            group_id,
            pool,
            required_destinations,
            required_datasets,
            schema_version,
            now,
        )

    warnings: list[str] = []
    chosen_order = _order(chosen)
    newer = [
        manifest
        for manifest in pool
        if manifest is not chosen and _order(manifest) > chosen_order
    ]
    for manifest in sorted(newer, key=_order):
        reason = _ignored_reason(
            manifest,
            required_destinations,
            required_datasets,
            schema_version,
            now,
        )
        warnings.append(
            f"newer generation {manifest.generation_id!r} is not usable: {reason}"
        )
    if newer:
        warnings.append(
            f"generation {chosen.generation_id!r} is stale: newer generations "
            "exist but none is complete and compatible"
        )
    if (
        now is not None
        and stale_after is not None
        and now - chosen.created_at > stale_after
    ):
        warnings.append(
            f"generation {chosen.generation_id!r} is stale: it is older than "
            f"{stale_after / 86400:g} days"
        )
    missing_destinations = sorted(
        set(required_destinations) - {item.destination for item in chosen.destinations}
    )
    if missing_destinations:
        warnings.append(
            f"generation {chosen.generation_id!r} is missing destinations: "
            + ", ".join(missing_destinations)
        )
    missing_datasets = sorted(
        set(required_datasets) - {name for name, _ in chosen.datasets}
    )
    if missing_datasets:
        warnings.append(
            f"generation {chosen.generation_id!r} is missing datasets: "
            + ", ".join(missing_datasets)
        )

    return PortalEntry(
        group_id=group_id,
        generation_id=chosen.generation_id,
        created_at=chosen.created_at,
        complete=True,
        manifest_hash=manifest_hash(chosen),
        destinations=chosen.destinations,
        datasets=chosen.datasets,
        sqlite=chosen.sqlite,
        warnings=tuple(warnings),
    )


def build_entries(
    manifests: Iterable[GenerationManifest],
    required_destinations: Iterable[str],
    required_datasets: Iterable[str],
    schema_version: int,
    now: float | None = None,
    *,
    stale_after: float | None = STALE_AFTER_SECONDS,
) -> tuple[PortalEntry, ...]:
    """One entry per recovery group, newest complete compatible generation.

    Selection delegates to the shared restore selector, so the portal and a
    restore plan agree on what is trustworthy. A group with no eligible
    generation still gets one entry, marked incomplete and carrying warnings,
    so an operator sees the gap instead of a silently missing group.
    ``now`` enables future-dated filtering and the age warning; without it the
    portal stays deterministic for tests and reproducible renders.
    """

    destinations, datasets, schema = _requirements(
        required_destinations, required_datasets, schema_version
    )
    if stale_after is not None and (
        not isinstance(stale_after, (int, float))
        or isinstance(stale_after, bool)
        or not math.isfinite(stale_after)
        or stale_after < 0
    ):
        raise ValidationError("portal stale_after must be a non-negative number")

    grouped: dict[str, list[GenerationManifest]] = {}
    for manifest in manifests:
        if not isinstance(manifest, GenerationManifest):
            raise ValidationError(
                "portal manifests must be GenerationManifest instances, "
                f"got {type(manifest).__name__}"
            )
        grouped.setdefault(manifest.group_id, []).append(manifest)

    entries = [
        _entry_for_group(
            group_id,
            grouped[group_id],
            destinations,
            datasets,
            schema,
            now,
            stale_after,
        )
        for group_id in sorted(grouped)
    ]
    return tuple(entries)


def _escape(value: object) -> str:
    return html.escape(str(value), quote=True)


def _timestamp(value: float) -> str | None:
    if not math.isfinite(value) or value <= 0:
        return None
    try:
        moment = datetime.fromtimestamp(value, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


_HEAD = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
<meta name="referrer" content="no-referrer">
<title>Recovery portal</title>
<style>
:root { color-scheme: light dark; }
body { font-family: ui-sans-serif, system-ui, sans-serif; line-height: 1.5; margin: 0 auto; max-width: 60rem; padding: 1.5rem; }
header { border-bottom: 1px solid currentColor; margin-bottom: 1.5rem; }
h1 { font-size: 1.6rem; margin-bottom: 0.25rem; }
h2 { font-size: 1.3rem; margin-bottom: 0.25rem; }
h3 { font-size: 1rem; margin-bottom: 0.25rem; }
.entry { border: 1px solid currentColor; border-radius: 0.4rem; margin-bottom: 1.25rem; padding: 1rem; }
.status { font-weight: 600; }
.complete .status { color: #1a7f37; }
.incomplete .status { color: #b42318; }
dl { display: grid; grid-template-columns: max-content 1fr; gap: 0.15rem 1rem; }
dt { font-weight: 600; }
dd { margin: 0; }
code, .hash { font-family: ui-monospace, monospace; overflow-wrap: anywhere; }
ul { margin: 0.25rem 0 0.75rem; padding-left: 1.25rem; }
.warning { color: #b42318; }
.empty, .ok { font-style: italic; }
footer { border-top: 1px solid currentColor; margin-top: 1.5rem; padding-top: 0.75rem; font-size: 0.85rem; }
</style>
</head>
<body>
<header>
<h1>Recovery portal</h1>
<p>Newest complete, compatible generation per recovery group. Partial generations are never shown as restorable.</p>
</header>
<main>"""

_FOOT = """</main>
<footer>Static page: no scripts, no forms, and no external resources.</footer>
</body>
</html>"""


def _render_list(items: list[str], empty: str) -> list[str]:
    if not items:
        return [f'<p class="ok">{_escape(empty)}</p>']
    return ["<ul>"] + [f"<li>{item}</li>" for item in items] + ["</ul>"]


def _render_entry(entry: PortalEntry) -> str:
    restorable = entry.restorable
    state = "complete" if restorable else "incomplete"
    generation = entry.generation_id or "none"
    if restorable:
        status = f"Generation {generation} is complete and compatible."
    else:
        status = "No complete compatible generation is available."

    created = _timestamp(entry.created_at) if restorable else None
    if created is None:
        created_cell = "not available"
    else:
        created_cell = f'<time datetime="{_escape(created)}">{_escape(created)}</time>'

    destination_items = [
        (
            f"<code>{_escape(ref.destination)}</code> &mdash; snapshot "
            f"<code>{_escape(ref.snapshot_id)}</code> "
            f"({_escape('verified' if ref.verified else 'unverified')})"
        )
        for ref in entry.destinations
    ]
    dataset_items = [
        f"<code>{_escape(name)}</code> &mdash; snapshot <code>{_escape(snapshot)}</code>"
        for name, snapshot in entry.datasets
    ]
    sqlite_items = [
        (
            f"<code>{_escape(name)}</code> &mdash; epoch {_escape(epoch)}, "
            f"position {_escape(position)}"
        )
        for name, epoch, position in entry.sqlite
    ]
    warning_items = [f'<span class="warning">{_escape(text)}</span>' for text in entry.warnings]

    parts = [
        f'<article class="entry {state}">',
        f"<h2>{_escape(entry.group_id)}</h2>",
        f'<p class="status">{_escape(status)}</p>',
        "<dl>",
        f"<dt>Generation</dt><dd><code>{_escape(generation)}</code></dd>",
        f"<dt>Created</dt><dd>{created_cell}</dd>",
        f"<dt>Manifest hash</dt><dd><code>{_escape(entry.manifest_hash or 'none')}</code></dd>",
        "</dl>",
        "<h3>Destination copies</h3>",
        *_render_list(destination_items, "No destination copies recorded."),
        "<h3>Datasets</h3>",
        *_render_list(dataset_items, "No datasets recorded."),
        "<h3>SQLite checkpoints</h3>",
        *_render_list(sqlite_items, "No SQLite checkpoints recorded."),
        "<h3>Warnings</h3>",
        *_render_list(warning_items, "No warnings."),
        "</article>",
    ]
    return "\n".join(parts)


def render_html(entries: Iterable[PortalEntry]) -> str:
    """Render a static page. Every value is escaped; nothing is executable."""

    ordered = sorted(
        entries, key=lambda entry: (entry.group_id, entry.created_at, entry.generation_id)
    )
    parts = [_HEAD]
    if not ordered:
        parts.append(
            '<p class="empty">No generations have been published yet.</p>'
        )
    for entry in ordered:
        parts.append(_render_entry(entry))
    parts.append(_FOOT)
    return "\n".join(parts) + "\n"


def _stage(directory: Path, name: str, data: bytes) -> str:
    handle, temporary = tempfile.mkstemp(
        prefix=f".{name}.", suffix=".tmp", dir=directory
    )
    try:
        with os.fdopen(handle, "wb") as file:
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
    except OSError:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    return temporary


def write_portal(
    directory: str | os.PathLike[str],
    manifests: Iterable[GenerationManifest],
    required_destinations: Iterable[str],
    required_datasets: Iterable[str],
    schema_version: int,
) -> tuple[Path, Path]:
    """Atomically write ``index.html`` and ``index.json`` into ``directory``.

    Each file is staged, fsynced, and renamed, so a reader sees either the old
    pair or a complete new file. Returns ``(index.html, index.json)``.
    """

    entries = build_entries(
        manifests,
        required_destinations,
        required_datasets,
        schema_version,
    )
    page = render_html(entries).encode("utf-8")
    payload = {
        "schema_version": schema_version,
        "entries": [entry.to_dict() for entry in entries],
    }
    index = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8") + b"\n"

    root = Path(directory)
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise PortalError(f"cannot create portal directory {root}: {exc}") from exc

    html_path = root / "index.html"
    json_path = root / "index.json"
    staged: list[tuple[str, Path]] = []
    temporary_files: list[str] = []
    try:
        for name, data in (("index.json", index), ("index.html", page)):
            temporary = _stage(root, name, data)
            temporary_files.append(temporary)
            staged.append((temporary, root / name))
        for temporary, target in staged:
            os.replace(temporary, target)
            temporary_files.remove(temporary)
    except OSError as exc:
        raise PortalError(f"cannot write portal in {root}: {exc}") from exc
    finally:
        for temporary in temporary_files:
            try:
                os.unlink(temporary)
            except OSError:
                pass
    try:
        directory_handle = os.open(root, os.O_RDONLY)
    except OSError:
        pass
    else:
        try:
            os.fsync(directory_handle)
        finally:
            os.close(directory_handle)
    return html_path, json_path
