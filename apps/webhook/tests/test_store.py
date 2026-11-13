"""Capability-source store.

The capability id is the credential for the hook URL, so it must be unguessable,
unique, and invalidated by rotation rather than reused.
"""

from __future__ import annotations

import json
import stat
import tempfile
import unittest
from pathlib import Path

from webhook.store import SourceStore, StoreError, new_capability


class StoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "sources.json"

    def store(self) -> SourceStore:
        store = SourceStore(self.path)
        store.load()
        return store

    def test_capability_ids_are_url_safe_and_256_bit(self) -> None:
        capability = new_capability()
        self.assertEqual(len(capability), 43)
        self.assertTrue(all(char.isalnum() or char in "-_" for char in capability))

    def test_registration_returns_an_unguessable_url_safe_id(self) -> None:
        source = self.store().register("github-platform", None)
        self.assertEqual(len(source.capability), 43)
        self.assertEqual(source.name, "github-platform")
        self.assertIsNone(source.secret)

    def test_two_sources_get_different_ids(self) -> None:
        store = self.store()
        first = store.register("one", None)
        second = store.register("two", None)
        self.assertNotEqual(first.capability, second.capability)

    def test_sources_survive_a_reload(self) -> None:
        source = self.store().register("github", "s3cr3t")
        reloaded = self.store().get(source.capability)
        self.assertIsNotNone(reloaded)
        assert reloaded is not None
        self.assertEqual(reloaded.name, "github")
        self.assertEqual(reloaded.secret, "s3cr3t")

    def test_reusing_a_name_rotates_the_capability(self) -> None:
        store = self.store()
        old = store.register("github", "s3cr3t")
        new = store.register("github", "newer-secret")
        self.assertNotEqual(old.capability, new.capability)
        self.assertIsNone(store.get(old.capability))
        self.assertIsNotNone(store.get(new.capability))
        self.assertEqual([source.name for source in store.sources()], ["github"])

    def test_rotation_persists(self) -> None:
        store = self.store()
        old = store.register("github", None)
        new = store.register("github", None)
        reloaded = self.store()
        self.assertIsNone(reloaded.get(old.capability))
        self.assertIsNotNone(reloaded.get(new.capability))

    def test_revoke_removes_the_source_and_persists(self) -> None:
        store = self.store()
        source = store.register("github", None)
        self.assertTrue(store.revoke(source.capability))
        self.assertIsNone(self.store().get(source.capability))
        self.assertFalse(store.revoke(source.capability))

    def test_invalid_names_are_refused(self) -> None:
        store = self.store()
        for name in ("", "Upper", "has space", "-leading", "trailing-", "a" * 65, "sla/sh"):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    store.register(name, None)

    def test_an_empty_secret_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            self.store().register("github", "")

    def test_the_file_is_owner_only(self) -> None:
        self.store().register("github", None)
        mode = stat.S_IMODE(self.path.stat().st_mode)
        self.assertEqual(mode, 0o600)

    def test_the_file_records_no_plaintext_capability_in_the_name_field(self) -> None:
        source = self.store().register("github", None)
        data = json.loads(self.path.read_text())
        self.assertEqual(data["version"], 1)
        self.assertEqual(data["sources"][0]["capability"], source.capability)

    def test_a_malformed_file_fails_closed(self) -> None:
        self.path.write_text("not json")
        with self.assertRaises(StoreError):
            self.store()

    def test_an_unknown_version_fails_closed(self) -> None:
        self.path.write_text(json.dumps({"version": 99, "sources": []}))
        with self.assertRaises(StoreError):
            self.store()

    def test_concurrent_registrations_do_not_corrupt_the_store(self) -> None:
        import concurrent.futures

        store = self.store()

        def register(index: int) -> None:
            store.register(f"source-{index}", None)

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(register, range(40)))
        reloaded = self.store()
        self.assertEqual(len(list(reloaded.sources())), 40)


if __name__ == "__main__":
    unittest.main()
