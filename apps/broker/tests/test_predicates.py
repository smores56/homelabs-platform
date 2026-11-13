"""Per-host request predicates: method, path and body gates.

A denial must be a `Denied` carrying a stable, log-safe reason, raised before
the broker resolves the upstream or touches the network (SDLC #624).
"""

from __future__ import annotations

import json
import re
import unittest

from broker.config import BodyGuard, Enrollment, Injection
from broker.policy import Denied, check_request

HOOK_PATH = "/repos/smores56/kanbei/hooks"
HOOK_URL = "https://agent-hooks.sammohr.dev/kanbei"


def hook_guard() -> BodyGuard:
    return BodyGuard(
        methods=("POST",),
        paths=(re.compile(r"^/repos/[^/]+/[^/]+/hooks$"),),
        field="config.url",
        allow=re.compile(r"^https://agent-hooks\.sammohr\.dev/"),
    )


def guarded(**overrides: object) -> Enrollment:
    values: dict[str, object] = {
        "name": "hooks",
        "scheme": "https",
        "host": "api.github.com",
        "port": 443,
        "allow_private": False,
        "secret_name": "hooks-token",
        "injection": Injection(kind="bearer"),
        "allow_methods": ("POST",),
        "allow_paths": (re.compile(r"^/repos/[^/]+/[^/]+/hooks$"),),
        "body_guards": (hook_guard(),),
    }
    values.update(overrides)
    return Enrollment(**values)


def plain(**overrides: object) -> Enrollment:
    values: dict[str, object] = {
        "name": "plain",
        "scheme": "https",
        "host": "api.example.test",
        "port": 443,
        "allow_private": False,
        "secret_name": "plain-token",
        "injection": Injection(kind="bearer"),
    }
    values.update(overrides)
    return Enrollment(**values)


def body(url: str = HOOK_URL, **extra: object) -> bytes:
    document: dict[str, object] = {"config": {"url": url}}
    document.update(extra)
    return json.dumps(document).encode()


class MethodPredicateTest(unittest.TestCase):
    def test_allow_methods_admits_only_the_list(self) -> None:
        check_request(guarded(), "POST", HOOK_PATH, body())
        with self.assertRaises(Denied) as raised:
            check_request(guarded(), "GET", HOOK_PATH, b"")
        self.assertEqual(raised.exception.reason, "method_denied")
        self.assertEqual(raised.exception.status, 403)

    def test_deny_methods_always_deny(self) -> None:
        enrollment = plain(deny_methods=("DELETE", "OPTIONS"))
        check_request(enrollment, "GET", "/anything", b"")
        for method in ("DELETE", "OPTIONS"):
            with self.assertRaises(Denied) as raised:
                check_request(enrollment, method, "/anything", b"")
            self.assertEqual(raised.exception.reason, "method_denied")

    def test_no_method_predicates_admit_every_method(self) -> None:
        for method in ("GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"):
            check_request(plain(), method, "/anything", b"")


class PathPredicateTest(unittest.TestCase):
    def test_allow_paths_is_an_anchored_allowlist(self) -> None:
        with self.assertRaises(Denied) as raised:
            check_request(guarded(), "POST", "/repos/smores56/kanbei/issues", body())
        self.assertEqual(raised.exception.reason, "path_denied")
        self.assertEqual(raised.exception.status, 403)

    def test_deny_paths_refuses_matching_paths_only(self) -> None:
        enrollment = plain(deny_paths=(re.compile(r"^/admin(/|$)"),))
        check_request(enrollment, "GET", "/v1/models", b"")
        with self.assertRaises(Denied) as raised:
            check_request(enrollment, "GET", "/admin/secrets", b"")
        self.assertEqual(raised.exception.reason, "path_denied")

    def test_no_path_predicates_admit_every_path(self) -> None:
        check_request(plain(), "GET", "/anything/at/all", b"")


class BodyPredicateTest(unittest.TestCase):
    def test_deny_body_patterns_refuse_a_matching_body(self) -> None:
        enrollment = plain(deny_body_patterns=(re.compile(r'"public"\s*:\s*true'),))
        check_request(enrollment, "POST", "/v1/notify", b'{"public": false}')
        with self.assertRaises(Denied) as raised:
            check_request(enrollment, "POST", "/v1/notify", b'{"public":  true}')
        self.assertEqual(raised.exception.reason, "body_denied")
        self.assertEqual(raised.exception.status, 403)

    def test_matching_guard_requires_the_json_field_to_match(self) -> None:
        check_request(guarded(), "POST", HOOK_PATH, body())
        for url in (
            "https://evil.example/hook",
            "http://agent-hooks.sammohr.dev/kanbei",
            "https://agent-hooks.sammohr.dev.evil.example/kanbei",
        ):
            with self.assertRaises(Denied) as raised:
                check_request(guarded(), "POST", HOOK_PATH, body(url))
            self.assertEqual(raised.exception.reason, "body_field_denied", url)

    def test_malformed_json_is_a_denial(self) -> None:
        for raw in (
            b"",
            b"{not json",
            b"\xff\xfe",
            b'{"config": {"url": "https://agent-hooks.sammohr.dev/a", "url": "https://evil.example/"}',
        ):
            with self.assertRaises(Denied) as raised:
                check_request(guarded(), "POST", HOOK_PATH, raw)
            self.assertEqual(raised.exception.reason, "body_json_invalid", raw)

    def test_missing_or_non_string_field_is_a_denial(self) -> None:
        for raw in (
            b"{}",
            b"[]",
            b'"string"',
            b'{"config": {}}',
            b'{"config": {"url": 42}}',
            b'{"config": "url"}',
        ):
            with self.assertRaises(Denied) as raised:
                check_request(guarded(), "POST", HOOK_PATH, raw)
            self.assertEqual(raised.exception.reason, "body_field_denied", raw)

    def test_guard_applies_only_to_matching_methods_and_paths(self) -> None:
        enrollment = plain(body_guards=(hook_guard(),))
        check_request(enrollment, "GET", HOOK_PATH, b"not json")
        check_request(enrollment, "POST", "/repos/smores56/kanbei/issues", b"not json")

    def test_oversized_body_is_refused_with_413(self) -> None:
        enrollment = plain(max_body_bytes=16)
        check_request(enrollment, "POST", "/v1/notify", b"x" * 16)
        with self.assertRaises(Denied) as raised:
            check_request(enrollment, "POST", "/v1/notify", b"x" * 17)
        self.assertEqual(raised.exception.reason, "request_too_large")
        self.assertEqual(raised.exception.status, 413)

    def test_denial_reason_never_contains_the_body_or_the_url(self) -> None:
        secret = "token-shaped-value-0123456789"
        with self.assertRaises(Denied) as raised:
            check_request(guarded(), "POST", HOOK_PATH, body("https://evil.example/" + secret))
        self.assertNotIn(secret, raised.exception.reason)
        self.assertNotIn("evil.example", raised.exception.reason)


if __name__ == "__main__":
    unittest.main()
