"""Delivery-id dedupe.

GitHub redelivers on a non-2xx answer and on manual "Redeliver", so the same id
arriving twice must enqueue once. The cache is bounded and time-limited so a
public endpoint cannot grow memory without bound.
"""

from __future__ import annotations

import unittest

from webhook.dedupe import DedupeCache


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class DedupeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = Clock()
        self.cache = DedupeCache(capacity=3, ttl=60.0, clock=self.clock)

    def test_first_delivery_claims_the_id(self) -> None:
        self.assertTrue(self.cache.claim("a"))

    def test_second_delivery_is_a_duplicate(self) -> None:
        self.assertTrue(self.cache.claim("a"))
        self.assertFalse(self.cache.claim("a"))

    def test_release_allows_a_retry_after_a_failed_enqueue(self) -> None:
        self.assertTrue(self.cache.claim("a"))
        self.cache.release("a")
        self.assertTrue(self.cache.claim("a"))

    def test_release_of_an_unknown_id_is_harmless(self) -> None:
        self.cache.release("never-seen")

    def test_an_id_expires_after_the_ttl(self) -> None:
        self.assertTrue(self.cache.claim("a"))
        self.clock.now += 61.0
        self.assertTrue(self.cache.claim("a"))

    def test_capacity_evicts_the_oldest_id(self) -> None:
        for key in ("a", "b", "c"):
            self.assertTrue(self.cache.claim(key))
        self.assertTrue(self.cache.claim("d"))
        self.assertEqual(len(self.cache), 3)
        self.assertTrue(self.cache.claim("a"))

    def test_a_repeat_does_not_extend_the_window(self) -> None:
        self.assertTrue(self.cache.claim("a"))
        self.assertFalse(self.cache.claim("a"))
        self.clock.now += 61.0
        self.assertTrue(self.cache.claim("a"))

    def test_concurrent_claims_admit_exactly_one_delivery(self) -> None:
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
            results = list(pool.map(lambda _: self.cache.claim("same"), range(64)))
        self.assertEqual(results.count(True), 1)


if __name__ == "__main__":
    unittest.main()
