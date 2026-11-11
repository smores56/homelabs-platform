"""Unit tests for the data-tenant Loki gateway.

The gateway is the only path to the `data` tenant, so the three properties that
matter are tested directly: the token decides access, only read paths pass, and
the tenant header is forced regardless of what the client sent.
"""

from __future__ import annotations

import importlib.util
import pathlib
import unittest

SOURCE = (
    pathlib.Path(__file__).resolve().parents[2]
    / "clusters/homelab/observability/loki-gateway/gateway.py"
)
spec = importlib.util.spec_from_file_location("loki_gateway", SOURCE)
assert spec and spec.loader
gateway = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gateway)

TOKEN = "minted-by-eso-0123456789"


class AuthorizationTest(unittest.TestCase):
    def test_correct_token(self) -> None:
        self.assertTrue(gateway.is_authorized({"Authorization": f"Bearer {TOKEN}"}, TOKEN))

    def test_wrong_token(self) -> None:
        self.assertFalse(gateway.is_authorized({"Authorization": "Bearer nope"}, TOKEN))

    def test_missing_header(self) -> None:
        self.assertFalse(gateway.is_authorized({}, TOKEN))

    def test_missing_server_token_denies_everything(self) -> None:
        self.assertFalse(gateway.is_authorized({"Authorization": "Bearer anything"}, ""))

    def test_bare_bearer_is_not_a_token(self) -> None:
        self.assertFalse(gateway.is_authorized({"Authorization": "Bearer"}, TOKEN))

    def test_lowercase_scheme_is_refused(self) -> None:
        self.assertFalse(gateway.is_authorized({"Authorization": f"bearer {TOKEN}"}, TOKEN))

    def test_surrounding_whitespace_is_trimmed(self) -> None:
        self.assertTrue(gateway.is_authorized({"Authorization": f"Bearer  {TOKEN}  "}, TOKEN))


class PathTest(unittest.TestCase):
    def test_query_range_get_and_post(self) -> None:
        self.assertTrue(gateway.is_allowed("GET", "/loki/api/v1/query_range?query=%7B%7D"))
        self.assertTrue(gateway.is_allowed("POST", "/loki/api/v1/query_range"))

    def test_labels_and_series(self) -> None:
        self.assertTrue(gateway.is_allowed("GET", "/loki/api/v1/labels"))
        self.assertTrue(gateway.is_allowed("GET", "/loki/api/v1/label/namespace/values"))
        self.assertTrue(gateway.is_allowed("GET", "/loki/api/v1/series"))

    def test_push_is_denied(self) -> None:
        self.assertFalse(gateway.is_allowed("POST", "/loki/api/v1/push"))

    def test_delete_and_flush_are_denied(self) -> None:
        self.assertFalse(gateway.is_allowed("POST", "/loki/api/v1/delete"))
        self.assertFalse(gateway.is_allowed("POST", "/flush"))

    def test_path_traversal_is_denied(self) -> None:
        self.assertFalse(gateway.is_allowed("POST", "/loki/api/v1/query_range/../../push"))

    def test_writes_with_read_method_on_unknown_path_are_denied(self) -> None:
        self.assertFalse(gateway.is_allowed("GET", "/loki/api/v1/status/buildinfo"))


class HeaderTest(unittest.TestCase):
    def test_tenant_is_forced_over_a_client_header(self) -> None:
        headers = gateway.upstream_headers({"X-Scope-OrgID": "raw"})
        self.assertEqual("data", headers["X-Scope-OrgID"])

    def test_client_credentials_are_stripped(self) -> None:
        headers = gateway.upstream_headers(
            {"Authorization": f"Bearer {TOKEN}", "Cookie": "session=1", "X-Forwarded-For": "10.0.0.1"}
        )
        self.assertNotIn("Authorization", headers)
        self.assertNotIn("Cookie", headers)
        self.assertNotIn("X-Forwarded-For", headers)

    def test_content_type_is_forwarded(self) -> None:
        headers = gateway.upstream_headers({"Content-Type": "application/json"})
        self.assertEqual("application/json", headers["Content-Type"])


if __name__ == "__main__":
    unittest.main()
