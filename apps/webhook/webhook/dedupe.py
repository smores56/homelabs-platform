"""Bounded, time-limited set of delivery ids.

GitHub retries a delivery until it gets a 2xx, and allows a manual redelivery,
so an id seen once must not enqueue twice. Memory is bounded because the hook
path is public: the oldest id is evicted first, and an entry expires after the
window even if it was never repeated.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Callable
from threading import Lock

DEFAULT_CAPACITY = 4096
DEFAULT_TTL_S = 6 * 60 * 60.0


class DedupeCache:
    def __init__(
        self,
        capacity: int = DEFAULT_CAPACITY,
        ttl: float = DEFAULT_TTL_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if capacity < 1:
            raise ValueError("capacity must be positive")
        if ttl <= 0:
            raise ValueError("ttl must be positive")
        self._capacity = capacity
        self._ttl = ttl
        self._clock = clock
        self._entries: OrderedDict[str, float] = OrderedDict()
        # The HTTP server is threaded; check-and-set must be one critical
        # section or two concurrent duplicates both enqueue.
        self._lock = Lock()

    def claim(self, key: str) -> bool:
        """Record `key` and return True, or return False if it is still live."""
        with self._lock:
            now = self._clock()
            self._prune(now)
            if key in self._entries:
                return False
            self._entries[key] = now + self._ttl
            while len(self._entries) > self._capacity:
                self._entries.popitem(last=False)
            return True

    def release(self, key: str) -> None:
        """Forget a claim so a failed enqueue can be retried by the sender."""
        with self._lock:
            self._entries.pop(key, None)

    def _prune(self, now: float) -> None:
        while self._entries:
            key, expires = next(iter(self._entries.items()))
            if expires > now:
                return
            del self._entries[key]

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)
