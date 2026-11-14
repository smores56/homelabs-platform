"""Wire tests for the read-only recovery portal service.

The service binds an ephemeral localhost port over a temporary generated portal
directory. No request leaves localhost, no token is needed, and no method can
mutate the directory.
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for candidate in (str(ROOT), str(ROOT / "libs")):
    if candidate not in sys.path:
        sys.path.insert(0, candidate)

from apps.data_plane import common, portal_service  # noqa: E402 - path setup above
from data_plane.backup.generations import (  # noqa: E402
    GenerationManifest,
    SnapshotRef,
)
from data_plane.recovery.portal import write_portal  # noqa: E402

DESTINATIONS = ("primary", "secondary")
DATASETS = ("content", "events")
SCHEMA_VERSION = 3


def manifest(**overrides) -> GenerationManifest:
    values = dict(
        version=1,
        generation_id="books-1000",
        group_id="books",
        created_at=1_000.0,
        datasets=(("content", "zfs-1"), ("events", "zfs-2")),
        sqlite=(("events", 2, 17),),
        content_hashes=(("/srv/books/a.epub", "ab" * 32),),
        destinations=(
            SnapshotRef(
                destination="primary",
                snapshot_id="snap-primary",
                created_at=1_000.0,
                group_id="books",
                dataset="data/books",
                verified=True,
            ),
            SnapshotRef(
                destination="secondary",
                snapshot_id="snap-secondary",
                created_at=1_000.0,
                group_id="books",
                dataset="data/books",
                verified=True,
            ),
        ),
        complete=True,
        schema_version=SCHEMA_VERSION,
    )
    values.update(overrides)
    return GenerationManifest(**values)


def header(headers: dict[str, str], name: str) -> str | None:
    wanted = name.lower()
    for key, value in headers.items():
        if key.lower() == wanted:
            return value
    return None


class Harness:
    """One portal service on an ephemeral localhost port."""

    def __init__(self, service: portal_service.PortalService) -> None:
        self.server = common.build_server(
            "portal", portal_service.handler_for(service), port=0
        )
        self.thread = threading.Thread(
            target=lambda: self.server.serve_forever(poll_interval=0.02),
            daemon=True,
        )

    def __enter__(self) -> "Harness":
        self.thread.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}{path}"

    def request(
        self, method: str, path: str
    ) -> tuple[int, dict[str, str], bytes]:
        request = urllib.request.Request(self.url(path), method=method)
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, dict(response.headers), response.read()
        except urllib.error.HTTPError as exc:
            try:
                raw = exc.read()
                headers = dict(exc.headers)
            finally:
                exc.close()
            return exc.code, headers, raw


class PortalServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name) / "portal"
        write_portal(
            self.directory,
            [manifest()],
            DESTINATIONS,
            DATASETS,
            SCHEMA_VERSION,
        )
        self.harness = Harness(portal_service.PortalService(self.directory))
        self.harness.__enter__()
        self.addCleanup(self.harness.__exit__, None, None, None)

    def test_root_serves_the_static_html_page_without_a_token(self):
        status, headers, body = self.harness.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertEqual(header(headers, "Content-Type"), "text/html; charset=utf-8")
        self.assertIn("public, max-age=60", header(headers, "Cache-Control") or "")
        self.assertEqual(header(headers, "X-Content-Type-Options"), "nosniff")
        self.assertIn(
            "default-src 'none'",
            header(headers, "Content-Security-Policy") or "",
        )
        text = body.decode("utf-8")
        self.assertIn("books", text)
        self.assertIn("books-1000", text)
        self.assertNotIn("<script", text)

    def test_index_json_matches_the_generated_portal(self):
        status, headers, body = self.harness.request("GET", "/index.json")
        self.assertEqual(status, 200)
        self.assertEqual(
            header(headers, "Content-Type"), "application/json; charset=utf-8"
        )
        self.assertIn("public, max-age=60", header(headers, "Cache-Control") or "")
        payload = json.loads(body)
        self.assertEqual(payload["schema_version"], SCHEMA_VERSION)
        self.assertEqual(payload["entries"][0]["generation_id"], "books-1000")

    def test_index_html_is_also_served(self):
        status, _, body = self.harness.request("GET", "/index.html")
        self.assertEqual(status, 200)
        self.assertIn(b"Recovery portal", body)

    def test_healthz_needs_no_auth_and_reports_entry_count(self):
        status, headers, body = self.harness.request("GET", "/healthz")
        self.assertEqual(status, 200)
        self.assertEqual(header(headers, "Cache-Control"), "no-store")
        self.assertEqual(
            json.loads(body),
            {"status": "ok", "service": "portal", "entries": 1},
        )

    def test_unknown_path_is_a_json_404(self):
        status, _, body = self.harness.request("GET", "/nope")
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(body)["error"]["code"], "not_found")

    def test_path_traversal_is_not_served(self):
        status, _, body = self.harness.request(
            "GET", "/index.json/../../etc/passwd"
        )
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(body)["error"]["code"], "not_found")

    def test_every_mutating_method_is_refused(self):
        before = {
            path.name: path.read_bytes() for path in self.directory.iterdir()
        }
        for method in ("POST", "PUT", "PATCH", "DELETE", "OPTIONS"):
            with self.subTest(method=method):
                status, headers, body = self.harness.request(method, "/")
                self.assertEqual(status, 405)
                self.assertEqual(
                    json.loads(body)["error"]["code"], "method_not_allowed"
                )
                self.assertEqual(header(headers, "Allow"), "GET, HEAD")
                self.assertEqual(
                    header(headers, "Cache-Control"), "no-store"
                )
        after = {path.name: path.read_bytes() for path in self.directory.iterdir()}
        self.assertEqual(after, before)

    def test_head_returns_headers_without_a_body(self):
        status, headers, body = self.harness.request("HEAD", "/")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"")
        self.assertGreater(int(header(headers, "Content-Length") or "0"), 0)

    def test_query_string_does_not_change_the_file(self):
        status, _, body = self.harness.request("GET", "/index.json?cache=0")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["entries"][0]["group_id"], "books")


class UnavailablePortalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.harness = Harness(
            portal_service.PortalService(Path(self.tmp.name) / "missing")
        )
        self.harness.__enter__()
        self.addCleanup(self.harness.__exit__, None, None, None)

    def test_page_and_index_are_404_until_generated(self):
        for path in ("/", "/index.json"):
            with self.subTest(path=path):
                status, headers, body = self.harness.request("GET", path)
                self.assertEqual(status, 404)
                self.assertEqual(header(headers, "Cache-Control"), "no-store")
                self.assertEqual(
                    json.loads(body)["error"]["code"], "not_found"
                )

    def test_healthz_is_unavailable_until_generated(self):
        status, _, body = self.harness.request("GET", "/healthz")
        self.assertEqual(status, 503)
        self.assertEqual(json.loads(body)["status"], "unavailable")

    def test_handler_factory_and_service_helpers_agree(self):
        service = portal_service.PortalService(self.tmp.name)
        self.assertIs(service.handler().portal_service, service)


class CacheConfigurationTests(unittest.TestCase):
    def test_cache_seconds_is_configurable(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / "portal"
            write_portal(
                directory, [manifest()], DESTINATIONS, DATASETS, SCHEMA_VERSION
            )
            with Harness(portal_service.PortalService(directory, cache_seconds=5)) as harness:
                status, headers, _ = harness.request("GET", "/")
            self.assertEqual(status, 200)
            self.assertEqual(header(headers, "Cache-Control"), "public, max-age=5")

    def test_negative_cache_seconds_is_rejected(self):
        with self.assertRaises(ValueError):
            portal_service.PortalService("/tmp/portal", cache_seconds=-1)


if __name__ == "__main__":
    unittest.main()
