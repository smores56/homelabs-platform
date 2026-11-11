"""Unit tests for the human-owned status exporter.

The exporter is deployed from source through a generated ConfigMap, so this
module is the only place its metric semantics are checked without a cluster.
"""

from __future__ import annotations

import importlib.util
import pathlib
import unittest

SOURCE = (
    pathlib.Path(__file__).resolve().parents[2]
    / "clusters/homelab/observability/status-exporter/exporter.py"
)
spec = importlib.util.spec_from_file_location("status_exporter", SOURCE)
assert spec and spec.loader
exporter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(exporter)


def pod(ready_states: list[bool], restarts: list[int]) -> dict:
    return {
        "metadata": {"name": "immich-server-0", "namespace": "data"},
        "status": {
            "containerStatuses": [
                {"ready": ready, "restartCount": restarts[i]}
                for i, ready in enumerate(ready_states)
            ]
        },
    }


def node(ready: str | None) -> dict:
    conditions = [] if ready is None else [{"type": "Ready", "status": ready}]
    return {"metadata": {"name": "cluster"}, "status": {"conditions": conditions}}


class NodeMetricsTest(unittest.TestCase):
    def test_ready_condition_true(self) -> None:
        self.assertEqual(1.0, exporter.node_ready(node("True")))

    def test_ready_condition_false(self) -> None:
        self.assertEqual(0.0, exporter.node_ready(node("False")))

    def test_no_conditions_is_not_ready(self) -> None:
        self.assertEqual(0.0, exporter.node_ready(node(None)))

    def test_sample_carries_the_node_name(self) -> None:
        self.assertEqual(
            ['homelabs_cluster_node_ready{node="cluster"} 1.0'],
            exporter.node_metrics([node("True")]),
        )


class PodMetricsTest(unittest.TestCase):
    def test_all_containers_ready(self) -> None:
        self.assertEqual(1.0, exporter.pod_ready(pod([True, True], [0, 0])))

    def test_any_container_unready(self) -> None:
        self.assertEqual(0.0, exporter.pod_ready(pod([True, False], [0, 1])))

    def test_no_container_statuses_is_not_ready(self) -> None:
        self.assertEqual(0.0, exporter.pod_ready({"status": {}}))

    def test_restarts_sum_across_containers(self) -> None:
        self.assertEqual(7, exporter.pod_restarts(pod([True, True], [4, 3])))

    def test_samples_carry_namespace_and_pod(self) -> None:
        lines = exporter.pod_metrics([pod([True], [2])])
        self.assertIn(
            'homelabs_data_pod_ready{namespace="data",pod="immich-server-0"} 1.0', lines
        )
        self.assertIn(
            'homelabs_data_pod_restart_count{namespace="data",pod="immich-server-0"} 2', lines
        )


def workload(kind: str, name: str, replicas: int | None, ready: int | None, *images: str) -> dict:
    spec: dict = {}
    if replicas is not None:
        spec["replicas"] = replicas
    spec["template"] = {"spec": {"containers": [{"image": image} for image in images]}}
    status: dict = {}
    if ready is not None:
        status["readyReplicas"] = ready
    return {
        "kind": kind,
        "metadata": {"name": name, "namespace": "data"},
        "spec": spec,
        "status": status,
    }


class WorkloadMetricsTest(unittest.TestCase):
    def test_partial_readiness(self) -> None:
        self.assertEqual(0.0, exporter.workload_ready(workload("Deployment", "immich", 2, 1)))

    def test_full_readiness(self) -> None:
        self.assertEqual(1.0, exporter.workload_ready(workload("StatefulSet", "pg", 1, 1)))

    def test_scaled_to_zero_is_ready(self) -> None:
        self.assertEqual(1.0, exporter.workload_ready(workload("Deployment", "off", 0, None)))

    def test_images_are_sorted_and_deduplicated(self) -> None:
        subject = workload(
            "Deployment",
            "immich",
            1,
            1,
            "ghcr.io/immich-app/immich-server:v2",
            "ghcr.io/immich-app/immich-server:v2",
            "redis:7.4",
        )
        self.assertEqual(
            ["ghcr.io/immich-app/immich-server:v2", "redis:7.4"],
            exporter.workload_images(subject),
        )

    def test_image_metric_is_an_info_sample(self) -> None:
        lines = exporter.workload_metrics([workload("Deployment", "immich", 1, 1, "redis:7.4")])
        self.assertIn(
            'homelabs_data_workload_image{image="redis:7.4",kind="Deployment",'
            'name="immich",namespace="data"} 1',
            lines,
        )


class RenderTest(unittest.TestCase):
    def test_label_values_are_escaped(self) -> None:
        line = exporter.render_sample("m", {"pod": 'a"b\\c'}, 1)
        self.assertEqual('m{pod="a\\"b\\\\c"} 1', line)

    def test_render_reports_scrape_success(self) -> None:
        text = exporter.render(["homelabs_data_pod_ready{a=\"b\"} 1"], True, 1794370185)
        self.assertIn("homelabs_data_pod_ready{a=\"b\"} 1", text)
        self.assertIn("homelabs_data_status_exporter_last_scrape_success 1", text)
        self.assertIn(
            "homelabs_data_status_exporter_last_scrape_timestamp_seconds 1794370185", text
        )
        self.assertTrue(text.endswith("\n"))

    def test_failed_scrape_still_serves_up(self) -> None:
        text = exporter.render([], False, 0)
        self.assertIn("homelabs_data_status_exporter_up 1", text)
        self.assertIn("homelabs_data_status_exporter_last_scrape_success 0", text)
        self.assertIn(
            "homelabs_data_status_exporter_last_scrape_timestamp_seconds 0", text
        )


class CacheTest(unittest.TestCase):
    """The age the digest reports is only real if this timestamp is honest."""

    class Client:
        def __init__(self, fail: bool = False) -> None:
            self.fail = fail
            self.calls = 0

        def collect(self) -> list[str]:
            self.calls += 1
            if self.fail:
                raise OSError("api unavailable")
            return ["homelabs_data_status_exporter_up{} 1"]

    def test_successful_scrape_stamps_wall_clock_time(self) -> None:
        cache = exporter.Cache(self.Client(), refresh_seconds=0)
        self.assertIn("homelabs_data_status_exporter_last_scrape_success 1", cache.metrics())
        self.assertGreater(cache.last_success_at, 0)
        self.assertIn(
            f"homelabs_data_status_exporter_last_scrape_timestamp_seconds {int(cache.last_success_at)}",
            cache.metrics(),
        )

    def test_a_failed_scrape_keeps_the_last_good_timestamp(self) -> None:
        client = self.Client()
        cache = exporter.Cache(client, refresh_seconds=0)
        cache.metrics()
        good = cache.last_success_at
        client.fail = True
        text = cache.metrics()
        self.assertIn("homelabs_data_status_exporter_last_scrape_success 0", text)
        # The age keeps growing from the last success instead of resetting, which
        # is what makes a stale exporter visible downstream.
        self.assertEqual(good, cache.last_success_at)
        self.assertIn(
            f"homelabs_data_status_exporter_last_scrape_timestamp_seconds {int(good)}", text
        )


if __name__ == "__main__":
    unittest.main()
