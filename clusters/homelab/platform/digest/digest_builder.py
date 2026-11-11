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
    exporter_refresh_seconds: int = 30,
    max_events: int = MAX_EVENTS,
    max_files: int = MAX_FILES,
    max_bytes: int = MAX_BYTES,
    window: str = WINDOW,
) -> dict[str, Any]:
    events, seen = activity_from_loki(loki_payload, limit=max_events)
    freshness = {
        "status_exporter": {
            "ok": exporter_ok,
            # The exporter serves a cached scrape; its own refresh bound is the
            # honest upper bound on age.
            "age_seconds": exporter_refresh_seconds if exporter_ok else None,
        },
        "loki_gateway": {"ok": loki_ok, "age_seconds": 0 if loki_ok else None},
    }
    return assemble(
        generated_at=generated_at,
        cluster=cluster_health(parse_prometheus(exporter_text)),
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
    print(json.dumps(summary, sort_keys=True))
    return 0 if (exporter_ok and loki_ok) else 1


if __name__ == "__main__":
    sys.exit(main())
