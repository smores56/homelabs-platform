#!/usr/bin/env python3
"""Platform-owned cluster API capability for the agent.

The agent (nanobot) runs without a service-account token and has no API route
(docs/tenancy.md). This process is the only thing that talks to the Kubernetes
API on its behalf, and it answers ten fixed questions — the agent's own state,
its logs, its events, a pod-template restart, a dispatch of the trusted image
workflow, and a read-only view of pods, events, nodes, workload metadata and
Flux Kustomizations. There is no path parameter, no proxy and no generic
forwarding: a route is a constant in ROUTES, and the API paths below are built
from constants or from names the API itself returned.

The identity is deliberately narrow: a Role in `tenant-apps` for its own pods'
logs/events and one `patch` on `deployments/nanobot`, plus a read-only
ClusterRole for the cluster picture. Secrets and ConfigMaps are refused twice —
absent from the RBAC and rejected by `api_path_allowed` before a request is
built. The one outbound call outside the cluster is a GitHub Actions dispatch
to a constant repository/workflow path, with a token read from a mounted file
and never logged.

Standard library only: the image is a stock python slim, there is no build step,
and the ConfigMap is generated from this file by kustomize.
"""

from __future__ import annotations

import json
import os
import re
import ssl
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, quote, urlsplit

SA_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"
AGENT_NAMESPACE = "tenant-apps"
AGENT_DEPLOYMENT = "nanobot"
AGENT_LABEL = "app.kubernetes.io/name"
AGENT_LABEL_VALUE = "nanobot"
CALLER_NAMESPACE = "broker"
DEFAULT_PORT = 8081
DEFAULT_TAIL = 100
MAX_TAIL = 500
MAX_EVENTS = 120
RESTART_ANNOTATION = "homelabs.dev/restart-requested-at"

# The one outbound GitHub call. Repository, workflow and ref are constants: a
# request cannot name another workflow, another ref or another endpoint.
GITHUB_API_BASE = "https://api.github.com"
GITHUB_API_VERSION = "2022-11-28"
BUILD_REPO = "smores56/homelabs-platform"
BUILD_WORKFLOW = "publish-nanobot.yml"
BUILD_REF = "main"
BUILD_TOKEN_FILE = "/run/secrets/build-token"
BUILD_DISPATCH_PATH = (
    f"/repos/{BUILD_REPO}/actions/workflows/{BUILD_WORKFLOW}/dispatches"
)

# The response may only carry these labels: everything else on a pod is either
# noise or an operator's private bookkeeping.
POD_LABELS = ("app.kubernetes.io/name", "homelabs.dev/managed-by")

# The whole HTTP surface. A path absent here is a 404; a path present with a
# different method is a 405. The probe is a TCP check on the port on purpose, so
# no health route can become a second, token-free endpoint.
ROUTES: dict[str, frozenset[str]] = {
    "/self/status": frozenset({"GET"}),
    "/self/logs": frozenset({"GET"}),
    "/self/events": frozenset({"GET"}),
    "/self/restart": frozenset({"POST"}),
    "/self/build": frozenset({"POST"}),
    "/cluster/pods": frozenset({"GET"}),
    "/cluster/events": frozenset({"GET"}),
    "/cluster/nodes": frozenset({"GET"}),
    "/cluster/workloads": frozenset({"GET"}),
    "/cluster/flux": frozenset({"GET"}),
}

# Route -> (Control method, success status). The restart is 202: the API server
# accepted the patch, the new pod is not there yet. The build is 202 for the same
# reason: GitHub accepted the dispatch, the run has not started.
HANDLERS: dict[str, tuple[str, int]] = {
    "/self/status": ("self_status", 200),
    "/self/logs": ("self_logs", 200),
    "/self/events": ("self_events", 200),
    "/self/restart": ("self_restart", 202),
    "/self/build": ("self_build", 202),
    "/cluster/pods": ("cluster_pods", 200),
    "/cluster/events": ("cluster_events", 200),
    "/cluster/nodes": ("cluster_nodes", 200),
    "/cluster/workloads": ("cluster_workloads", 200),
    "/cluster/flux": ("cluster_flux", 200),
}

# Second line of defence behind RBAC: the client refuses to build a request for
# these even if a future handler asked for one.
FORBIDDEN_RESOURCES = frozenset({"secrets", "configmaps"})

WORKLOAD_RESOURCES = (
    ("Deployment", "/apis/apps/v1/deployments"),
    ("StatefulSet", "/apis/apps/v1/statefulsets"),
    ("DaemonSet", "/apis/apps/v1/daemonsets"),
)

_FIELD_UNSAFE = re.compile(r"[^A-Za-z0-9._~:/@+,=-]")
_DIGITS = re.compile(r"[0-9]{1,8}")
_SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9]([-A-Za-z0-9.]*[A-Za-z0-9])?$")


class ApiError(Exception):
    """An upstream API failure; the handler answers 502 and audits the status."""

    def __init__(self, message: str, upstream_status: int | None = None) -> None:
        super().__init__(message)
        self.upstream_status = upstream_status


class BuildUnavailable(Exception):
    """No dispatch token on disk: the handler answers 503, nothing is sent."""


class ForbiddenPath(Exception):
    """A request path the client must never build. A bug, not a user error."""


@dataclass(frozen=True)
class Route:
    status: int
    path: str
    query: dict[str, str]


def resolve_route(method: str, target: str) -> Route:
    """Map a request line onto the allowlist.

    200 means routable, 404 an unknown path, 405 a known path with the wrong
    method. The query is parsed but only `tail` survives, and the route itself
    is the exact path: no normalisation, so `..` and `//` stay unmatched.
    """
    split = urlsplit(target)
    path = split.path
    methods = ROUTES.get(path)
    if methods is None:
        return Route(404, path, {})
    if method.upper() not in methods:
        return Route(405, path, {})
    query = parse_qs(split.query, keep_blank_values=True)
    tail = query.get("tail")
    return Route(200, path, {"tail": tail[0]} if tail else {})


def clamp_tail(raw: str | None) -> int:
    """Bound the log tail so one request cannot ask for an unbounded read."""
    text = "" if raw is None else str(raw).strip()
    if not _DIGITS.fullmatch(text):
        return DEFAULT_TAIL
    return max(1, min(MAX_TAIL, int(text)))


def audit_line(
    method: str,
    path: str,
    status: int,
    caller: str,
    duration_ms: int,
    at: datetime | None = None,
) -> str:
    """One line per request, and only these five fields.

    Nothing here can carry a header, a token or a body: the caller is resolved
    from the source address, and the query string never reaches the log.
    """
    stamp = (at or datetime.now(timezone.utc)).isoformat(timespec="seconds")

    def field(value: Any) -> str:
        return _FIELD_UNSAFE.sub("_", str(value))

    return (
        f"{stamp} level=audit method={field(method)} path={field(path)} "
        f"status={int(status)} caller={field(caller)} duration_ms={int(duration_ms)}"
    )


def api_path_allowed(path: str) -> bool:
    """Refuse Secrets/ConfigMaps and any path that tries to climb out."""
    lowered = path.lower()
    if ".." in lowered or "\\" in lowered:
        return False
    segments = [part for part in lowered.split("?", 1)[0].split("/") if part]
    return not any(part in FORBIDDEN_RESOURCES for part in segments)


def replica_set_uids_for(replica_sets: list[dict], deployment_uid: str) -> set[str]:
    """UIDs of the ReplicaSets the nanobot Deployment owns.

    Pods are owned by a ReplicaSet, never by the Deployment, so the chain has to
    be walked one link. Matching on UID (not name) means a deleted and recreated
    Deployment does not inherit its predecessor's pods.
    """
    uids: set[str] = set()
    for replica_set in replica_sets:
        uid = (replica_set.get("metadata") or {}).get("uid")
        if not uid:
            continue
        for owner in (replica_set.get("metadata") or {}).get("ownerReferences") or []:
            if owner.get("kind") == "Deployment" and owner.get("uid") == deployment_uid:
                uids.add(uid)
    return uids


def select_self_pods(pods: list[dict], owner_uids: set[str]) -> list[dict]:
    """Pods the agent may read: labelled nanobot and owned by one of its sets.

    Both conditions are required. The label alone would match a copy-pasted pod
    spec; ownership alone would match a pod that is not an agent at all.
    """
    selected: list[dict] = []
    for pod in pods:
        metadata = pod.get("metadata") or {}
        labels = metadata.get("labels") or {}
        if labels.get(AGENT_LABEL) != AGENT_LABEL_VALUE:
            continue
        for owner in metadata.get("ownerReferences") or []:
            if owner.get("kind") == "ReplicaSet" and owner.get("uid") in owner_uids:
                selected.append(pod)
                break
    return selected


def select_self_events(events: list[dict], names: set[str]) -> list[dict]:
    """Events about the nanobot pods or the Deployment itself."""
    return [
        event
        for event in events
        if (event.get("involvedObject") or {}).get("name") in names
    ]


def caller_from_pods(pods: list[dict], address: str) -> str:
    """Resolve the caller's pod by its address, else keep the address.

    Only broker pods can reach the service (NetworkPolicy), so the lookup is
    scoped there; a miss degrades the audit field, never the request.
    """
    for pod in pods:
        metadata = pod.get("metadata") or {}
        if (pod.get("status") or {}).get("podIP") == address:
            return f"{metadata.get('namespace', CALLER_NAMESPACE)}/{metadata.get('name', '')}"
    return address


def _conditions(obj: dict) -> list[dict]:
    return (obj.get("status") or {}).get("conditions") or []


def _condition_true(obj: dict, kind: str) -> bool:
    for condition in _conditions(obj):
        if condition.get("type") == kind:
            return condition.get("status") == "True"
    return False


def curate_pod(pod: dict) -> dict:
    """Status only: a pod spec carries env, secret refs and volumes."""
    metadata = pod.get("metadata") or {}
    status = pod.get("status") or {}
    statuses = status.get("containerStatuses") or []
    labels = metadata.get("labels") or {}
    return {
        "name": metadata.get("name"),
        "namespace": metadata.get("namespace"),
        "phase": status.get("phase", "Unknown"),
        "ready": sum(1 for item in statuses if item.get("ready")),
        "restarts": sum(int(item.get("restartCount", 0)) for item in statuses),
        "images": sorted({str(item.get("image")) for item in statuses if item.get("image")}),
        "node": (pod.get("spec") or {}).get("nodeName"),
        "startedAt": status.get("startTime"),
        "labels": {key: labels[key] for key in POD_LABELS if key in labels},
    }


def curate_deployment(deployment: dict) -> dict:
    """Replica counts and conditions, never the pod template."""
    metadata = deployment.get("metadata") or {}
    status = deployment.get("status") or {}
    spec = deployment.get("spec") or {}
    replicas = spec.get("replicas")
    return {
        "name": metadata.get("name"),
        "namespace": metadata.get("namespace"),
        "desired": 1 if replicas is None else int(replicas),
        "ready": int(status.get("readyReplicas") or 0),
        "updated": int(status.get("updatedReplicas") or 0),
        "available": int(status.get("availableReplicas") or 0),
        "conditions": [
            {
                "type": condition.get("type"),
                "status": condition.get("status"),
                "reason": condition.get("reason"),
            }
            for condition in _conditions(deployment)
        ],
    }


def curate_event(event: dict) -> dict:
    involved = event.get("involvedObject") or {}
    metadata = event.get("metadata") or {}
    return {
        "namespace": metadata.get("namespace"),
        "name": metadata.get("name"),
        "type": event.get("type"),
        "reason": event.get("reason"),
        "message": event.get("message"),
        "count": event.get("count"),
        "firstSeen": event.get("firstTimestamp"),
        "lastSeen": event.get("lastTimestamp") or event.get("eventTime"),
        "involved": {
            "kind": involved.get("kind"),
            "namespace": involved.get("namespace"),
            "name": involved.get("name"),
        },
    }


def _event_stamp(event: dict) -> str:
    return str(
        event.get("lastTimestamp")
        or event.get("eventTime")
        or (event.get("metadata") or {}).get("creationTimestamp")
        or ""
    )


def cap_events(events: list[dict]) -> list[dict]:
    """Newest first, bounded, so one noisy namespace cannot fill the response."""
    return sorted(events, key=_event_stamp, reverse=True)[:MAX_EVENTS]


def curate_node(node: dict) -> dict:
    metadata = node.get("metadata") or {}
    status = node.get("status") or {}
    capacity = status.get("capacity") or {}
    return {
        "name": metadata.get("name"),
        "ready": _condition_true(node, "Ready"),
        "unschedulable": bool((node.get("spec") or {}).get("unschedulable")),
        "version": (status.get("nodeInfo") or {}).get("kubeletVersion"),
        "capacity": {key: capacity[key] for key in ("cpu", "memory") if key in capacity},
    }


def curate_workload(workload: dict) -> dict:
    """Metadata only: no spec, no status, no template."""
    metadata = workload.get("metadata") or {}
    labels = metadata.get("labels") or {}
    return {
        "kind": workload.get("kind"),
        "namespace": metadata.get("namespace"),
        "name": metadata.get("name"),
        "createdAt": metadata.get("creationTimestamp"),
        "app": labels.get(AGENT_LABEL),
    }


def curate_kustomization(kustomization: dict) -> dict:
    """Readiness only; a postBuild substitution map can hold a credential."""
    metadata = kustomization.get("metadata") or {}
    spec = kustomization.get("spec") or {}
    status = kustomization.get("status") or {}
    message = None
    for condition in _conditions(kustomization):
        if condition.get("type") == "Ready":
            message = condition.get("message")
    return {
        "name": metadata.get("name"),
        "namespace": metadata.get("namespace"),
        "ready": _condition_true(kustomization, "Ready"),
        "suspended": bool(spec.get("suspended")),
        "interval": spec.get("interval"),
        "revision": status.get("lastAppliedRevision"),
        "message": message,
    }


def _safe_segment(value: str) -> str:
    """Only DNS-safe names go into an API path; anything else is a bug."""
    if not isinstance(value, str) or not _SAFE_SEGMENT.match(value):
        raise ForbiddenPath("unsafe path segment")
    return quote(value, safe="")


class KubernetesClient:
    """The service's only route to the API server, with the scope baked in."""

    def __init__(self, sa_dir: str = SA_DIR, timeout: float = 10.0) -> None:
        self.timeout = timeout
        with open(os.path.join(sa_dir, "token"), encoding="utf-8") as handle:
            self.token = handle.read().strip()
        with open(os.path.join(sa_dir, "ca.crt"), "rb") as handle:
            self.context = ssl.create_default_context(cafile=None)
            self.context.load_verify_locations(cadata=handle.read().decode("utf-8"))
        host = os.environ.get("KUBERNETES_SERVICE_HOST", "kubernetes.default.svc")
        port = os.environ.get("KUBERNETES_SERVICE_PORT", "443")
        self.base_url = f"https://{host}:{port}"

    def _request(
        self, method: str, path: str, body: bytes | None = None, content_type: str = "application/json"
    ) -> bytes:
        if not api_path_allowed(path):
            raise ForbiddenPath(path.split("?", 1)[0])
        headers = {"Authorization": f"Bearer {self.token}", "Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = content_type
        request = urllib.request.Request(
            self.base_url + path, data=body, method=method, headers=headers
        )
        try:
            with urllib.request.urlopen(
                request, context=self.context, timeout=self.timeout
            ) as response:
                return response.read()
        except (urllib.error.URLError, OSError, ValueError) as exc:
            # The exception type and the resource path, never the request: the
            # token rides in a header and must not reach a log line.
            raise ApiError(f"{method} {path.split('?', 1)[0]}: {type(exc).__name__}") from exc

    def get(self, path: str) -> dict:
        return json.loads(self._request("GET", path).decode("utf-8"))

    def get_text(self, path: str) -> str:
        return self._request("GET", path).decode("utf-8", errors="replace")

    def patch(self, path: str, body: dict) -> dict:
        raw = self._request(
            "PATCH", path, json.dumps(body).encode("utf-8"),
            content_type="application/merge-patch+json",
        )
        return json.loads(raw.decode("utf-8"))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse a redirect instead of following it to a second path."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


class GitHubClient:
    """The service's only outbound call: dispatch the trusted image workflow.

    The path, body and headers are constants; the token comes from a file so a
    Secret can be mounted and rotated without the value ever reaching argv, the
    environment or a log line. The file is read per dispatch, and a missing or
    empty file raises BuildUnavailable before any request is built.
    """

    def __init__(self, token_file: str | None = None, base_url: str | None = None,
                 timeout: float = 10.0) -> None:
        self.token_file = token_file or os.environ.get("BUILD_TOKEN_FILE", BUILD_TOKEN_FILE)
        self.base_url = (
            base_url or os.environ.get("GITHUB_API_BASE", GITHUB_API_BASE)
        ).rstrip("/")
        self.timeout = timeout
        self.opener = urllib.request.build_opener(_NoRedirect)

    def _token(self) -> str:
        try:
            with open(self.token_file, encoding="utf-8") as handle:
                token = handle.read().strip()
        except OSError as exc:
            raise BuildUnavailable(type(exc).__name__) from exc
        if not token:
            raise BuildUnavailable("empty token file")
        return token

    def dispatch(self) -> int:
        """POST {"ref": "main"} and return GitHub's answer (204 on success)."""
        headers = {
            "Authorization": f"Bearer {self._token()}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": GITHUB_API_VERSION,
            "Content-Type": "application/json",
        }
        request = urllib.request.Request(
            self.base_url + BUILD_DISPATCH_PATH,
            data=json.dumps({"ref": BUILD_REF}).encode("utf-8"),
            method="POST",
            headers=headers,
        )
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                return int(response.status)
        except urllib.error.HTTPError as exc:
            # Status only: the path is a constant and the response is not logged.
            raise ApiError("dispatch rejected", upstream_status=exc.code) from exc
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise ApiError(f"dispatch failed: {type(exc).__name__}") from exc


class Control:
    """The ten answers, each built from constants and API-returned names."""

    def __init__(self, client: Any, namespace: str = AGENT_NAMESPACE,
                 deployment: str = AGENT_DEPLOYMENT, build: Any = None) -> None:
        self.client = client
        self.namespace = namespace
        self.deployment_name = deployment
        self.build = build if build is not None else GitHubClient()

    # -- paths are built here, never from a request parameter ----------------
    def _deployment_path(self) -> str:
        return (
            f"/apis/apps/v1/namespaces/{_safe_segment(self.namespace)}"
            f"/deployments/{_safe_segment(self.deployment_name)}"
        )

    def _replica_sets_path(self) -> str:
        return f"/apis/apps/v1/namespaces/{_safe_segment(self.namespace)}/replicasets"

    def _pods_path(self) -> str:
        return f"/api/v1/namespaces/{_safe_segment(self.namespace)}/pods"

    def _events_path(self) -> str:
        return f"/api/v1/namespaces/{_safe_segment(self.namespace)}/events"

    def _log_path(self, pod: str, container: str, tail: int) -> str:
        return (
            f"/api/v1/namespaces/{_safe_segment(self.namespace)}/pods/"
            f"{_safe_segment(pod)}/log?tail={int(tail)}&container={_safe_segment(container)}"
        )

    # -- self ---------------------------------------------------------------
    def _deployment(self) -> dict:
        return self.client.get(self._deployment_path())

    def self_pods(self) -> list[dict]:
        uid = (self._deployment().get("metadata") or {}).get("uid")
        if not uid:
            raise ApiError("deployment has no uid")
        owner_uids = replica_set_uids_for(
            self.client.get(self._replica_sets_path()).get("items", []), uid
        )
        if not owner_uids:
            return []
        return select_self_pods(self.client.get(self._pods_path()).get("items", []), owner_uids)

    def self_status(self) -> dict:
        return {
            "deployment": curate_deployment(self._deployment()),
            "pods": [curate_pod(pod) for pod in self.self_pods()],
        }

    def self_logs(self, tail: int = DEFAULT_TAIL) -> dict:
        tail = max(1, min(MAX_TAIL, int(tail)))
        pods = []
        for pod in self.self_pods():
            name = (pod.get("metadata") or {}).get("name")
            if not name:
                continue
            statuses = {
                item.get("name"): item
                for item in (pod.get("status") or {}).get("containerStatuses") or []
            }
            containers = []
            for container in (pod.get("spec") or {}).get("containers") or []:
                container_name = container.get("name")
                if not container_name or container_name not in statuses:
                    continue
                state = statuses[container_name].get("state") or {}
                if "running" not in state and "terminated" not in state:
                    # A container that never started has no log stream; asking
                    # the API would answer 400 and fail the whole request.
                    continue
                text = self.client.get_text(self._log_path(name, container_name, tail))
                containers.append(
                    {"name": container_name, "lines": text.splitlines()[-tail:]}
                )
            pods.append({"pod": name, "containers": containers})
        return {"tail": tail, "pods": pods}

    def self_events(self) -> dict:
        names = {pod.get("metadata", {}).get("name") for pod in self.self_pods()}
        names.add(self.deployment_name)
        events = self.client.get(self._events_path()).get("items", [])
        return {"events": [curate_event(event) for event in select_self_events(events, names)]}

    def self_restart(self) -> dict:
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.client.patch(
            self._deployment_path(),
            {"spec": {"template": {"metadata": {"annotations": {RESTART_ANNOTATION: stamp}}}}},
        )
        return {
            "deployment": f"{self.namespace}/{self.deployment_name}",
            "annotation": RESTART_ANNOTATION,
            "value": stamp,
        }

    # -- build --------------------------------------------------------------
    def self_build(self) -> dict:
        """Dispatch the trusted image workflow; the token never leaves the client."""
        upstream = self.build.dispatch()
        return {
            "repository": BUILD_REPO,
            "workflow": BUILD_WORKFLOW,
            "ref": BUILD_REF,
            "upstreamStatus": upstream,
        }

    # -- cluster (read-only) -------------------------------------------------
    def cluster_pods(self) -> dict:
        items = self.client.get("/api/v1/pods").get("items", [])
        return {"pods": [curate_pod(pod) for pod in items]}

    def cluster_events(self) -> dict:
        items = self.client.get("/api/v1/events").get("items", [])
        return {"events": [curate_event(event) for event in cap_events(items)]}

    def cluster_nodes(self) -> dict:
        items = self.client.get("/api/v1/nodes").get("items", [])
        return {"nodes": [curate_node(node) for node in items]}

    def cluster_workloads(self) -> dict:
        workloads = []
        for kind, path in WORKLOAD_RESOURCES:
            for item in self.client.get(path).get("items", []):
                workloads.append(curate_workload(dict(item, kind=kind)))
        return {"workloads": workloads}

    def cluster_flux(self) -> dict:
        items = self.client.get(
            "/apis/kustomize.toolkit.fluxcd.io/v1/kustomizations"
        ).get("items", [])
        return {"kustomizations": [curate_kustomization(item) for item in items]}


class CallerIndex:
    """Address -> broker pod, cached so the audit lookup is not per request."""

    def __init__(self, client: Any, namespace: str = CALLER_NAMESPACE,
                 ttl: float = 10.0) -> None:
        self.client = client
        self.namespace = namespace
        self.ttl = ttl
        self.lock = threading.Lock()
        self.fetched_at = 0.0
        self.pods: list[dict] = []

    def resolve(self, address: str) -> str:
        with self.lock:
            if time.monotonic() - self.fetched_at >= self.ttl:
                self.fetched_at = time.monotonic()
                try:
                    self.pods = self.client.get(
                        f"/api/v1/namespaces/{_safe_segment(self.namespace)}/pods"
                    ).get("items", [])
                except Exception:  # noqa: BLE001 - the audit degrades, the request does not
                    self.pods = []
            pods = self.pods
        return caller_from_pods(pods, address)


class Handler(BaseHTTPRequestHandler):
    control: Control
    callers: CallerIndex
    protocol_version = "HTTP/1.1"
    server_version = "agent-control"
    sys_version = ""

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._dispatch("POST")

    def do_PUT(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._dispatch("PUT")

    def do_PATCH(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._dispatch("PATCH")

    def do_DELETE(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._dispatch("DELETE")

    def do_HEAD(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._dispatch("HEAD")

    def do_OPTIONS(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._dispatch("OPTIONS")

    def _dispatch(self, method: str) -> None:
        started = time.monotonic()
        route = resolve_route(method, self.path)
        status = 500
        try:
            if route.status != 200:
                status = route.status
                payload = {"error": "not found" if status == 404 else "method not allowed"}
            else:
                name, success = HANDLERS[route.path]
                if route.path == "/self/logs":
                    result = getattr(self.control, name)(clamp_tail(route.query.get("tail")))
                else:
                    result = getattr(self.control, name)()
                status = success
                payload = dict(result)
        except BuildUnavailable:
            status = 503
            payload = {"error": "build_unavailable"}
        except ApiError as exc:
            status = 502
            payload = {"error": "upstream", "detail": str(exc)}
            if exc.upstream_status is not None:
                payload["upstreamStatus"] = exc.upstream_status
        except ForbiddenPath:
            # Only reachable through a bug: the client refused its own path.
            status = 500
            payload = {"error": "refused"}
        except Exception:  # noqa: BLE001 - answer, audit, and keep serving
            status = 500
            payload = {"error": "internal"}
        try:
            payload["generatedAt"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            self._respond(status, payload)
        finally:
            # After the response, so a slow or unreachable API server delays the
            # audit line, never the caller.
            caller = self.callers.resolve(self.client_address[0])
            elapsed = int((time.monotonic() - started) * 1000)
            print(audit_line(method, route.path, status, caller, elapsed), flush=True)

    def _respond(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, indent=1, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: Any) -> None:
        # Silence BaseHTTPRequestHandler's stderr line: the audit line in
        # _dispatch is the only request log.
        pass


def main() -> None:
    port = int(os.environ.get("PORT", str(DEFAULT_PORT)))
    namespace = os.environ.get("AGENT_NAMESPACE", AGENT_NAMESPACE)
    deployment = os.environ.get("AGENT_DEPLOYMENT", AGENT_DEPLOYMENT)
    client = KubernetesClient()
    Handler.control = Control(client, namespace, deployment)
    Handler.callers = CallerIndex(client)
    print(
        f"level=info service=agent-control port={port} "
        f"namespace={namespace} deployment={deployment}",
        flush=True,
    )
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
