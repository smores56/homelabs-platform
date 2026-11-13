"""Unit tests for the Alertmanager mail relay.

The relay is deployed from source through a generated ConfigMap, so this module
is the only place its subject/body contract is checked without a cluster or a
Cloudflare token.
"""

from __future__ import annotations

import importlib.util
import pathlib
import unittest

SOURCE = (
    pathlib.Path(__file__).resolve().parents[2]
    / "clusters/homelab/observability/mailrelay/mailrelay.py"
)
spec = importlib.util.spec_from_file_location("mailrelay", SOURCE)
assert spec and spec.loader
mailrelay = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mailrelay)


def alert(name: str, status: str = "firing", **labels: str) -> dict:
    return {
        "status": status,
        "labels": {"alertname": name, "namespace": "immich", **labels},
        "annotations": {"summary": f"{name} summary", "description": f"{name} detail"},
        "generatorURL": "http://prometheus:9090/graph",
    }


class SummarizeTest(unittest.TestCase):
    def test_firing_batch_names_the_alert(self) -> None:
        subject, body = mailrelay.summarize({"alerts": [alert("ResticBackupStale")]})
        self.assertEqual("[cluster] 1 firing: ResticBackupStale", subject)
        self.assertIn("status: 1 firing", body)
        self.assertIn("* firing: ResticBackupStale", body)
        self.assertIn("namespace=immich", body)
        self.assertIn("summary: ResticBackupStale summary", body)

    def test_resolved_batch_says_resolved(self) -> None:
        subject, body = mailrelay.summarize(
            {"alerts": [alert("TargetDown", status="resolved")]}
        )
        self.assertEqual("[cluster] 1 resolved: TargetDown", subject)
        self.assertIn("* resolved: TargetDown", body)

    def test_mixed_batch_counts_both(self) -> None:
        subject, _ = mailrelay.summarize(
            {
                "alerts": [
                    alert("TargetDown"),
                    alert("ResticBackupStale", status="resolved"),
                ]
            }
        )
        self.assertEqual("[cluster] 1 firing, 1 resolved: TargetDown, ResticBackupStale", subject)

    def test_many_alert_names_are_truncated_in_the_subject(self) -> None:
        subject, body = mailrelay.summarize(
            {"alerts": [alert("A"), alert("B"), alert("C"), alert("D")]}
        )
        self.assertEqual("[cluster] 4 firing: A, B (+2)", subject)
        # The body still carries every alert: the subject is a preview, not the mail.
        for name in ("A", "B", "C", "D"):
            self.assertIn(f"* firing: {name}", body)

    def test_empty_batch_is_still_a_valid_mail(self) -> None:
        subject, body = mailrelay.summarize({"alerts": []})
        self.assertEqual("[cluster] no alerts: ", subject)
        self.assertIn("Alertmanager sent a batch with no alerts.", body)


class DeliveryTest(unittest.TestCase):
    def test_transport_failure_is_an_exception(self) -> None:
        # The URL does not resolve in the sandbox, which is the failure path a
        # 5xx from Cloudflare takes: Alertmanager retries instead of losing it.
        with self.assertRaises(Exception):
            mailrelay.send(
                "subject",
                "body",
                account_id="0" * 32,
                sender="alerts@send.example",
                recipient="ops@example",
                token="token",
                api_base="http://127.0.0.1:1/client/v4",
            )


if __name__ == "__main__":
    unittest.main()
