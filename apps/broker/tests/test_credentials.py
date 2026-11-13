"""Credential resolution and the fail-closed broker path."""

from __future__ import annotations

import io
import unittest

from broker.config import AppScope, Config, Enrollment, Injection
from broker.credentials import AppCredentials, StaticCredentials
from broker.github_app import CredentialUnavailable
from broker.server import Broker


class FakeTokens:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def token(self, installation, repositories, permissions):
        self.calls.append((installation, repositories, permissions))
        return "ghs_minted"

    def active_tokens(self):
        return ["ghs_minted"]


def app_enrollment(installation="github-installation-id"):
    return Enrollment(
        name="gh",
        scheme="https",
        host="github.com",
        port=443,
        allow_private=False,
        secret_name="",
        injection=Injection(kind="basic", username="x-access-token"),
        app=AppScope(
            installation=installation,
            repositories=("homelabs-platform",),
            permissions=(("contents", "write"),),
        ),
    )


class StaticTests(unittest.TestCase):
    def test_a_static_enrollment_reads_its_file(self):
        enrollment = Enrollment(
            name="neuralwatt",
            scheme="https",
            host="api.example.test",
            port=443,
            allow_private=False,
            secret_name="neuralwatt-api-key",
            injection=Injection(kind="bearer"),
        )
        credentials = StaticCredentials({"neuralwatt-api-key": "sk-test"})
        self.assertEqual(credentials.value(enrollment), "sk-test")
        self.assertEqual(credentials.values(), {"neuralwatt-api-key": "sk-test"})


class AppTests(unittest.TestCase):
    def test_the_installation_file_name_resolves_to_the_id(self):
        tokens = FakeTokens()
        credentials = AppCredentials(
            {},
            tokens,
            {"github-installation-id": 181438296},
        )
        self.assertEqual(credentials.value(app_enrollment()), "ghs_minted")
        self.assertEqual(
            tokens.calls,
            [(181438296, ("homelabs-platform",), {"contents": "write"})],
        )

    def test_minted_tokens_join_the_redaction_set(self):
        credentials = AppCredentials({}, FakeTokens(), {"github-installation-id": 1})
        credentials.value(app_enrollment())
        self.assertIn("ghs_minted", credentials.values().values())

    def test_an_unknown_installation_is_a_credential_failure(self):
        credentials = AppCredentials({}, FakeTokens(), {})
        with self.assertRaises(CredentialUnavailable):
            credentials.value(app_enrollment())


class RaisingCredentials:
    def value(self, enrollment):
        raise CredentialUnavailable("github-app: installation 1 token request failed (422)")

    def values(self):
        return {}


class Handler:
    command = "GET"
    path = "/gh/repos/smores56/homelabs-platform/pulls"
    headers = {"Host": "broker"}

    def __init__(self):
        self.wfile = io.BytesIO()
        self.rfile = io.BytesIO(b"")
        self.status = None

    def send_response_only(self, status):
        self.status = status

    def send_header(self, name, value):
        return

    def end_headers(self):
        return


class FailClosedTests(unittest.TestCase):
    def test_a_minting_failure_is_a_503_and_never_a_forward(self):
        config = Config(
            listen_host="127.0.0.1",
            listen_port=8080,
            deny_addresses=(),
            hosts={"gh": app_enrollment()},
        )
        resolver = lambda *args, **kwargs: [  # noqa: E731
            (2, 1, 6, "", ("93.184.216.34", 443))
        ]
        broker = Broker(config=config, credentials=RaisingCredentials(), resolver=resolver)
        handler = Handler()

        broker.handle(handler)

        self.assertEqual(handler.status, 503)
        self.assertIn(b"credential_unavailable", handler.wfile.getvalue())


if __name__ == "__main__":
    unittest.main()


class OptionalSecretTests(unittest.TestCase):
    def test_an_unprovisioned_optional_secret_is_a_503_and_never_a_forward(self):
        # SDLC #640: the notify provider is undecided, so the enrollment ships
        # with optional_secret and must not take the broker down at startup.
        config = Config(
            listen_host="127.0.0.1",
            listen_port=8080,
            deny_addresses=(),
            hosts={
                "notify": Enrollment(
                    name="notify",
                    scheme="https",
                    host="notify.invalid",
                    port=443,
                    allow_private=False,
                    secret_name="notify-token",
                    injection=Injection(kind="bearer"),
                    optional_secret=True,
                )
            },
        )
        broker = Broker(
            config=config,
            credentials=StaticCredentials({}),
            resolver=lambda *args, **kwargs: [(2, 1, 6, "", ("93.184.216.34", 443))],
        )
        handler = Handler()
        handler.command = "POST"
        handler.path = "/notify/v1/notify"
        handler.headers = {"Host": "broker", "Content-Length": "0"}

        broker.handle(handler)

        self.assertEqual(handler.status, 503)
        self.assertIn(b"credential_unavailable", handler.wfile.getvalue())
