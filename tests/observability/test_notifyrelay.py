"""Unit tests for the cluster's notify relay.

The relay is deployed from source through a generated ConfigMap, so this module
is the only place its title/body/priority contract is checked without a cluster
or an ntfy token.
"""

from __future__ import annotations

import importlib.util
import pathlib
import unittest

SOURCE = (
    pathlib.Path(__file__).resolve().parents[2]
    / "clusters/homelab/observability/notifyrelay/notifyrelay.py"
)
spec = importlib.util.spec_from_file_location("notifyrelay", SOURCE)
assert spec and spec.loader
notifyrelay = importlib.util.module_from_spec(spec)
spec.loader.exec_module(notifyrelay)


def alert(name: str, status: str = "firing", **labels: str) -> dict:
    return {
        "status": status,
        "labels": {"alertname": name, "namespace": "immich", **labels},
        "annotations": {"summary": f"{name} summary", "description": f"{name} detail"},
        "generatorURL": "http://prometheus:9090/graph",
    }


def flux_event(severity: str = "error", **overrides: object) -> dict:
    payload = {
        "involvedObject": {
            "kind": "Kustomization",
            "namespace": "flux-system",
            "name": "platform-observability",
            "apiVersion": "kustomize.toolkit.fluxcd.io/v1",
        },
        "severity": severity,
        "reason": "ReconciliationFailed",
        "message": "kustomize build failed: accumulating resources: no such file",
        "reportingController": "kustomize-controller",
        "reportingInstance": "kustomize-controller-6d9f7c8b5-x2vql",
        "timestamp": "2026-11-14T03:00:00Z",
        "metadata": {"revision": "main@sha1:0f1e2d3c"},
    }
    payload.update(overrides)
    return payload


class AlertmanagerTest(unittest.TestCase):
    def test_firing_batch_names_the_alert_and_is_high_priority(self) -> None:
        title, body, priority = notifyrelay.summarize_alertmanager(
            {"alerts": [alert("ResticBackupStale")]}
        )
        self.assertEqual("[cluster] 1 firing: ResticBackupStale", title)
        self.assertEqual("high", priority)
        self.assertIn("status: 1 firing", body)
        self.assertIn("* firing: ResticBackupStale", body)
        self.assertIn("namespace=immich", body)
        self.assertIn("summary: ResticBackupStale summary", body)

    def test_resolved_batch_says_resolved_and_is_not_high_priority(self) -> None:
        title, body, priority = notifyrelay.summarize_alertmanager(
            {"alerts": [alert("TargetDown", status="resolved")]}
        )
        self.assertEqual("[cluster] 1 resolved: TargetDown", title)
        self.assertEqual("default", priority, "a recovery must not buzz like a failure")
        self.assertIn("* resolved: TargetDown", body)

    def test_mixed_batch_counts_both_and_stays_high_priority(self) -> None:
        title, _, priority = notifyrelay.summarize_alertmanager(
            {
                "alerts": [
                    alert("TargetDown"),
                    alert("ResticBackupStale", status="resolved"),
                ]
            }
        )
        self.assertEqual("[cluster] 1 firing, 1 resolved: TargetDown, ResticBackupStale", title)
        self.assertEqual("high", priority)

    def test_many_alert_names_are_truncated_in_the_title(self) -> None:
        title, body, _ = notifyrelay.summarize_alertmanager(
            {"alerts": [alert("A"), alert("B"), alert("C"), alert("D")]}
        )
        self.assertEqual("[cluster] 4 firing: A, B (+2)", title)
        # The body still carries every alert: the title is a preview, not the message.
        for name in ("A", "B", "C", "D"):
            self.assertIn(f"* firing: {name}", body)

    def test_empty_batch_is_still_a_valid_message(self) -> None:
        title, body, priority = notifyrelay.summarize_alertmanager({"alerts": []})
        self.assertEqual("[cluster] no alerts: ", title)
        self.assertEqual("default", priority)
        self.assertIn("Alertmanager sent a batch with no alerts.", body)


class FluxTest(unittest.TestCase):
    def test_error_event_names_the_object_and_is_high_priority(self) -> None:
        title, body, priority = notifyrelay.summarize_flux(flux_event())
        self.assertEqual(
            "[cluster] Kustomization flux-system/platform-observability: ReconciliationFailed",
            title,
        )
        self.assertEqual("high", priority)
        self.assertIn("severity: error", body)
        self.assertIn("object: Kustomization flux-system/platform-observability", body)
        self.assertIn("controller: kustomize-controller", body)
        self.assertIn("revision: main@sha1:0f1e2d3c", body)
        self.assertIn("kustomize build failed", body)

    def test_info_event_is_not_high_priority(self) -> None:
        _, _, priority = notifyrelay.summarize_flux(flux_event(severity="info"))
        self.assertEqual("default", priority)

    def test_missing_fields_do_not_raise(self) -> None:
        title, body, priority = notifyrelay.summarize_flux({})
        self.assertEqual("[cluster] object unknown", title)
        self.assertEqual("default", priority)
        self.assertIn("(no message)", body)

    def test_cluster_scoped_object_has_no_namespace_prefix(self) -> None:
        payload = flux_event()
        payload["involvedObject"] = {"kind": "GitRepository", "name": "flux-system"}
        title, _, _ = notifyrelay.summarize_flux(payload)
        self.assertEqual("[cluster] GitRepository flux-system: ReconciliationFailed", title)


class FitTest(unittest.TestCase):
    def test_short_body_is_untouched(self) -> None:
        self.assertEqual("hello\n", notifyrelay.fit("hello\n"))

    def test_long_body_is_truncated_to_the_limit(self) -> None:
        body = "x" * (notifyrelay.MAX_BODY_BYTES * 2)
        fitted = notifyrelay.fit(body)
        self.assertLessEqual(len(fitted.encode()), notifyrelay.MAX_BODY_BYTES)
        self.assertTrue(fitted.endswith(notifyrelay.TRUNCATED))
        self.assertTrue(fitted.startswith("x" * 100), "the head is what names the alerts")

    def test_multibyte_body_is_truncated_on_a_character_boundary(self) -> None:
        body = "é" * notifyrelay.MAX_BODY_BYTES
        fitted = notifyrelay.fit(body)
        self.assertLessEqual(len(fitted.encode()), notifyrelay.MAX_BODY_BYTES)
        self.assertTrue(fitted.endswith(notifyrelay.TRUNCATED))


class PublishTest(unittest.TestCase):
    def test_transport_failure_is_an_exception(self) -> None:
        # The port is closed in the sandbox, which is the failure path a 5xx from
        # the bus takes: the sender retries instead of losing the alert.
        with self.assertRaises(Exception):
            notifyrelay.publish(
                "title",
                "body",
                "high",
                base_url="http://127.0.0.1:1",
                topic="alerts",
                token="tk_" + "a" * 29,
            )


if __name__ == "__main__":
    unittest.main()
