"""Target-integrity tests: address policy, DNS pinning, redirects, request shape."""

from __future__ import annotations

import socket
import unittest

from broker.policy import Denied, address_allowed, check_redirect, resolve_pinned, split_target


def resolver_for(*addresses: str):
    def resolver(host: str, port: int, *, type: int = socket.SOCK_STREAM) -> list[tuple]:
        result = []
        for address in addresses:
            if ":" in address:
                result.append((socket.AF_INET6, type, 6, "", (address, port, 0, 0)))
            else:
                result.append((socket.AF_INET, type, 6, "", (address, port)))
        return result

    return resolver


class AddressPolicyTest(unittest.TestCase):
    def test_public_address_allowed(self) -> None:
        for address in ("140.82.112.5", "2606:50c0:8000::153"):
            self.assertTrue(address_allowed(address, allow_private=False), address)

    def test_always_denied(self) -> None:
        for address in (
            "10.42.0.10",  # pod CIDR
            "127.0.0.1",
            "169.254.169.254",  # cloud metadata
            "100.64.0.1",  # CGNAT
            "0.0.0.0",
            "224.0.0.1",
            "255.255.255.255",
            "::1",
            "fe80::1",
            "ff02::1",
            "::",
        ):
            self.assertFalse(address_allowed(address, allow_private=True), address)

    def test_private_denied_unless_enrolled(self) -> None:
        for address in ("10.0.0.5", "172.16.4.4", "192.168.1.2", "10.43.0.10", "fd00::5"):
            self.assertFalse(address_allowed(address, allow_private=False), address)
            self.assertTrue(address_allowed(address, allow_private=True), address)

    def test_service_cidr_is_private_only(self) -> None:
        self.assertFalse(address_allowed("10.43.0.10", allow_private=False))
        self.assertTrue(address_allowed("10.43.0.10", allow_private=True))
        self.assertFalse(address_allowed("10.42.0.10", allow_private=True))

    def test_node_address_denied_even_when_public(self) -> None:
        self.assertFalse(address_allowed("203.0.113.9", allow_private=True, deny_addresses=["203.0.113.9"]))

    def test_ipv4_mapped_private_address_denied(self) -> None:
        self.assertFalse(address_allowed("::ffff:10.0.0.5", allow_private=False))
        self.assertTrue(address_allowed("::ffff:10.0.0.5", allow_private=True))
        self.assertFalse(address_allowed("::ffff:10.42.0.9", allow_private=True))
        self.assertTrue(address_allowed("::ffff:140.82.1.1", allow_private=False))

    def test_non_global_denied(self) -> None:
        for address in ("192.0.2.10", "198.51.100.10", "2001:db8::1"):
            self.assertFalse(address_allowed(address, allow_private=True), address)


class ResolveTest(unittest.TestCase):
    def test_pins_first_allowed_address(self) -> None:
        pinned = resolve_pinned("api.example.test", 443, allow_private=False, resolver=resolver_for("140.82.1.1", "140.82.1.2"))
        self.assertEqual(str(pinned.ip), "140.82.1.1")
        self.assertEqual(pinned.family, socket.AF_INET)

    def test_any_refused_address_denies_the_whole_answer(self) -> None:
        with self.assertRaises(Denied) as raised:
            resolve_pinned("api.example.test", 443, allow_private=False, resolver=resolver_for("140.82.1.1", "10.0.0.5"))
        self.assertIn("address_refused", raised.exception.reason)

    def test_private_answer_needs_private_enrollment(self) -> None:
        pinned = resolve_pinned("loki.observability.svc", 3100, allow_private=True, resolver=resolver_for("10.43.0.10"))
        self.assertEqual(str(pinned.ip), "10.43.0.10")
        with self.assertRaises(Denied):
            resolve_pinned("loki.observability.svc", 3100, allow_private=False, resolver=resolver_for("10.43.0.10"))

    def test_dns_failure_and_empty_are_denials(self) -> None:
        def failing(host: str, port: int, *, type: int = socket.SOCK_STREAM) -> list[tuple]:
            raise socket.gaierror("no such host")

        with self.assertRaises(Denied) as raised:
            resolve_pinned("missing.test", 443, allow_private=False, resolver=failing)
        self.assertEqual(raised.exception.status, 502)
        with self.assertRaises(Denied):
            resolve_pinned("empty.test", 443, allow_private=False, resolver=resolver_for())

    def test_loopback_names_never_resolve(self) -> None:
        with self.assertRaises(Denied) as raised:
            resolve_pinned("localhost", 443, allow_private=True, resolver=resolver_for("10.43.0.10"))
        self.assertEqual(raised.exception.reason, "loopback_name")


class RedirectTest(unittest.TestCase):
    def details(self, **overrides: object) -> dict:
        values = {"scheme": "https", "host": "api.example.test", "port": 443}
        values.update(overrides)
        return values

    def test_relative_and_same_host_pass(self) -> None:
        check_redirect("/next", **self.details())
        check_redirect("https://API.example.test/next", **self.details())
        check_redirect("https://api.example.test:443/next", **self.details())

    def test_cross_host_and_port_denied(self) -> None:
        for location in (
            "https://evil.example/",
            "https://api.example.test.evil.example/",
            "https://api.example.test:8443/",
            "http://api.example.test/next",
            "ftp://api.example.test/next",
            "https://api.example.test:not-a-port/next",
        ):
            with self.assertRaises(Denied, msg=location):
                check_redirect(location, **self.details())


class SplitTargetTest(unittest.TestCase):
    def test_plain_and_nested_paths(self) -> None:
        self.assertEqual(split_target("/neuralwatt/v1/models", "broker.test"), ("neuralwatt", "/v1/models", ""))
        self.assertEqual(split_target("/tinyfish?query=hello", "broker.test"), ("tinyfish", "/", "query=hello"))
        self.assertEqual(split_target("/loki/loki/api/v1/query_range?limit=5", "broker.test"),
                         ("loki", "/loki/api/v1/query_range", "limit=5"))

    def test_dot_segments_are_canonicalized(self) -> None:
        self.assertEqual(split_target("/loki/a/../b", "broker.test")[1], "/b")
        self.assertEqual(split_target("/loki/a/./b/", "broker.test")[1], "/a/b/")

    def test_rejects_absolute_form_and_ambiguous_paths(self) -> None:
        for target in ("http://evil.example/x", "https://evil.example/x", "//evil.example/x", "loki/x"):
            with self.assertRaises(Denied, msg=target):
                split_target(target, "broker.test")

    def test_rejects_ip_literal_host(self) -> None:
        for host in ("10.43.0.1", "127.0.0.1:8080", "[::1]:8080", "::1"):
            with self.assertRaises(Denied, msg=host) as raised:
                split_target("/loki/x", host)
            self.assertEqual(raised.exception.status, 400)

    def test_rejects_encoded_nul_and_backslash(self) -> None:
        for target in ("/loki/a%00b", "/loki/a%5Cb"):
            with self.assertRaises(Denied, msg=target):
                split_target(target, "broker.test")

    def test_requires_host(self) -> None:
        with self.assertRaises(Denied):
            split_target("/loki/x", "")


if __name__ == "__main__":
    unittest.main()
