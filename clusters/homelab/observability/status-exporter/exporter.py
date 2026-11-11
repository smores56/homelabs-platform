#!/usr/bin/env python3
"""Human-owned status exporter for the `data` namespace.

Agents (nanobot) never receive `get pods` in a data namespace, so this process
is the sanctioned, read-only view of what runs there: readiness, restart counts
and image versions, rendered as Prometheus metrics. It holds one narrowly scoped
identity: the workload ClusterRole grants `get/list/watch pods` and `get/list`
on deployments/statefulsets and is bound by one RoleBinding per data namespace,
while the cluster-scoped `nodes` read lives in a separate ClusterRole bound by a
single ClusterRoleBinding. Binding the workload role cluster-wide would hand out
`pods` everywhere, which is why the roles are split.

Standard library only: the image is a stock python slim, there is no build step,
and the ConfigMap is generated from this file by kustomize.
"""

from __future__ import annotations

import json
import os
import ssl
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Iterable

SA_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"
DEFAULT_NAMESPACE = "data"
DEFAULT_PORT = 9101
DEFAULT_REFRESH_SECONDS = 30.0


def escape_label(value: Any) -> str:
    """Escape a Prometheus label value (backslash, quote, newline)."""
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def render_sample(name: str, labels: dict[str, str], value: Any) -> str:
    """Render one Prometheus exposition line; labels are sorted for stability."""
    if labels:
        body = ",".join(f'{k}="{escape_label(labels[k])}"' for k in sorted(labels))
        return f"{name}{{{body}}} {value}"
    return f"{name} {value}"


def pod_ready(pod: dict[str, Any]) -> float:
    """1.0 when every container reports ready, else 0.0."""
    statuses = pod.get("status", {}).get("containerStatuses") or []
    if not statuses:
        return 0.0
    return 1.0 if all(bool(s.get("ready")) for s in statuses) else 0.0


def pod_restarts(pod: dict[str, Any]) -> int:
    """Sum of restartCount across containers, the number an operator watches."""
    statuses = pod.get("status", {}).get("containerStatuses") or []
    return sum(int(s.get("restartCount", 0)) for s in statuses)


def _desired_replicas(workload: dict[str, Any]) -> int:
    replicas = workload.get("spec", {}).get("replicas")
    return 1 if replicas is None else int(replicas)


def workload_ready(workload: dict[str, Any]) -> float:
    """1.0 when the workload reports all desired replicas ready.

    A workload scaled to zero is ready: it is not broken, it is off.
    """
    desired = _desired_replicas(workload)
    if desired == 0:
        return 1.0
    status = workload.get("status", {})
    ready = status.get("readyReplicas")
    if ready is None:
        ready = status.get("availableReplicas", 0)
    return 1.0 if int(ready or 0) >= desired else 0.0


def workload_images(workload: dict[str, Any]) -> list[str]:
    """Sorted, de-duplicated images of the workload's app containers."""
    containers = workload.get("spec", {}).get("template", {}).get("spec", {}).get("containers") or []
    images = {c.get("image") for c in containers if c.get("image")}
    return sorted(images)


def node_ready(node: dict[str, Any]) -> float:
    """1.0 when the node's Ready condition is True, else 0.0.

    A node with no conditions is not ready: the API omits the list while the
    node is still registering, and the digest must not report it as healthy.
    """
    conditions = node.get("status", {}).get("conditions") or []
    for condition in conditions:
        if condition.get("type") == "Ready":
            return 1.0 if condition.get("status") == "True" else 0.0
    return 0.0


def node_metrics(nodes: Iterable[dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    for node in nodes:
        labels = {"node": node.get("metadata", {}).get("name", "")}
        lines.append(render_sample("homelabs_cluster_node_ready", labels, node_ready(node)))
    return lines


def pod_metrics(pods: Iterable[dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    for pod in pods:
        name = pod.get("metadata", {}).get("name", "")
        namespace = pod.get("metadata", {}).get("namespace", DEFAULT_NAMESPACE)
        labels = {"namespace": namespace, "pod": name}
        lines.append(render_sample("homelabs_data_pod_ready", labels, pod_ready(pod)))
        lines.append(render_sample("homelabs_data_pod_restart_count", labels, pod_restarts(pod)))
    return lines


def workload_metrics(workloads: Iterable[dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    for workload in workloads:
        metadata = workload.get("metadata", {})
        kind = workload.get("kind", "")
        namespace = metadata.get("namespace", DEFAULT_NAMESPACE)
        labels = {"namespace": namespace, "kind": kind, "name": metadata.get("name", "")}
        lines.append(render_sample("homelabs_data_workload_ready", labels, workload_ready(workload)))
        for image in workload_images(workload):
            lines.append(
                render_sample("homelabs_data_workload_image", {**labels, "image": image}, 1)
            )
    return lines


def render_success(success: bool, last_success_at: float | None = None) -> str:
    """Exporter self-metrics.

    `last_scrape_timestamp_seconds` is the wall clock of the last *successful*
    API read, so a consumer (the digest) can compute a real age instead of
    assuming the refresh interval. 0 means "never succeeded", which is not the
    same as "just refreshed".
    """
    stamp = int(last_success_at or 0)
    return "\n".join(
        [
            "# HELP homelabs_data_status_exporter_up 1 while the exporter serves metrics.",
            "# TYPE homelabs_data_status_exporter_up gauge",
            render_sample("homelabs_data_status_exporter_up", {}, 1 if success else 0),
            "# HELP homelabs_data_status_exporter_last_scrape_success "
            "1 when the last Kubernetes API read succeeded.",
            "# TYPE homelabs_data_status_exporter_last_scrape_success gauge",
            render_sample(
                "homelabs_data_status_exporter_last_scrape_success", {}, 1 if success else 0
            ),
            "# HELP homelabs_data_status_exporter_last_scrape_timestamp_seconds "
            "Unix time of the last successful Kubernetes API read (0 = never).",
            "# TYPE homelabs_data_status_exporter_last_scrape_timestamp_seconds gauge",
            render_sample(
                "homelabs_data_status_exporter_last_scrape_timestamp_seconds", {}, stamp
            ),
        ]
    )


def render(samples: list[str], success: bool, last_success_at: float | None = None) -> str:
    return "\n".join(samples + [render_success(success, last_success_at), ""])


class KubernetesClient:
    """Read-only client for the in-cluster API, scoped by its RoleBindings.

    The data tier is one namespace per app, so the client reads a list: the
    exporter holds one RoleBinding per namespace and cannot see any other.
    """

    def __init__(self, namespaces: list[str], sa_dir: str = SA_DIR) -> None:
        self.namespaces = namespaces
        self.token = open(os.path.join(sa_dir, "token"), encoding="utf-8").read().strip()
        with open(os.path.join(sa_dir, "ca.crt"), "rb") as handle:
            self.context = ssl.create_default_context(cafile=None)
            self.context.load_verify_locations(cadata=handle.read().decode("utf-8"))
        self.base = os.environ.get("KUBERNETES_SERVICE_HOST", "kubernetes.default.svc")
        port = os.environ.get("KUBERNETES_SERVICE_PORT", "443")
        self.base_url = f"https://{self.base}:{port}"

    def get(self, path: str) -> dict[str, Any]:
        request = urllib.request.Request(
            self.base_url + path,
            headers={"Authorization": f"Bearer {self.token}", "Accept": "application/json"},
        )
        with urllib.request.urlopen(request, context=self.context, timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))

    def collect(self) -> list[str]:
        # One cluster-scoped read (the nodes ClusterRole grants `nodes` get/list)
        # so the digest can report node readiness without an agent ever holding
        # this identity.
        samples: list[str] = node_metrics(self.get("/api/v1/nodes").get("items", []))
        for namespace in self.namespaces:
            pods = self.get(f"/api/v1/namespaces/{namespace}/pods").get("items", [])
            deployments = self.get(f"/apis/apps/v1/namespaces/{namespace}/deployments").get(
                "items", []
            )
            statefulsets = self.get(f"/apis/apps/v1/namespaces/{namespace}/statefulsets").get(
                "items", []
            )
            samples += pod_metrics(pods) + workload_metrics(deployments + statefulsets)
        return samples


class Cache:
    """Holds the last successful scrape for `refresh_seconds`."""

    def __init__(self, client: KubernetesClient, refresh_seconds: float) -> None:
        self.client = client
        self.refresh_seconds = refresh_seconds
        self.lock = threading.Lock()
        self.fetched_at = 0.0
        self.last_success_at = 0.0
        self.samples: list[str] = []
        self.success = False

    def metrics(self) -> str:
        with self.lock:
            if time.monotonic() - self.fetched_at >= self.refresh_seconds:
                self.fetched_at = time.monotonic()
                try:
                    self.samples = self.client.collect()
                    self.success = True
                    # Wall clock, not monotonic: consumers compare against UTC.
                    self.last_success_at = time.time()
                except Exception:  # noqa: BLE001 - any API/transport error is a failed scrape
                    self.success = False
            return render(self.samples, self.success, self.last_success_at)


class Handler(BaseHTTPRequestHandler):
    cache: Cache

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if self.path == "/healthz":
            self._respond(200, "text/plain; charset=utf-8", "ok\n")
        elif self.path == "/metrics":
            self._respond(200, "text/plain; version=0.0.4; charset=utf-8", self.cache.metrics())
        else:
            self._respond(404, "text/plain; charset=utf-8", "not found\n")

    def _respond(self, status: int, content_type: str, body: str) -> None:
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, fmt: str, *args: Any) -> None:
        pass


def main() -> None:
    raw = os.environ.get("DATA_NAMESPACES", os.environ.get("DATA_NAMESPACE", DEFAULT_NAMESPACE))
    namespaces = [part.strip() for part in raw.split(",") if part.strip()]
    port = int(os.environ.get("EXPORT_PORT", DEFAULT_PORT))
    refresh = float(os.environ.get("REFRESH_SECONDS", DEFAULT_REFRESH_SECONDS))
    Handler.cache = Cache(KubernetesClient(namespaces), refresh)
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    server.serve_forever()


if __name__ == "__main__":
    main()
