"""Enrollment parsing tests, including a drift check against the shipped config."""

from __future__ import annotations

import json
import pathlib
import tempfile
import unittest

from broker.config import ConfigError, load_app_signing, load_config, load_secrets

KEY_PEM = "-----BEGIN PRIVATE KEY-----\nfake\n-----END PRIVATE KEY-----\n"

SHIPPED = pathlib.Path(__file__).resolve().parents[3] / "clusters/homelab/broker/config/broker.json"


class ShippedConfigTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config(SHIPPED, env={})

    def test_logical_hosts_are_the_contract(self) -> None:
        self.assertEqual(
            sorted(self.config.hosts),
            [
                "github-agent",
                "github-agent-push",
                "github-apps",
                "github-platform",
                "github-platform-push",
                "github-sdlc",
                "github-sdlc-git",
                "loki",
                "memory",
                "neuralwatt",
                "notify",
                "self",
                "status",
                "tinyfish",
            ],
        )

    def test_public_hosts_never_allow_private_addresses(self) -> None:
        for name in (
            "neuralwatt",
            "github-apps",
            "github-platform-push",
            "github-platform",
            "github-agent-push",
            "github-agent",
            "github-sdlc-git",
            "github-sdlc",
            "tinyfish",
        ):
            self.assertFalse(self.config.hosts[name].allow_private, name)

    def test_in_cluster_hosts_are_private_and_use_http(self) -> None:
        for name in ("loki", "memory", "self", "status"):
            enrollment = self.config.hosts[name]
            self.assertTrue(enrollment.allow_private, name)
            self.assertEqual(enrollment.scheme, "http", name)
        self.assertEqual(dict(self.config.hosts["loki"].headers)["X-Scope-OrgID"], "data")

    def test_credentialed_enrollments_name_secrets_or_an_app_scope_and_use_https(self) -> None:
        for name, enrollment in self.config.hosts.items():
            if enrollment.injection.kind == "none":
                self.assertEqual(enrollment.secret_name, "", name)
                self.assertIsNone(enrollment.app, name)
            else:
                # Public hosts must be TLS; the in-cluster gateway is the one
                # private exception and is asserted separately.
                self.assertTrue(enrollment.scheme == "https" or enrollment.allow_private, name)
                self.assertTrue(enrollment.secret_name or enrollment.app, name)

    def test_github_app_enrollments_carry_their_scope(self) -> None:
        self.assertIsNotNone(self.config.github_app)
        apps = self.config.hosts["github-apps"].app
        self.assertEqual(apps.repositories, ("homelabs-apps",))
        self.assertEqual(dict(apps.permissions), {"contents": "write"})
        platform = self.config.hosts["github-platform"].app
        self.assertEqual(dict(platform.permissions), {"contents": "write", "pull_requests": "write"})
        push = self.config.hosts["github-platform-push"].app
        self.assertEqual(dict(push.permissions), {"contents": "write"})
        self.assertEqual(push.repositories, ("homelabs-platform",))
        self.assertEqual(self.config.hosts["github-platform-push"].injection.kind, "basic")

    def test_agent_enrollments_cover_the_pr_repositories(self) -> None:
        # The agent's own repos: it may push agent/* branches and open pull
        # requests, and nothing else. The per-repo rulesets are the real gate.
        expected = ("nix-config", "kanbei")
        push = self.config.hosts["github-agent-push"]
        self.assertEqual(push.host, "github.com")
        self.assertEqual(push.injection.kind, "basic")
        self.assertEqual(push.injection.username, "x-access-token")
        self.assertEqual(push.app.repositories, expected)
        self.assertEqual(dict(push.app.permissions), {"contents": "write", "workflows": "write"})
        api = self.config.hosts["github-agent"]
        self.assertEqual(api.host, "api.github.com")
        self.assertEqual(api.injection.kind, "bearer")
        self.assertEqual(api.app.repositories, expected)
        self.assertEqual(dict(api.app.permissions), {"contents": "read", "pull_requests": "write"})

    def test_sdlc_hosts_scope_the_registry_repository(self) -> None:
        # SDLC #629: the work registry (smores56/sdlc) is a repository of its
        # own. Git transport and REST API are separate hosts, a token cannot
        # mint `metadata` without `read`, and issues are the only thing written.
        git = self.config.hosts["github-sdlc-git"]
        self.assertEqual(git.host, "github.com")
        self.assertEqual(git.scheme, "https")
        self.assertEqual(git.injection.kind, "basic")
        self.assertEqual(git.injection.username, "x-access-token")
        self.assertEqual(git.app.repositories, ("sdlc",))
        self.assertEqual(dict(git.app.permissions), {"contents": "write"})
        api = self.config.hosts["github-sdlc"]
        self.assertEqual(api.host, "api.github.com")
        self.assertEqual(api.scheme, "https")
        self.assertEqual(api.injection.kind, "bearer")
        self.assertEqual(api.app.repositories, ("sdlc",))
        self.assertEqual(dict(api.app.permissions), {"issues": "write", "metadata": "read"})

    def test_memory_is_a_private_http_host_with_an_optional_token(self) -> None:
        # SDLC #631: the graphiti wrapper. In-cluster HTTP with a bearer token
        # is the documented private exception. The token lives in the graphiti
        # Secret, so until step 32 projects it a missing file must be a 503 for
        # this host, never a startup abort.
        memory = self.config.hosts["memory"]
        self.assertEqual(memory.host, "memory-mcp.graphiti.svc.cluster.local")
        self.assertEqual(memory.port, 8182)
        self.assertEqual(memory.scheme, "http")
        self.assertTrue(memory.allow_private)
        self.assertEqual(memory.secret_name, "memory-mcp-token")
        self.assertEqual(memory.injection.kind, "bearer")
        self.assertTrue(memory.optional_secret)

    def test_only_the_unprovisioned_hosts_are_optional(self) -> None:
        # Fail-closed stays the default: an optional secret is a 503 per host,
        # so widening it to another enrollment needs this list to change.
        self.assertEqual(
            {name for name, enrollment in self.config.hosts.items() if enrollment.optional_secret},
            {"memory", "notify"},
        )

    def test_a_missing_memory_token_never_aborts_the_load(self) -> None:
        directory = tempfile.mkdtemp()
        required = {
            enrollment.secret_name
            for enrollment in self.config.hosts.values()
            if enrollment.secret_name and not enrollment.optional_secret
        }
        for name in required:
            pathlib.Path(directory, name).write_text("value\n")
        secrets = load_secrets(self.config.hosts, directory)
        self.assertNotIn("memory-mcp-token", secrets)

    def test_notify_is_a_public_placeholder_with_a_body_limit(self) -> None:
        # The provider is undecided (SDLC #640): the endpoint is a deliberate
        # `.invalid` placeholder, documented in docs/manual-steps.md step 24.
        notify = self.config.hosts["notify"]
        self.assertEqual(notify.host, "notify.invalid")
        self.assertEqual(notify.scheme, "https")
        self.assertFalse(notify.allow_private)
        self.assertEqual(notify.secret_name, "notify-token")
        self.assertTrue(notify.optional_secret, "the provider is undecided; a missing token must not crashloop the broker")
        self.assertEqual(notify.injection.kind, "bearer")
        self.assertEqual(notify.allow_methods, ("POST",))
        self.assertEqual(len(notify.allow_paths), 1)
        self.assertIsNotNone(notify.max_body_bytes)
        self.assertLessEqual(notify.max_body_bytes, 64 * 1024)

    def test_self_is_private_and_credential_free(self) -> None:
        agent_control = self.config.hosts["self"]
        self.assertEqual(agent_control.host, "agent-control.tenant-apps.svc.cluster.local")
        self.assertEqual(agent_control.port, 8081)
        self.assertEqual(agent_control.scheme, "http")
        self.assertTrue(agent_control.allow_private)
        self.assertEqual(agent_control.secret_name, "")
        self.assertEqual(agent_control.injection.kind, "none")

    def test_existing_hosts_carry_no_request_predicates(self) -> None:
        # Defaults must preserve the pre-#624 broker: an enrollment without
        # predicates forwards every method and path it always did.
        for name in (
            "neuralwatt",
            "github-apps",
            "github-platform-push",
            "github-platform",
            "github-agent-push",
            "github-agent",
            "github-sdlc-git",
            "github-sdlc",
            "tinyfish",
            "loki",
            "memory",
            "status",
        ):
            enrollment = self.config.hosts[name]
            self.assertEqual(enrollment.allow_methods, (), name)
            self.assertEqual(enrollment.deny_methods, (), name)
            self.assertEqual(enrollment.allow_paths, (), name)
            self.assertEqual(enrollment.deny_paths, (), name)
            self.assertEqual(enrollment.deny_body_patterns, (), name)
            self.assertEqual(enrollment.body_guards, (), name)
            self.assertIsNone(enrollment.max_body_bytes, name)

    def test_no_credential_value_in_the_config(self) -> None:
        raw = json.loads(SHIPPED.read_text())
        for enrollment in raw["logical_hosts"].values():
            self.assertNotIn("value", enrollment)
            if "secret" in enrollment:
                self.assertIn(
                    enrollment["secret"],
                    {
                        "neuralwatt-api-key",
                        "tinyfish-api-key",
                        "loki-gateway-token",
                        "notify-token",
                        "memory-mcp-token",
                    },
                )
            self.assertNotIn("private_key", enrollment)
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

    def test_optional_secret_needs_a_static_secret(self) -> None:
        with self.assertRaises(ConfigError):
            load_config(self.write(self.base(secret="", inject={"kind": "none"}, optional_secret=True)), env={})

    def test_credential_header_cannot_be_a_fixed_header(self) -> None:
        with self.assertRaises(ConfigError):
            load_config(self.write(self.base(headers={"Authorization": "Bearer x"})), env={})
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


class PredicateConfigTest(unittest.TestCase):
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

    def test_predicates_parse_and_compile(self) -> None:
        config = load_config(
            self.write(
                self.base(
                    allow_methods=["post", "GET"],
                    deny_methods=["delete"],
                    allow_paths=["^/repos/[^/]+/[^/]+/hooks$"],
                    deny_paths=["^/admin"],
                    deny_body_patterns=['"public"\\s*:\\s*true'],
                    max_body_bytes=4096,
                    body_guards=[
                        {
                            "methods": ["POST"],
                            "paths": ["^/repos/[^/]+/[^/]+/hooks$"],
                            "field": "config.url",
                            "allow": "^https://agent-hooks\\.sammohr\\.dev/",
                        }
                    ],
                )
            ),
            env={},
        )
        enrollment = config.hosts["example"]
        self.assertEqual(enrollment.allow_methods, ("POST", "GET"))
        self.assertEqual(enrollment.deny_methods, ("DELETE",))
        self.assertTrue(enrollment.allow_paths[0].fullmatch("/repos/a/b/hooks"))
        self.assertEqual(enrollment.max_body_bytes, 4096)
        guard = enrollment.body_guards[0]
        self.assertEqual(guard.methods, ("POST",))
        self.assertEqual(guard.field, "config.url")
        self.assertTrue(guard.allow.match("https://agent-hooks.sammohr.dev/x"))

    def test_predicates_default_to_empty(self) -> None:
        enrollment = load_config(self.write(self.base()), env={}).hosts["example"]
        self.assertEqual(enrollment.allow_methods, ())
        self.assertEqual(enrollment.deny_methods, ())
        self.assertEqual(enrollment.allow_paths, ())
        self.assertEqual(enrollment.deny_paths, ())
        self.assertEqual(enrollment.deny_body_patterns, ())
        self.assertEqual(enrollment.body_guards, ())
        self.assertIsNone(enrollment.max_body_bytes)

    def test_bad_predicates_are_refused_at_load(self) -> None:
        for overrides in (
            {"allow_methods": ["FETCH"]},
            {"deny_methods": ["POST", 1]},
            {"allow_methods": ["POST"], "deny_methods": ["POST"]},
            {"allow_methods": "POST"},
            {"allow_paths": ["("]},
            {"deny_paths": ["ok", None]},
            {"deny_body_patterns": [""]},
            {"deny_body_patterns": ["("]},
            {"max_body_bytes": 0},
            {"max_body_bytes": 64 * 1024 * 1024 + 1},
            {"max_body_bytes": True},
            {"max_body_bytes": "1024"},
            {"body_guards": [{"field": "config.url", "allow": "x"}]},
            {"body_guards": [{"paths": ["^/hooks$"], "allow": "x"}]},
            {"body_guards": [{"paths": ["^/hooks$"], "field": "config.url"}]},
            {"body_guards": [{"paths": ["^/hooks$"], "field": "", "allow": "x"}]},
            {"body_guards": [{"paths": ["^/hooks$"], "field": "a..b", "allow": "x"}]},
            {"body_guards": [{"paths": ["^/hooks$"], "field": "config.url", "allow": "("}]},
            {"body_guards": [{"paths": ["^/hooks$"], "field": "config.url", "allow": "x", "extra": 1}]},
            {"body_guards": [{"paths": ["^/hooks$"], "field": "config.url", "allow": "x", "methods": ["FETCH"]}]},
        ):
            with self.assertRaises(ConfigError, msg=repr(overrides)):
                load_config(self.write(self.base(**overrides)), env={})


class GithubAppEnrollmentTest(unittest.TestCase):
    def write(self, raw: object) -> str:
        directory = tempfile.mkdtemp()
        path = pathlib.Path(directory, "broker.json")
        path.write_text(json.dumps(raw))
        return str(path)

    def app_base(self, **overrides: object) -> dict:
        enrollment = {
            "scheme": "https",
            "host": "github.com",
            "port": 443,
            "github_app": {
                "installation": "example-installation-id",
                "repositories": ["homelabs-apps"],
                "permissions": {"contents": "write"},
            },
            "inject": {"kind": "basic", "username": "x-access-token"},
        }
        enrollment.update(overrides)
        return {
            "listen": "127.0.0.1:8080",
            "github_app": {},
            "logical_hosts": {"example": enrollment},
        }

    def test_app_enrollment_parses(self) -> None:
        config = load_config(self.write(self.app_base()), env={})
        enrollment = config.hosts["example"]
        self.assertIsNone(enrollment.secret_name or None)
        self.assertEqual(enrollment.app.installation, "example-installation-id")
        self.assertEqual(config.github_app.key_secret, "github-app-private-key")
        self.assertEqual(config.github_app.id_secret, "github-app-id")

    def test_scope_and_secret_are_mutually_exclusive(self) -> None:
        raw = self.app_base()
        raw["logical_hosts"]["example"]["secret"] = "example-token"
        with self.assertRaises(ConfigError):
            load_config(self.write(raw), env={})

    def test_an_app_enrollment_must_inject_a_credential(self) -> None:
        with self.assertRaises(ConfigError):
            load_config(self.write(self.app_base(inject={"kind": "none"})), env={})

    def test_an_app_enrollment_must_be_https(self) -> None:
        with self.assertRaises(ConfigError):
            load_config(self.write(self.app_base(scheme="http")), env={})

    def test_bad_ids_repos_and_permissions_are_refused(self) -> None:
        for scope in (
            {"installation": 0, "repositories": ["r"], "permissions": {"contents": "write"}},
            {"installation": 1, "repositories": [], "permissions": {"contents": "write"}},
            {"installation": 1, "repositories": ["r"], "permissions": {}},
            {"installation": 1, "repositories": ["r"], "permissions": {"contents": "sudo"}},
            {"installation": 1, "repositories": ["r"], "permissions": {"contents": "write"}, "extra": 1},
        ):
            with self.assertRaises(ConfigError):
                load_config(self.write(self.app_base(github_app=scope)), env={})

    def test_a_scope_without_a_top_level_app_is_refused(self) -> None:
        raw = self.app_base()
        del raw["github_app"]
        with self.assertRaises(ConfigError):
            load_config(self.write(raw), env={})

    def test_a_top_level_app_without_a_consumer_is_refused(self) -> None:
        raw = self.app_base()
        raw["logical_hosts"]["example"] = {
            "scheme": "https",
            "host": "api.example.test",
            "secret": "example-token",
            "inject": {"kind": "bearer"},
        }
        with self.assertRaises(ConfigError):
            load_config(self.write(raw), env={})


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

    def test_optional_secret_is_skipped_when_absent_or_empty(self) -> None:
        config = load_config(SHIPPED, env={})
        directory = tempfile.mkdtemp()
        for name in {enrollment.secret_name for enrollment in config.hosts.values() if enrollment.secret_name}:
            if name == "notify-token":
                continue
            pathlib.Path(directory, name).write_text("value\n")
        secrets = load_secrets(config.hosts, directory)
        self.assertNotIn("notify-token", secrets)
        pathlib.Path(directory, "notify-token").write_text("\n")
        self.assertNotIn("notify-token", load_secrets(config.hosts, directory))
        pathlib.Path(directory, "notify-token").write_text("token\n")
        self.assertEqual(load_secrets(config.hosts, directory)["notify-token"], "token")

    def test_the_app_key_is_required_when_an_app_is_enrolled(self) -> None:
        config = load_config(SHIPPED, env={})
        directory = tempfile.mkdtemp()
        for name in {enrollment.secret_name for enrollment in config.hosts.values() if enrollment.secret_name}:
            pathlib.Path(directory, name).write_text("value\n")
        with self.assertRaises(ConfigError):
            load_app_signing(config, directory)
        pathlib.Path(directory, "github-app-id").write_text("not-a-number")
        with self.assertRaises(ConfigError):
            load_app_signing(config, directory)
        pathlib.Path(directory, "github-app-id").write_text("4242\n")
        with self.assertRaises(ConfigError):
            load_app_signing(config, directory)
        pathlib.Path(directory, "github-app-private-key").write_text(KEY_PEM)
        with self.assertRaises(ConfigError):
            # every enrollment's installation id is still missing
            load_app_signing(config, directory)
        scopes = {
            enrollment.app.installation
            for enrollment in config.hosts.values()
            if enrollment.app is not None
        }
        for name in scopes:
            pathlib.Path(directory, name).write_text("99\n")
        signing = load_app_signing(config, directory)
        self.assertEqual(signing.app_id, 4242)
        self.assertEqual(signing.private_key, KEY_PEM.encode())
        self.assertEqual(set(signing.installations), scopes)
        self.assertTrue(all(value == 99 for value in signing.installations.values()))


if __name__ == "__main__":
    unittest.main()
