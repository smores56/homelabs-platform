"""Enrollment parsing tests, including a drift check against the shipped config."""

from __future__ import annotations

import json
import pathlib
import tempfile
import unittest

from broker.config import ConfigError, load_config, load_secrets

SHIPPED = pathlib.Path(__file__).resolve().parents[3] / "clusters/homelab/broker/config/broker.json"


class ShippedConfigTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config(SHIPPED, env={})

    def test_logical_hosts_are_the_contract(self) -> None:
        self.assertEqual(
            sorted(self.config.hosts),
            ["github-apps", "github-fork", "github-platform", "loki", "neuralwatt", "status", "tinyfish"],
        )

    def test_public_hosts_never_allow_private_addresses(self) -> None:
        for name in ("neuralwatt", "github-apps", "github-fork", "github-platform", "tinyfish"):
            self.assertFalse(self.config.hosts[name].allow_private, name)

    def test_in_cluster_hosts_are_private_and_use_http(self) -> None:
        for name in ("loki", "status"):
            enrollment = self.config.hosts[name]
            self.assertTrue(enrollment.allow_private, name)
            self.assertEqual(enrollment.scheme, "http", name)
        self.assertEqual(dict(self.config.hosts["loki"].headers)["X-Scope-OrgID"], "data")

    def test_credentialed_enrollments_name_secrets_and_use_https(self) -> None:
        for name, enrollment in self.config.hosts.items():
            if enrollment.injection.kind == "none":
                self.assertEqual(enrollment.secret_name, "", name)
            else:
                # Public hosts must be TLS; the in-cluster gateway is the one
                # private exception and is asserted separately.
                self.assertTrue(enrollment.scheme == "https" or enrollment.allow_private, name)
                self.assertTrue(enrollment.secret_name, name)

    def test_no_credential_value_in_the_config(self) -> None:
        raw = json.loads(SHIPPED.read_text())
        for enrollment in raw["logical_hosts"].values():
            self.assertNotIn("value", enrollment)
            if "secret" in enrollment:
                self.assertIn(
                    enrollment["secret"],
                    {"neuralwatt-api-key", "github-apps-token", "github-platform-token", "tinyfish-api-key", "loki-gateway-token"},
                )
        self.assertNotRegex(SHIPPED.read_text(), r"[A-Za-z0-9_-]{32,}")


class ConfigValidationTest(unittest.TestCase):
    def write(self, raw: object) -> str:
        directory = tempfile.mkdtemp()
        path = pathlib.Path(directory, "broker.json")
        path.write_text(json.dumps(raw))
        return str(path)

    def base(self, **overrides: object) -> dict:
        enrollment = {
            "scheme": "https",
            "host": "api.example.test",
            "port": 443,
            "secret": "example-token",
            "inject": {"kind": "bearer"},
        }
        enrollment.update(overrides)
        return {"listen": "127.0.0.1:8080", "logical_hosts": {"example": enrollment}}

    def test_minimal_valid(self) -> None:
        config = load_config(self.write(self.base()), env={})
        self.assertEqual(config.listen_port, 8080)
        self.assertEqual(config.hosts["example"].injection.kind, "bearer")

    def test_ip_literal_host_refused(self) -> None:
        with self.assertRaises(ConfigError):
            load_config(self.write(self.base(host="10.43.0.1")), env={})

    def test_credential_requires_secret_and_https(self) -> None:
        with self.assertRaises(ConfigError):
            load_config(self.write(self.base(secret="")), env={})
        with self.assertRaises(ConfigError):
            load_config(self.write(self.base(scheme="http")), env={})

    def test_no_injection_must_not_name_a_secret(self) -> None:
        with self.assertRaises(ConfigError):
            load_config(self.write(self.base(inject={"kind": "none"})), env={})

    def test_unknown_kind_and_bad_port(self) -> None:
        with self.assertRaises(ConfigError):
            load_config(self.write(self.base(inject={"kind": "oauth"})), env={})
        with self.assertRaises(ConfigError):
            load_config(self.write(self.base(port=70000)), env={})

    def test_credential_header_cannot_be_a_fixed_header(self) -> None:
        with self.assertRaises(ConfigError):
            load_config(self.write(self.base(headers={"Authorization": "Bearer x"})), env={})

    def test_header_values_cannot_contain_newlines(self) -> None:
        with self.assertRaises(ConfigError):
            load_config(self.write(self.base(headers={"X-Tenant": "a\r\nX-Evil: 1"})), env={})

    def test_env_listen_and_deny_addresses(self) -> None:
        config = load_config(
            self.write(self.base()),
            env={"BROKER_LISTEN": "0.0.0.0:9000", "BROKER_DENY_ADDRESSES": "203.0.113.4, 203.0.113.5"},
        )
        self.assertEqual(config.listen_host, "0.0.0.0")
        self.assertEqual(config.listen_port, 9000)
        self.assertEqual(len(config.deny_addresses), 2)


class SecretsTest(unittest.TestCase):
    def test_reads_and_strips(self) -> None:
        config = load_config(SHIPPED, env={})
        directory = tempfile.mkdtemp()
        for name in {enrollment.secret_name for enrollment in config.hosts.values() if enrollment.secret_name}:
            pathlib.Path(directory, name).write_text(f"{name}-value\n")
        secrets = load_secrets(config.hosts, directory)
        self.assertEqual(secrets["neuralwatt-api-key"], "neuralwatt-api-key-value")

    def test_missing_or_empty_secret_is_fatal(self) -> None:
        config = load_config(SHIPPED, env={})
        directory = tempfile.mkdtemp()
        with self.assertRaises(ConfigError):
            load_secrets(config.hosts, directory)
        pathlib.Path(directory, "neuralwatt-api-key").write_text("\n")
        with self.assertRaises(ConfigError):
            load_secrets(config.hosts, directory)


if __name__ == "__main__":
    unittest.main()
