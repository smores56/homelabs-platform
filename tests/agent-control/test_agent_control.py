"""Unit tests for the platform-owned agent-control service.

The service is deployed from source through a generated ConfigMap, so this
module is the only place its routing, filtering and audit contract is checked
without a cluster. The pure functions are exercised directly; the `Control`
class runs against a fake API client that fails the test on any request path the
service would not be allowed to make.
"""

from __future__ import annotations

import importlib.util
import inspect
import pathlib
import sys
import unittest
from datetime import datetime, timezone

SOURCE = (
    pathlib.Path(__file__).resolve().parents[2]
    / "clusters/homelab/agent-control/agent_control.py"
)
spec = importlib.util.spec_from_file_location("agent_control", SOURCE)
assert spec and spec.loader
agent_control = importlib.util.module_from_spec(spec)
sys.modules["agent_control"] = agent_control
spec.loader.exec_module(agent_control)

DEPLOYMENT_UID = "11111111-2222-3333-4444-555555555555"
REPLICA_SET_UID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
OLD_REPLICA_SET_UID = "bbbbbbbb-cccc-dddd-eeee-ffffffffffff"


def pods_for_test() -> list[dict]:
    return [
        {
            "metadata": {
                "name": "nanobot-current",
                "namespace": "tenant-apps",
                "labels": {"app.kubernetes.io/name": "nanobot"},
                "ownerReferences": [{"kind": "ReplicaSet", "uid": REPLICA_SET_UID}],
            },
            "spec": {"containers": [{"name": "gateway"}]},
            "status": {
                "podIP": "10.42.1.10",
                "phase": "Running",
                "containerStatuses": [
                    {"name": "gateway", "ready": True, "restartCount": 2, "state": {"running": {}}}
                ],
            },
        },
        {
            "metadata": {
                "name": "nanobot-old",
                "namespace": "tenant-apps",
                "labels": {"app.kubernetes.io/name": "nanobot"},
                "ownerReferences": [{"kind": "ReplicaSet", "uid": OLD_REPLICA_SET_UID}],
            },
            "spec": {"containers": [{"name": "gateway"}]},
            "status": {"phase": "Succeeded", "containerStatuses": []},
        },
        {
            "metadata": {
                "name": "lookalike",
                "namespace": "tenant-apps",
                "labels": {"app.kubernetes.io/name": "nanobot"},
                "ownerReferences": [{"kind": "ReplicaSet", "uid": "someone-else"}],
            },
            "spec": {"containers": [{"name": "gateway"}]},
            "status": {"phase": "Running", "containerStatuses": []},
        },
        {
            "metadata": {
                "name": "unlabelled",
                "namespace": "tenant-apps",
                "labels": {"app.kubernetes.io/name": "other"},
                "ownerReferences": [{"kind": "ReplicaSet", "uid": REPLICA_SET_UID}],
            },
            "spec": {"containers": [{"name": "gateway"}]},
            "status": {"phase": "Running", "containerStatuses": []},
        },
        {
            "metadata": {
                "name": "broker-abc",
                "namespace": "broker",
                "labels": {"app.kubernetes.io/name": "broker"},
            },
            "spec": {"containers": []},
            "status": {"podIP": "10.42.2.7", "phase": "Running"},
        },
    ]


def replica_sets() -> list[dict]:
    return [
        {
            "metadata": {
                "name": "nanobot-current",
                "uid": REPLICA_SET_UID,
                "ownerReferences": [{"kind": "Deployment", "name": "nanobot", "uid": DEPLOYMENT_UID}],
            }
        },
        {
            "metadata": {
                "name": "nanobot-old",
                "uid": OLD_REPLICA_SET_UID,
                "ownerReferences": [{"kind": "Deployment", "name": "nanobot", "uid": DEPLOYMENT_UID}],
            }
        },
        {
            "metadata": {
                "name": "broker-abc",
                "uid": "cccccccc-dddd-eeee-ffff-000000000000",
                "ownerReferences": [{"kind": "Deployment", "name": "broker", "uid": "other"}],
            }
        },
    ]


def deployment() -> dict:
    return {
        "metadata": {
            "name": "nanobot",
            "namespace": "tenant-apps",
            "uid": DEPLOYMENT_UID,
            "labels": {"app.kubernetes.io/name": "nanobot"},
            "annotations": {"kubectl.kubernetes.io/last-applied-configuration": "{}"},
        },
        "spec": {
            "replicas": 1,
            "template": {"spec": {"containers": [{"name": "gateway", "env": [
                {"name": "TOKEN", "valueFrom": {"secretKeyRef": {"name": "s", "key": "k"}}}
            ]}]}},
        },
        "status": {
            "readyReplicas": 1,
            "updatedReplicas": 1,
            "availableReplicas": 1,
            "conditions": [{"type": "Available", "status": "True"}],
        },
    }


class FakeClient:
    """Records every request and refuses what the RBAC scope forbids.

    Anything the walled garden would reject — a Secrets or ConfigMaps read, an
    unversioned path — raises instead of answering, so a handler that widened
    its own reach fails the suite rather than a review.
    """

    def __init__(self, responses: dict[str, object] | None = None) -> None:
        self.calls: list[tuple[str, str, object]] = []
        self.responses = responses or {}

    def _record(self, method: str, path: str, body: object = None) -> None:
        assert agent_control.api_path_allowed(path), f"forbidden API path: {path}"
        self.calls.append((method, path, body))

    def _answer(self, path: str) -> object:
        for prefix, response in self.responses.items():
            if path.startswith(prefix):
                return response
        return {"items": []}

    def get(self, path: str) -> object:
        self._record("GET", path)
        return self._answer(path)

    def get_text(self, path: str) -> str:
        self._record("GET", path)
        answer = self._answer(path)
        return answer if isinstance(answer, str) else ""

    def patch(self, path: str, body: dict) -> object:
        self._record("PATCH", path, body)
        return self._answer(path)


def control_with(responses: dict[str, object] | None = None) -> tuple:
    client = FakeClient(responses)
    return agent_control.Control(client), client


class RouteTest(unittest.TestCase):
    """The allowlist is exact: nine paths, their methods, nothing else."""

    def test_every_documented_path_is_routable(self) -> None:
        for method, path in [
            ("GET", "/self/status"),
            ("GET", "/self/logs"),
            ("GET", "/self/events"),
            ("POST", "/self/restart"),
            ("GET", "/cluster/pods"),
            ("GET", "/cluster/events"),
            ("GET", "/cluster/nodes"),
            ("GET", "/cluster/workloads"),
            ("GET", "/cluster/flux"),
        ]:
            route = agent_control.resolve_route(method, path)
            self.assertEqual(route.status, 200, path)
            self.assertEqual(route.path, path)

    def test_wrong_method_is_rejected(self) -> None:
        self.assertEqual(agent_control.resolve_route("GET", "/self/restart").status, 405)
        self.assertEqual(agent_control.resolve_route("POST", "/self/status").status, 405)
        self.assertEqual(agent_control.resolve_route("DELETE", "/cluster/pods").status, 405)

    def test_unknown_paths_are_not_found(self) -> None:
        for path in [
            "/",
            "",
            "/healthz",
            "/metrics",
            "/api",
            "/api/v1/secrets",
            "/api/v1/namespaces/tenant-apps/secrets",
            "/cluster/pods/nanobot",
            "/cluster/pods/",
            "/cluster/Pods",
            "/self/logs/",
            "/self",
            "/secrets",
            "/configmaps",
            "/../secret",
        ]:
            self.assertEqual(agent_control.resolve_route("GET", path).status, 404, path)
            self.assertEqual(agent_control.resolve_route("POST", path).status, 404, path)

    def test_traversal_that_lands_on_a_route_is_not_a_route(self) -> None:
        # urlsplit leaves the dot segments in place, so the raw target must
        # match a route exactly; nothing normalises it into an allowlisted path.
        for target in ["/cluster/./pods", "/self/../self/status", "//cluster/pods"]:
            self.assertEqual(agent_control.resolve_route("GET", target).status, 404, target)

    def test_query_string_is_not_part_of_the_route(self) -> None:
        route = agent_control.resolve_route("GET", "/self/logs?tail=7&token=s3cr3t")
        self.assertEqual(route.status, 200)
        self.assertEqual(route.path, "/self/logs")
        self.assertEqual(route.query, {"tail": "7"})


class TailTest(unittest.TestCase):
    def test_defaults_and_clamps(self) -> None:
        self.assertEqual(agent_control.clamp_tail(None), agent_control.DEFAULT_TAIL)
        self.assertEqual(agent_control.clamp_tail("50"), 50)
        self.assertEqual(agent_control.clamp_tail(" 50 "), 50)
        self.assertEqual(agent_control.clamp_tail("0"), 1)
        self.assertEqual(agent_control.clamp_tail("-1"), agent_control.DEFAULT_TAIL)
        self.assertEqual(agent_control.clamp_tail("9999"), agent_control.MAX_TAIL)
        self.assertEqual(agent_control.clamp_tail("1e3"), agent_control.DEFAULT_TAIL)
        self.assertEqual(agent_control.clamp_tail("abc"), agent_control.DEFAULT_TAIL)
        self.assertEqual(agent_control.clamp_tail("99999999999999999999"), agent_control.DEFAULT_TAIL)


class AuditTest(unittest.TestCase):
    """One line per request, and it can only carry the four named fields."""

    def test_line_shape(self) -> None:
        at = datetime(2026, 11, 13, 12, 0, 0, tzinfo=timezone.utc)
        line = agent_control.audit_line(
            "GET", "/self/status", 200, "broker/broker-abc", 12, at=at
        )
        self.assertEqual(
            line,
            "2026-11-13T12:00:00+00:00 level=audit method=GET path=/self/status "
            "status=200 caller=broker/broker-abc duration_ms=12",
        )
        self.assertNotIn("\n", line)

    def test_line_has_no_field_for_headers_or_bodies(self) -> None:
        # A fourth positional field would be a way to smuggle a token into the
        # log; the signature is the contract.
        self.assertEqual(
            list(inspect.signature(agent_control.audit_line).parameters),
            ["method", "path", "status", "caller", "duration_ms", "at"],
        )

    def test_query_is_never_logged(self) -> None:
        route = agent_control.resolve_route("GET", "/self/logs?tail=5&authorization=s3cr3t")
        line = agent_control.audit_line("GET", route.path, 200, "broker/broker-abc", 3)
        self.assertNotIn("s3cr3t", line)
        self.assertNotIn("authorization", line)
        self.assertIn("path=/self/logs ", line)

    def test_control_characters_cannot_break_the_line(self) -> None:
        line = agent_control.audit_line(
            "GET", "/self/status\ninjected", 200, "broker/x\ty", 1
        )
        self.assertEqual(len(line.splitlines()), 1)
        self.assertNotIn("\t", line)


class ScopeGuardTest(unittest.TestCase):
    """Secrets and ConfigMaps are refused in the client, not just absent."""

    def test_forbidden_resources_are_refused(self) -> None:
        for path in [
            "/api/v1/secrets",
            "/api/v1/namespaces/tenant-apps/secrets",
            "/api/v1/namespaces/tenant-apps/secrets/broker-credentials",
            "/api/v1/configmaps",
            "/api/v1/namespaces/broker/configmaps/broker-config",
            "/api/v1/namespaces/tenant-apps/SECRETS",
        ]:
            self.assertFalse(agent_control.api_path_allowed(path), path)

    def test_allowlisted_resources_pass_the_guard(self) -> None:
        for path in [
            "/api/v1/pods",
            "/api/v1/namespaces/tenant-apps/pods",
            "/api/v1/namespaces/tenant-apps/pods/nanobot-abc/log?tail=10&container=gateway",
            "/api/v1/events",
            "/api/v1/nodes",
            "/apis/apps/v1/deployments",
            "/apis/kustomize.toolkit.fluxcd.io/v1/kustomizations",
        ]:
            self.assertTrue(agent_control.api_path_allowed(path), path)

    def test_every_handler_path_stays_inside_the_scope(self) -> None:
        control, client = control_with(
            {
                "/apis/apps/v1/namespaces/tenant-apps/deployments/nanobot": deployment(),
                "/apis/apps/v1/namespaces/tenant-apps/replicasets": {"items": replica_sets()},
                "/api/v1/namespaces/tenant-apps/pods": {"items": pods_for_test()},
            }
        )
        control.self_status()
        control.self_logs(10)
        control.self_events()
        control.self_restart()
        control.cluster_pods()
        control.cluster_events()
        control.cluster_nodes()
        control.cluster_workloads()
        control.cluster_flux()
        forbidden = [
            path
            for _, path, _body in client.calls
            if not agent_control.api_path_allowed(path)
        ]
        self.assertEqual(forbidden, [])
        for method, path, _body in client.calls:
            self.assertNotIn("/secrets", path)
            self.assertNotIn("/configmaps", path)
            self.assertIn(method, {"GET", "PATCH"})


class LogFilterTest(unittest.TestCase):
    """`/self/logs` may only read pods the nanobot Deployment owns."""

    def test_replica_set_uids_follow_the_deployment_owner(self) -> None:
        uids = agent_control.replica_set_uids_for(replica_sets(), DEPLOYMENT_UID)
        self.assertEqual(uids, {REPLICA_SET_UID, OLD_REPLICA_SET_UID})

    def test_selector_requires_both_label_and_ownership(self) -> None:
        pods = agent_control.select_self_pods(
            pods_for_test(), agent_control.replica_set_uids_for(replica_sets(), DEPLOYMENT_UID)
        )
        self.assertEqual([p["metadata"]["name"] for p in pods], ["nanobot-current", "nanobot-old"])

    def test_lookalikes_are_excluded(self) -> None:
        pods = agent_control.select_self_pods(pods_for_test(), {REPLICA_SET_UID})
        self.assertEqual([p["metadata"]["name"] for p in pods], ["nanobot-current"])

    def test_logs_are_read_per_started_container_only(self) -> None:
        pending = {
            "metadata": {
                "name": "nanobot-pending",
                "namespace": "tenant-apps",
                "labels": {"app.kubernetes.io/name": "nanobot"},
                "ownerReferences": [{"kind": "ReplicaSet", "uid": REPLICA_SET_UID}],
            },
            "spec": {"containers": [{"name": "gateway"}, {"name": "sidecar"}]},
            "status": {
                "phase": "Pending",
                "containerStatuses": [
                    {"name": "gateway", "ready": False, "restartCount": 0, "state": {"waiting": {}}}
                ],
            },
        }
        running = pods_for_test()[0]
        control, client = control_with(
            {
                "/apis/apps/v1/namespaces/tenant-apps/deployments/nanobot": deployment(),
                "/apis/apps/v1/namespaces/tenant-apps/replicasets": {"items": replica_sets()},
                "/api/v1/namespaces/tenant-apps/pods": {"items": [running, pending]},
                "/api/v1/namespaces/tenant-apps/pods/nanobot-current/log": "line one\nline two\n",
            }
        )
        payload = control.self_logs(5)
        log_calls = [call for call in client.calls if call[1].endswith("/log?tail=5&container=gateway")]
        self.assertEqual(len(log_calls), 1, client.calls)
        self.assertIn("nanobot-current", log_calls[0][1])
        self.assertEqual(
            [p["pod"] for p in payload["pods"]], ["nanobot-current", "nanobot-pending"]
        )
        self.assertEqual(payload["tail"], 5)

    def test_tail_is_clamped_before_it_reaches_the_api(self) -> None:
        control, client = control_with(
            {
                "/apis/apps/v1/namespaces/tenant-apps/deployments/nanobot": deployment(),
                "/apis/apps/v1/namespaces/tenant-apps/replicasets": {"items": replica_sets()},
                "/api/v1/namespaces/tenant-apps/pods": {"items": pods_for_test()},
            }
        )
        control.self_logs(agent_control.clamp_tail("9999"))
        self.assertTrue(
            any("tail=500" in path for _, path, _ in client.calls), client.calls
        )

    def test_no_nanobot_pods_means_no_log_reads(self) -> None:
        control, client = control_with(
            {
                "/apis/apps/v1/namespaces/tenant-apps/deployments/nanobot": deployment(),
                "/apis/apps/v1/namespaces/tenant-apps/replicasets": {"items": []},
                "/api/v1/namespaces/tenant-apps/pods": {"items": pods_for_test()},
            }
        )
        payload = control.self_logs(10)
        self.assertEqual(payload["pods"], [])
        self.assertFalse(any("/log?" in path for _, path, _ in client.calls))


class RestartTest(unittest.TestCase):
    """The one write: a pod-template annotation on deployments/nanobot."""

    def test_restart_patches_only_the_annotation(self) -> None:
        control, client = control_with()
        payload = control.self_restart()
        patches = [call for call in client.calls if call[0] == "PATCH"]
        self.assertEqual(len(patches), 1)
        _method, path, body = patches[0]
        self.assertEqual(path, "/apis/apps/v1/namespaces/tenant-apps/deployments/nanobot")
        self.assertEqual(list(body), ["spec"])
        self.assertEqual(list(body["spec"]), ["template"])
        self.assertEqual(list(body["spec"]["template"]), ["metadata"])
        self.assertEqual(
            list(body["spec"]["template"]["metadata"]), ["annotations"]
        )
        annotations = body["spec"]["template"]["metadata"]["annotations"]
        self.assertEqual(list(annotations), [agent_control.RESTART_ANNOTATION])
        self.assertRegex(annotations[agent_control.RESTART_ANNOTATION], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertEqual(payload["deployment"], "tenant-apps/nanobot")

    def test_restart_never_names_another_deployment(self) -> None:
        control, _client = control_with()
        control.self_restart()
        self.assertNotIn("broker", control.client.calls[0][1])


class EventFilterTest(unittest.TestCase):
    def test_only_nanobot_objects_are_reported(self) -> None:
        events = [
            {"metadata": {"name": "a"}, "involvedObject": {"kind": "Pod", "name": "nanobot-current"}},
            {"metadata": {"name": "b"}, "involvedObject": {"kind": "Pod", "name": "immich-server-0"}},
            {"metadata": {"name": "c"}, "involvedObject": {"kind": "Deployment", "name": "nanobot"}},
        ]
        kept = agent_control.select_self_events(events, {"nanobot-current", "nanobot"})
        self.assertEqual([e["metadata"]["name"] for e in kept], ["a", "c"])


class CurationTest(unittest.TestCase):
    """Nothing a pod spec carries (env, secret refs, volumes) leaves the service."""

    def test_pod_curation_drops_the_spec(self) -> None:
        payload = agent_control.curate_pod(pods_for_test()[0])
        rendered = repr(payload)
        self.assertNotIn("spec", payload)
        self.assertNotIn("configMap", rendered)
        self.assertEqual(payload["name"], "nanobot-current")
        self.assertEqual(payload["namespace"], "tenant-apps")
        self.assertEqual(payload["phase"], "Running")
        self.assertEqual(payload["ready"], 1)
        self.assertEqual(payload["restarts"], 2)
        self.assertEqual(payload["labels"], {"app.kubernetes.io/name": "nanobot"})

    def test_deployment_curation_drops_the_template(self) -> None:
        payload = agent_control.curate_deployment(deployment())
        self.assertNotIn("spec", payload)
        self.assertNotIn("TOKEN", repr(payload))
        self.assertEqual(payload["name"], "nanobot")
        self.assertEqual(payload["desired"], 1)
        self.assertEqual(payload["ready"], 1)

    def test_workload_curation_is_metadata_only(self) -> None:
        workload = {
            "kind": "Deployment",
            "metadata": {
                "name": "immich-server",
                "namespace": "immich",
                "creationTimestamp": "2026-11-01T00:00:00Z",
                "labels": {
                    "app.kubernetes.io/name": "immich",
                    "homelabs.dev/managed-by": "platform",
                },
            },
            "spec": {"template": {"spec": {"containers": [{"env": [{"name": "DB_PASSWORD"}]}]}}},
            "status": {"readyReplicas": 1},
        }
        payload = agent_control.curate_workload(workload)
        self.assertEqual(
            payload,
            {
                "kind": "Deployment",
                "namespace": "immich",
                "name": "immich-server",
                "createdAt": "2026-11-01T00:00:00Z",
                "app": "immich",
            },
        )

    def test_kustomization_curation_keeps_only_readiness(self) -> None:
        kustomization = {
            "metadata": {"name": "homelabs-apps", "namespace": "flux-system"},
            "spec": {
                "interval": "10m",
                "suspended": False,
                "path": "./deploy",
                "postBuild": {"substitute": {"TOKEN": "s3cr3t"}},
            },
            "status": {
                "lastAppliedRevision": "main@sha1:abc",
                "conditions": [{"type": "Ready", "status": "True", "message": "Applied revision"}],
            },
        }
        payload = agent_control.curate_kustomization(kustomization)
        self.assertNotIn("s3cr3t", repr(payload))
        self.assertEqual(payload["name"], "homelabs-apps")
        self.assertEqual(payload["ready"], True)

    def test_node_curation_reports_readiness_only(self) -> None:
        node = {
            "metadata": {"name": "cluster", "labels": {"node-role.kubernetes.io/master": ""}},
            "spec": {"podCIDR": "10.42.0.0/24"},
            "status": {
                "capacity": {"cpu": "8", "memory": "16Gi", "pods": "110"},
                "nodeInfo": {"kubeletVersion": "v1.31.4+k3s1"},
                "conditions": [{"type": "Ready", "status": "True"}],
            },
        }
        payload = agent_control.curate_node(node)
        self.assertEqual(payload["name"], "cluster")
        self.assertEqual(payload["ready"], True)
        self.assertEqual(payload["version"], "v1.31.4+k3s1")
        self.assertEqual(payload["capacity"], {"cpu": "8", "memory": "16Gi"})


class CallerTest(unittest.TestCase):
    def test_caller_is_resolved_by_pod_ip(self) -> None:
        self.assertEqual(
            agent_control.caller_from_pods(pods_for_test(), "10.42.2.7"), "broker/broker-abc"
        )

    def test_unknown_caller_falls_back_to_the_address(self) -> None:
        self.assertEqual(
            agent_control.caller_from_pods(pods_for_test(), "10.42.9.9"), "10.42.9.9"
        )


class SizeTest(unittest.TestCase):
    def test_event_list_is_capped_newest_first(self) -> None:
        events = [
            {"metadata": {"name": str(i)}, "lastTimestamp": f"2026-11-13T00:{i // 60:02d}:{i % 60:02d}Z"}
            for i in range(agent_control.MAX_EVENTS + 20)
        ]
        kept = agent_control.cap_events(
            [{"metadata": e["metadata"], "lastTimestamp": e["lastTimestamp"]} for e in events]
        )
        self.assertEqual(len(kept), agent_control.MAX_EVENTS)
        self.assertEqual(kept[0]["lastTimestamp"], "2026-11-13T00:02:19Z")


if __name__ == "__main__":
    unittest.main()
