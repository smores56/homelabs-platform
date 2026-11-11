#!/usr/bin/env python3
"""Build the cluster digest the host writer ships into Kanbei's inbox.

One direction only: this process reads the human-owned status exporter and the
Loki `data` tenant through the gateway, never the Kubernetes API, and writes a
single bounded JSON document to the mounted digest share. Nothing it emits is
controlled by an agent: the activity section is rebuilt from an allowlist, so a
message body, a prompt or a tool payload cannot survive even when it is planted
in a data-tenant line.

Determinism is part of the contract. Keys are sorted, event order is a total
order over the allowlisted fields, and no run-dependent value other than
`generated_at` reaches the file. Two runs over the same inputs differ only in
that field, which lets the host writer use `generated_at` as its cursor.

Standard library only: the image is a stock python slim and the ConfigMap is
generated from this file by kustomize.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Iterable

SCHEMA_VERSION = "1"
MAX_BYTES = 64 * 1024
MAX_FILES = 96
MAX_EVENTS = 400
WINDOW = "15m"
FIELD_CHARS = 64

# Staleness bounds. The exporter refreshes its cache every 30 s, so three
# refreshes without a new successful read means it is serving a stale snapshot
# or has never succeeded; that is a fault and fails the run. The data tenant is
# written by Vector from container logs, which are bursty: "nothing in the
# digest window" is worth flagging (an empty activity section must not read as a
# quiet host), but a quiet homelab at night is not a broken pipeline, so that
# flag is an alert in the document and on stderr, not a failed run.
EXPORTER_STALE_SECONDS = 90

DEFAULT_OUTPUT = "/mnt/data/cluster-digest"
DEFAULT_EXPORTER_URL = "http://status-exporter.observability.svc.cluster.local:9101/metrics"
DEFAULT_GATEWAY_URL = "http://loki-gateway.observability.svc.cluster.local:3100"
DEFAULT_TOKEN_FILE = "/var/run/secrets/loki-gateway/token"

# The only fields that may leave the `data` tenant. `logical_host` is the
# stream's `namespace` label: the digest names which host emitted the line, not
# the pod, container or path, and never the line itself.
EVENT_FIELDS = ("level", "component", "error_code", "status")
ACTIVITY_ALLOWLIST = ("logical_host", "timestamp") + EVENT_FIELDS

LATEST = "latest.json"
SNAPSHOT_PREFIX = "digest-"
SNAPSHOT_FORMAT = "%Y%m%dT%H%M%SZ"

_SAMPLE = re.compile(
    r"^(?P<name>[A-Za-z_:][A-Za-z0-9_:]*)"
    r"(?:\{(?P<labels>[^}]*)\})?"
    r"\s+(?P<value>[^ \t]+)\s*$"
)
_LABEL = re.compile(r'([A-Za-z_][A-Za-z0-9_]*)\s*=\s*"((?:[^"\\]|\\.)*)"')
_UNSAFE = re.compile(r"[^\x20-\x7e]")


class DigestTooLarge(RuntimeError):
    """The mandatory sections alone exceed the byte budget."""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _label_value(raw: str) -> str:
    return raw.replace('\\\\', '\\').replace('\\"', '"').replace('\\n', '\n')


def parse_prometheus(text: str) -> list[dict[str, Any]]:
    """Parse the status exporter's exposition text into typed samples.

    Comments and malformed lines are skipped: a scrape that half-parses should
    shrink the digest, not abort it, and the freshness map records the failure.
    """
    samples: list[dict[str, Any]] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = _SAMPLE.match(stripped)
        if not match:
            continue
        try:
            value = float(match.group("value"))
        except ValueError:
            continue
        labels = {
            key: _label_value(raw)
            for key, raw in _LABEL.findall(match.group("labels") or "")
        }
        samples.append({"name": match.group("name"), "labels": labels, "value": value})
    return samples


def _int_value(value: float) -> int:
    return int(value)


def _by_name(samples: Iterable[dict[str, Any]], name: str) -> list[dict[str, Any]]:
    return [sample for sample in samples if sample["name"] == name]


def cluster_health(samples: list[dict[str, Any]]) -> dict[str, Any]:
    """Node readiness plus data workload ready/restarts/versions.

    Restarts are summed per workload by the pod-name prefix the exporter already
    uses; a workload whose pods the exporter could not see reports 0, which is
    why the exporter's own scrape-success sample is part of the freshness map.
    """
    nodes = sorted(
        (
            {"name": sample["labels"].get("node", ""), "ready": sample["value"] >= 1}
            for sample in _by_name(samples, "homelabs_cluster_node_ready")
        ),
        key=lambda node: node["name"],
    )

    pods = sorted(
        (
            {
                "namespace": sample["labels"].get("namespace", ""),
                "pod": sample["labels"].get("pod", ""),
                "ready": False,
                "restarts": _int_value(sample["value"]),
            }
            for sample in _by_name(samples, "homelabs_data_pod_restart_count")
        ),
        key=lambda pod: (pod["namespace"], pod["pod"]),
    )
    ready_by_pod = {
        (sample["labels"].get("namespace", ""), sample["labels"].get("pod", "")): sample["value"] >= 1
        for sample in _by_name(samples, "homelabs_data_pod_ready")
    }
    for pod in pods:
        pod["ready"] = ready_by_pod.get((pod["namespace"], pod["pod"]), pod["ready"])

    workloads: dict[tuple[str, str, str], dict[str, Any]] = {}
    for sample in _by_name(samples, "homelabs_data_workload_ready"):
        labels = sample["labels"]
        key = (labels.get("namespace", ""), labels.get("kind", ""), labels.get("name", ""))
        workloads[key] = {
            "namespace": key[0],
            "kind": key[1],
            "name": key[2],
            "ready": sample["value"] >= 1,
            "restarts": 0,
            "images": [],
        }
    for sample in _by_name(samples, "homelabs_data_workload_image"):
        labels = sample["labels"]
        key = (labels.get("namespace", ""), labels.get("kind", ""), labels.get("name", ""))
        workload = workloads.setdefault(
            key,
            {
                "namespace": key[0],
                "kind": key[1],
                "name": key[2],
                "ready": False,
                "restarts": 0,
                "images": [],
            },
        )
        image = labels.get("image")
        if image and image not in workload["images"]:
            workload["images"].append(image)

    for workload in workloads.values():
        prefix = f"{workload['name']}-"
        workload["restarts"] = sum(
            pod["restarts"]
            for pod in pods
            if pod["namespace"] == workload["namespace"] and pod["pod"].startswith(prefix)
        )
        workload["images"].sort()

    return {
        "nodes": nodes,
        "pods": pods,
        "workloads": sorted(
            workloads.values(), key=lambda w: (w["namespace"], w["kind"], w["name"])
        ),
    }


def clean_field(value: Any) -> str:
    """Normalise one allowlisted value: printable ASCII, bounded length.

    A data-tenant writer controls the value of its own `level`/`component`
    field, so the value is treated as untrusted text: control characters are
    dropped and the length is capped. This is what keeps a 64 KiB digest a bound
    even when every allowlisted field is stuffed.
    """
    text = _UNSAFE.sub("", str(value)).strip()
    return text[:FIELD_CHARS]


def _line_timestamp(line: dict[str, Any], fallback_ns: str) -> str:
    raw = line.get("timestamp")
    if isinstance(raw, str) and raw:
        return clean_field(raw)
    try:
        seconds = int(fallback_ns) / 1e9
    except (TypeError, ValueError):
        return ""
    return datetime.fromtimestamp(seconds, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _entry_time(value: Any) -> datetime | None:
    """Timestamp of one Loki stream entry: the entry header wins.

    Loki's nanosecond header is authoritative and always present; the line's own
    `timestamp` field is a fallback for entries without one (a non-JSON line).
    """
    header = None
    if isinstance(value, (list, tuple)) and len(value) == 2:
        try:
            header = int(value[0])
        except (TypeError, ValueError):
            header = None
        if header is None:
            try:
                parsed = json.loads(value[1])
            except (TypeError, ValueError):
                parsed = None
            if isinstance(parsed, dict):
                raw = parsed.get("timestamp")
                if isinstance(raw, str) and raw:
                    try:
                        return datetime.strptime(raw, "%Y-%m-%dT%H:%M:%SZ").replace(
                            tzinfo=timezone.utc
                        )
                    except ValueError:
                        return None
    if header is None:
        return None
    return datetime.fromtimestamp(header / 1e9, tz=timezone.utc)


def newest_event_time(payload: dict[str, Any]) -> datetime | None:
    """Newest entry timestamp in the returned streams, or None when empty."""
    newest: datetime | None = None
    for stream in (payload.get("data") or {}).get("result") or []:
        for value in stream.get("values") or []:
            moment = _entry_time(value)
            if moment is not None and (newest is None or moment > newest):
                newest = moment
    return newest


def _sample_value(samples: Iterable[dict[str, Any]], name: str) -> float | None:
    matches = _by_name(samples, name)
    return matches[-1]["value"] if matches else None


def freshness_report(
    *,
    samples: list[dict[str, Any]],
    loki_payload: dict[str, Any],
    generated_at: datetime,
    exporter_ok: bool,
    loki_ok: bool,
    exporter_stale_seconds: int = EXPORTER_STALE_SECONDS,
    loki_stale_seconds: int | None = None,
    window: str = WINDOW,
) -> dict[str, Any]:
    """Measured freshness, never assumed.

    `status_exporter`: `age_seconds` is `generated_at` minus the exporter's own
    `last_scrape_timestamp_seconds` sample, and `ok` additionally requires
    `last_scrape_success == 1`. A reachable exporter whose last good read is
    older than `exporter_stale_seconds` is stale *and* not ok: it is serving a
    snapshot, so the digest would report an old cluster as current.

    `loki_gateway`: `age_seconds` is `generated_at` minus the newest stored
    data-tenant entry. `ok` is the query succeeding; `stale` says the query
    window held nothing (or the newest entry is older than the bound, which is
    the window by default), which is the signal that the write path
    (Vector -> Loki) is broken, and is an alert rather than a failed run.
    """
    loki_bound = loki_stale_seconds if loki_stale_seconds is not None else _window_seconds(window)
    success = _sample_value(samples, "homelabs_data_status_exporter_last_scrape_success")
    stamped = _sample_value(samples, "homelabs_data_status_exporter_last_scrape_timestamp_seconds")
    exporter_age: int | None = None
    if exporter_ok and stamped is not None and stamped > 0:
        exporter_age = max(0, int(generated_at.timestamp() - stamped))
    exporter_up = bool(exporter_ok and success == 1)
    exporter_stale = exporter_age is None or exporter_age > exporter_stale_seconds

    newest = newest_event_time(loki_payload)
    loki_age: int | None = None
    if newest is not None:
        loki_age = max(0, int((generated_at - newest).total_seconds()))

    return {
        "status_exporter": {
            "ok": exporter_up and not exporter_stale,
            "age_seconds": exporter_age,
            "stale": exporter_stale,
        },
        "loki_gateway": {
            "ok": bool(loki_ok),
            "age_seconds": loki_age,
            "stale": newest is None or loki_age is None or loki_age > loki_bound,
        },
    }


def digest_ok(freshness: dict[str, Any]) -> bool:
    """A run is successful only when every source is `ok` (stale counts for the
    exporter, whose `ok` already folds it in)."""
    return all(entry["ok"] for entry in freshness.values())


def stale_sources(freshness: dict[str, Any], bounds: dict[str, int]) -> list[str]:
    """One human-readable line per stale source, for the job log."""
    messages: list[str] = []
    for name, entry in sorted(freshness.items()):
        if not entry["stale"]:
            continue
        age = entry["age_seconds"]
        bound = bounds.get(name, 0)
        seen = "no timestamp" if age is None else f"{age}s old"
        messages.append(f"{name}: newest observation is {seen} (bound {bound}s)")
    return messages


def activity_from_loki(payload: dict[str, Any], limit: int = MAX_EVENTS) -> tuple[list[dict[str, str]], int]:
    """Rebuild activity events from an allowlist, never from the raw line.

    Returns the sorted events and how many stream entries were seen before the
    cap. A line that is not a JSON object contributes nothing: it cannot be
    classified, and dropping it is the fail-closed choice.
    """
    streams = (payload.get("data") or {}).get("result") or []
    events: list[dict[str, str]] = []
    seen = 0
    for stream in streams:
        labels = stream.get("stream") or {}
        # Only the host label is read; pod, container, path and stream labels are
        # ignored on purpose.
        logical_host = clean_field(labels.get("namespace", "unknown")) or "unknown"
        for value in stream.get("values") or []:
            if not isinstance(value, (list, tuple)) or len(value) != 2:
                continue
            seen += 1
            try:
                parsed = json.loads(value[1])
            except (TypeError, ValueError):
                continue
            if not isinstance(parsed, dict):
                continue
            event: dict[str, str] = {
                "logical_host": logical_host,
                "timestamp": _line_timestamp(parsed, value[0]),
            }
            for field in EVENT_FIELDS:
                field_value = clean_field(parsed.get(field, ""))
                if field_value:
                    event[field] = field_value
            events.append(event)
    events.sort(key=lambda event: tuple(event.get(field, "") for field in ACTIVITY_ALLOWLIST))
    return events[:limit], seen


def assemble(
    *,
    generated_at: datetime,
    cluster: dict[str, Any],
    events: list[dict[str, str]],
    observed_events: int,
    freshness: dict[str, Any],
    max_events: int = MAX_EVENTS,
    max_files: int = MAX_FILES,
    max_bytes: int = MAX_BYTES,
    window: str = WINDOW,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": iso(generated_at),
        "window": window,
        "source_freshness": freshness,
        "cluster": cluster,
        "nanobot_activity": {
            "observed_events": observed_events,
            "event_count": len(events),
            "truncated": observed_events > len(events),
            "events": events,
        },
        "limits": {"max_bytes": max_bytes, "max_events": max_events, "max_files": max_files},
    }


def encode(document: dict[str, Any]) -> bytes:
    """Canonical JSON: sorted keys, compact separators, ASCII only, one newline."""
    return (json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n").encode(
        "utf-8"
    )


def fit(document: dict[str, Any], max_bytes: int = MAX_BYTES) -> bytes:
    """Enforce the byte budget by dropping the oldest events, deterministically.

    The mandatory sections are small; if they alone exceed the budget the job
    fails rather than inventing a second truncation rule.
    """
    events = document["nanobot_activity"]["events"]
    payload = encode(document)
    while len(payload) > max_bytes and events:
        events.pop(0)
        document["nanobot_activity"]["event_count"] = len(events)
        document["nanobot_activity"]["truncated"] = True
        payload = encode(document)
    if len(payload) > max_bytes:
        raise DigestTooLarge(f"digest is {len(payload)} bytes with no events (limit {max_bytes})")
    return payload


def _write_file(path: str, payload: bytes) -> None:
    """Write through a temp file in the same directory, then rename atomically.

    The host writer watches `latest.json`; a reader must never observe a partial
    document, so the visible name only appears after fsync + rename.
    """
    handle, temporary = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".tmp-")
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        if os.path.exists(temporary):
            os.unlink(temporary)
        raise


def prune(directory: str, keep: int = MAX_FILES) -> list[str]:
    """Keep the newest `keep` snapshots; return the names that were removed."""
    snapshots = sorted(
        (
            name
            for name in os.listdir(directory)
            if name.startswith(SNAPSHOT_PREFIX) and name.endswith(".json")
        ),
        reverse=True,
    )
    removed: list[str] = []
    for name in snapshots[keep:]:
        os.unlink(os.path.join(directory, name))
        removed.append(name)
    return removed


def publish(
    directory: str,
    document: dict[str, Any],
    *,
    keep: int = MAX_FILES,
    max_bytes: int = MAX_BYTES,
) -> dict[str, Any]:
    """Write the snapshot and `latest.json`, then prune. Returns a run summary."""
    payload = fit(document, max_bytes)
    os.makedirs(directory, exist_ok=True)
    snapshot = SNAPSHOT_PREFIX + document["generated_at"].replace("-", "").replace(":", "") + ".json"
    _write_file(os.path.join(directory, snapshot), payload)
    _write_file(os.path.join(directory, LATEST), payload)
    return {
        "generated_at": document["generated_at"],
        "bytes": len(payload),
        "events": document["nanobot_activity"]["event_count"],
        "latest": os.path.join(directory, LATEST),
        "snapshot": os.path.join(directory, snapshot),
        "pruned": prune(directory, keep),
    }


def fetch(url: str, token: str = "", timeout: float = 10.0) -> bytes:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def loki_query_url(gateway_url: str, window: str, limit: int, now: datetime) -> str:
    end_ns = int(now.timestamp() * 1e9)
    seconds = _window_seconds(window)
    start_ns = end_ns - seconds * 1_000_000_000
    query = urllib.parse.urlencode(
        {
            "query": '{tier="data"}',
            "start": str(start_ns),
            "end": str(end_ns),
            "limit": str(limit),
            "direction": "backward",
        }
    )
    return f"{gateway_url.rstrip('/')}/loki/api/v1/query_range?{query}"


def _window_seconds(window: str) -> int:
    match = re.fullmatch(r"(\d+)([smh])", window)
    if not match:
        raise ValueError(f"unsupported window: {window}")
    amount = int(match.group(1))
    return amount * {"s": 1, "m": 60, "h": 3600}[match.group(2)]


def build(
    *,
    exporter_text: str,
    loki_payload: dict[str, Any],
    generated_at: datetime,
    exporter_ok: bool = True,
    loki_ok: bool = True,
    exporter_stale_seconds: int = EXPORTER_STALE_SECONDS,
    loki_stale_seconds: int | None = None,
    max_events: int = MAX_EVENTS,
    max_files: int = MAX_FILES,
    max_bytes: int = MAX_BYTES,
    window: str = WINDOW,
) -> dict[str, Any]:
    samples = parse_prometheus(exporter_text)
    events, seen = activity_from_loki(loki_payload, limit=max_events)
    freshness = freshness_report(
        samples=samples,
        loki_payload=loki_payload,
        generated_at=generated_at,
        exporter_ok=exporter_ok,
        loki_ok=loki_ok,
        exporter_stale_seconds=exporter_stale_seconds,
        loki_stale_seconds=loki_stale_seconds,
        window=window,
    )
    return assemble(
        generated_at=generated_at,
        cluster=cluster_health(samples),
        events=events,
        observed_events=seen,
        freshness=freshness,
        max_events=max_events,
        max_files=max_files,
        max_bytes=max_bytes,
        window=window,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build and publish the cluster digest.")
    parser.add_argument("--output", default=os.environ.get("DIGEST_DIR", DEFAULT_OUTPUT))
    parser.add_argument("--exporter-url", default=os.environ.get("STATUS_EXPORTER_URL", DEFAULT_EXPORTER_URL))
    parser.add_argument("--gateway-url", default=os.environ.get("LOKI_GATEWAY_URL", DEFAULT_GATEWAY_URL))
    parser.add_argument(
        "--token-file", default=os.environ.get("LOKI_GATEWAY_TOKEN_FILE", DEFAULT_TOKEN_FILE)
    )
    parser.add_argument("--window", default=os.environ.get("DIGEST_WINDOW", WINDOW))
    parser.add_argument("--max-bytes", type=int, default=int(os.environ.get("DIGEST_MAX_BYTES", MAX_BYTES)))
    parser.add_argument("--max-events", type=int, default=int(os.environ.get("DIGEST_MAX_EVENTS", MAX_EVENTS)))
    parser.add_argument("--max-files", type=int, default=int(os.environ.get("DIGEST_MAX_FILES", MAX_FILES)))
    args = parser.parse_args(argv)

    token = ""
    try:
        with open(args.token_file, encoding="utf-8") as handle:
            token = handle.read().strip()
    except OSError:
        token = ""
    if not token:
        # Fail closed: without the gateway token the activity section would be
        # silently empty, which reads exactly like a quiet host.
        print("digest: Loki gateway token is missing or empty", file=sys.stderr)
        return 2

    generated_at = utc_now()
    exporter_ok = True
    try:
        exporter_text = fetch(args.exporter_url).decode("utf-8")
    except OSError as error:
        print(f"digest: status exporter read failed: {error}", file=sys.stderr)
        exporter_text = ""
        exporter_ok = False

    loki_ok = True
    try:
        loki_payload = json.loads(
            fetch(loki_query_url(args.gateway_url, args.window, args.max_events * 2, generated_at), token)
        )
    except (OSError, ValueError) as error:
        print(f"digest: Loki query failed: {error}", file=sys.stderr)
        loki_payload = {"data": {"result": []}}
        loki_ok = False

    document = build(
        exporter_text=exporter_text,
        loki_payload=loki_payload,
        generated_at=generated_at,
        exporter_ok=exporter_ok,
        loki_ok=loki_ok,
        max_events=args.max_events,
        max_files=args.max_files,
        max_bytes=args.max_bytes,
        window=args.window,
    )
    try:
        summary = publish(args.output, document, keep=args.max_files, max_bytes=args.max_bytes)
    except DigestTooLarge as error:
        print(f"digest: {error}", file=sys.stderr)
        return 3

    freshness = document["source_freshness"]
    for message in stale_sources(
        freshness,
        {
            "status_exporter": EXPORTER_STALE_SECONDS,
            "loki_gateway": _window_seconds(args.window),
        },
    ):
        # An alert, not a crash: the document is already written and must stay
        # readable so the host writer and the reader see *why* it is stale.
        print(f"digest: stale source {message}", file=sys.stderr)
    print(json.dumps(summary, sort_keys=True))
    return 0 if digest_ok(freshness) else 1


if __name__ == "__main__":
    sys.exit(main())
