"""Unit tests for the cluster digest builder.

The builder is deployed from source through a generated ConfigMap, so this
module is the only place its redaction, ordering and size rules are checked
without a cluster. The planted fixture is the same one the docs reference:
allowlisted classification survives, a message body, a prompt and a tool
payload do not.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import pathlib
import tempfile
import unittest
import urllib.parse
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parents[2]
SOURCE = ROOT / "clusters/homelab/platform/digest/digest_builder.py"
FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures"

spec = importlib.util.spec_from_file_location("digest_builder", SOURCE)
assert spec and spec.loader
digest = importlib.util.module_from_spec(spec)
spec.loader.exec_module(digest)

GENERATED_AT = datetime(2026, 11, 11, 4, 10, 0, tzinfo=timezone.utc)
PLANTED = ("PROMPT", "Vacation", "IMG_1234", "private letter", "diary", "read_file", "shadow")


def exporter_text() -> str:
    return (FIXTURES / "status-metrics.txt").read_text(encoding="utf-8")


def loki_payload() -> dict:
    return json.loads((FIXTURES / "loki-planted.json").read_text(encoding="utf-8"))


def build(**overrides) -> dict:
    kwargs = {
        "exporter_text": exporter_text(),
        "loki_payload": loki_payload(),
        "generated_at": GENERATED_AT,
    }
    kwargs.update(overrides)
    return digest.build(**kwargs)


class PrometheusTest(unittest.TestCase):
    def test_comments_and_blank_lines_are_skipped(self) -> None:
        samples = digest.parse_prometheus("# HELP x y\n\nm{a=\"1\"} 2\n")
        self.assertEqual([{"name": "m", "labels": {"a": "1"}, "value": 2.0}], samples)

    def test_escaped_label_values_are_unescaped(self) -> None:
        samples = digest.parse_prometheus('m{pod="a\\"b\\\\c"} 1\n')
        self.assertEqual('a"b\\c', samples[0]["labels"]["pod"])

    def test_malformed_line_is_skipped(self) -> None:
        self.assertEqual([], digest.parse_prometheus("not a sample\nm 1 extra tokens\n"))


class ClusterHealthTest(unittest.TestCase):
    def setUp(self) -> None:
        self.health = digest.cluster_health(digest.parse_prometheus(exporter_text()))

    def test_node_readiness_is_reported(self) -> None:
        self.assertEqual([{"name": "cluster", "ready": True}], self.health["nodes"])

    def test_pods_carry_readiness_and_restarts(self) -> None:
        self.assertEqual(
            [
                {"namespace": "immich", "pod": "immich-server-0", "ready": True, "restarts": 3},
                {"namespace": "immich", "pod": "immich-server-1", "ready": False, "restarts": 5},
            ],
            self.health["pods"],
        )

    def test_workload_restarts_sum_by_pod_prefix(self) -> None:
        workloads = {workload["name"]: workload for workload in self.health["workloads"]}
        self.assertEqual(8, workloads["immich"]["restarts"])
        self.assertEqual(0, workloads["postgres"]["restarts"])
        self.assertFalse(workloads["immich"]["ready"])

    def test_images_are_versions_from_the_exporter(self) -> None:
        workloads = {workload["name"]: workload for workload in self.health["workloads"]}
        self.assertEqual(["ghcr.io/immich-app/immich-server:v2"], workloads["immich"]["images"])

    def test_zero_sample_is_not_ready(self) -> None:
        self.assertEqual(
            [{"name": "cluster", "ready": False}],
            digest.cluster_health(
                [{"name": "homelabs_cluster_node_ready", "labels": {"node": "cluster"}, "value": 0.0}]
            )["nodes"],
        )


class QueryTest(unittest.TestCase):
    def test_query_url_pins_the_data_tenant_and_the_window(self) -> None:
        url = digest.loki_query_url("http://gw:3100/", "15m", 10, GENERATED_AT)
        self.assertTrue(url.startswith("http://gw:3100/loki/api/v1/query_range?"))
        parsed = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        self.assertEqual(['{tier="data"}'], parsed["query"])
        self.assertEqual(["10"], parsed["limit"])
        span = (int(parsed["end"][0]) - int(parsed["start"][0])) // 1_000_000_000
        self.assertEqual(900, span)

    def test_unknown_window_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            digest.loki_query_url("http://gw:3100", "15d", 10, GENERATED_AT)


class RedactionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.document = build()
        self.payload = digest.encode(self.document)

    def test_only_allowlisted_fields_survive(self) -> None:
        allowed = set(digest.ACTIVITY_ALLOWLIST)
        for event in self.document["nanobot_activity"]["events"]:
            self.assertLessEqual(set(event), allowed, event)

    def test_planted_prompt_body_and_tool_payload_are_absent(self) -> None:
        text = self.payload.decode("utf-8")
        for needle in PLANTED:
            self.assertNotIn(needle, text)

    def test_stream_labels_other_than_the_host_are_dropped(self) -> None:
        for event in self.document["nanobot_activity"]["events"]:
            self.assertNotIn("pod", event)
            self.assertNotIn("container", event)
            self.assertNotIn("tier", event)

    def test_non_json_line_contributes_nothing(self) -> None:
        hosts = {event["logical_host"] for event in self.document["nanobot_activity"]["events"]}
        self.assertEqual({"immich", "graphiti"}, hosts)
        graphiti = [
            event
            for event in self.document["nanobot_activity"]["events"]
            if event["logical_host"] == "graphiti"
        ]
        self.assertEqual(1, len(graphiti))
        self.assertEqual("warn", graphiti[0]["level"])

    def test_full_classification_line_is_kept(self) -> None:
        immich = [
            event
            for event in self.document["nanobot_activity"]["events"]
            if event.get("error_code") == "E1234"
        ][0]
        self.assertEqual("immich-server", immich["component"])
        self.assertEqual("500", immich["status"])
        self.assertEqual("2026-11-11T04:00:00Z", immich["timestamp"])

    def test_control_characters_and_long_fields_are_bounded(self) -> None:
        event, _ = digest.activity_from_loki(
            {
                "data": {
                    "result": [
                        {
                            "stream": {"namespace": "immich"},
                            "values": [
                                [
                                    "1762826400000000000",
                                    json.dumps(
                                        {"level": "err\x1b[31m" + "x" * 200, "component": "c"}
                                    ),
                                ]
                            ],
                        }
                    ]
                }
            }
        )
        self.assertEqual("err[31m" + "x" * (digest.FIELD_CHARS - len("err[31m")), event[0]["level"])


class DeterminismTest(unittest.TestCase):
    def test_same_inputs_produce_identical_bytes(self) -> None:
        self.assertEqual(digest.encode(build()), digest.encode(build()))

    def test_input_order_does_not_change_the_bytes(self) -> None:
        reordered = copy.deepcopy(loki_payload())
        result = reordered["data"]["result"]
        result.reverse()
        for stream in result:
            stream["values"].reverse()
        self.assertEqual(
            digest.encode(build()),
            digest.encode(build(loki_payload=reordered)),
        )

    def test_keys_are_sorted_in_the_output(self) -> None:
        document = build()
        text = digest.encode(document).decode("utf-8")
        self.assertTrue(text.startswith('{"cluster":'))
        self.assertIn('"generated_at":"2026-11-11T04:10:00Z"', text)

    def test_events_are_in_total_order(self) -> None:
        keys = [
            tuple(event.get(field, "") for field in digest.ACTIVITY_ALLOWLIST)
            for event in build()["nanobot_activity"]["events"]
        ]
        self.assertEqual(sorted(keys), keys)


class BoundsTest(unittest.TestCase):
    def _many(self, count: int) -> dict:
        values = [
            [
                str(1762826400000000000 + index),
                json.dumps(
                    {
                        "timestamp": f"2026-11-11T04:{index // 60:02d}:{index % 60:02d}Z",
                        "level": "info" * 5,
                        "component": "component-" + "z" * 40,
                        "status": "200",
                    }
                ),
            ]
            for index in range(count)
        ]
        return {"data": {"result": [{"stream": {"namespace": "immich"}, "values": values}]}}

    def test_event_cap_and_truncation_flag(self) -> None:
        document = build(loki_payload=self._many(50), max_events=10)
        activity = document["nanobot_activity"]
        self.assertEqual(50, activity["observed_events"])
        self.assertEqual(10, activity["event_count"])
        self.assertTrue(activity["truncated"])

    def test_byte_budget_drops_oldest_events(self) -> None:
        document = build(loki_payload=self._many(400), max_bytes=4096)
        payload = digest.fit(document, max_bytes=4096)
        self.assertLessEqual(len(payload), 4096)
        self.assertTrue(document["nanobot_activity"]["truncated"])
        # The newest event survives; the oldest does not.
        events = json.loads(payload)["nanobot_activity"]["events"]
        self.assertEqual("2026-11-11T04:06:39Z", events[-1]["timestamp"])
        self.assertNotEqual("2026-11-11T04:00:00Z", events[0]["timestamp"])

    def test_mandatory_sections_over_budget_fail_closed(self) -> None:
        document = build(loki_payload={"data": {"result": []}})
        with self.assertRaises(digest.DigestTooLarge):
            digest.fit(document, max_bytes=16)

    def test_limits_are_published(self) -> None:
        limits = build()["limits"]
        self.assertEqual(digest.MAX_BYTES, limits["max_bytes"])
        self.assertEqual(digest.MAX_FILES, limits["max_files"])
        self.assertEqual(digest.MAX_EVENTS, limits["max_events"])


class FreshnessTest(unittest.TestCase):
    def test_successful_sources_report_zero_or_cached_age(self) -> None:
        freshness = build()["source_freshness"]
        self.assertEqual({"ok": True, "age_seconds": 30}, freshness["status_exporter"])
        self.assertEqual({"ok": True, "age_seconds": 0}, freshness["loki_gateway"])

    def test_failed_sources_report_not_ok_and_null_age(self) -> None:
        freshness = build(exporter_ok=False, loki_ok=False)["source_freshness"]
        self.assertEqual({"ok": False, "age_seconds": None}, freshness["status_exporter"])
        self.assertEqual({"ok": False, "age_seconds": None}, freshness["loki_gateway"])


class PublishTest(unittest.TestCase):
    def test_latest_and_snapshot_are_written_without_temp_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            document = build()
            summary = digest.publish(directory, document)
            names = sorted(path.name for path in pathlib.Path(directory).iterdir())
            self.assertEqual(
                ["digest-20261111T041000Z.json", "latest.json"], names
            )
            self.assertEqual(
                json.loads((pathlib.Path(directory) / "latest.json").read_text()),
                json.loads((pathlib.Path(directory) / summary["snapshot"]).read_text()),
            )
            self.assertEqual([], summary["pruned"])

    def test_publish_replaces_latest_and_keeps_history(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            digest.publish(directory, build())
            later = datetime(2026, 11, 11, 4, 25, 0, tzinfo=timezone.utc)
            digest.publish(directory, build(generated_at=later))
            latest = json.loads((pathlib.Path(directory) / "latest.json").read_text())
            self.assertEqual("2026-11-11T04:25:00Z", latest["generated_at"])
            self.assertEqual(
                ["digest-20261111T041000Z.json", "digest-20261111T042500Z.json", "latest.json"],
                sorted(path.name for path in pathlib.Path(directory).iterdir()),
            )

    def test_prune_keeps_the_newest_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            for minute in range(5):
                moment = datetime(2026, 11, 11, 4, minute, 0, tzinfo=timezone.utc)
                digest.publish(directory, build(generated_at=moment), keep=2)
            names = sorted(path.name for path in pathlib.Path(directory).iterdir())
            self.assertEqual(
                ["digest-20261111T040300Z.json", "digest-20261111T040400Z.json", "latest.json"],
                names,
            )

    def test_main_fails_closed_without_a_token(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            code = digest.main(
                [
                    "--output",
                    directory,
                    "--token-file",
                    str(pathlib.Path(directory) / "missing-token"),
                ]
            )
            self.assertEqual(2, code)
            self.assertEqual([], list(pathlib.Path(directory).iterdir()))


if __name__ == "__main__":
    unittest.main()
