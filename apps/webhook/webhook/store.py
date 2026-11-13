"""Capability sources, persisted as one owner-only JSON file on the PVC.

A source is a name plus the capability id that forms the unguessable hook URL,
and an optional HMAC secret. Registering an existing name replaces its id, which
is the rotation primitive: the old URL stops resolving in the same write.

The file is written atomically (temp file + `os.replace`) so a crash never
leaves half a store, and it is mode 0600 because it holds HMAC secrets. Only
this pod mounts the claim, and no other namespace can reach the volume.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

VERSION = 1
CAPABILITY_BYTES = 32
MAX_NAME = 64
MAX_SECRET = 256
_NAME = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")
FILE_MODE = 0o600


class StoreError(Exception):
    """The persisted store is unreadable or from an unknown version."""


@dataclass(frozen=True)
class Source:
    capability: str
    name: str
    secret: str | None
    created: float


def new_capability() -> str:
    """256 random bits in URL-safe base64: the hook URL is the credential."""
    return secrets.token_urlsafe(CAPABILITY_BYTES)


class SourceStore:
    def __init__(
        self,
        path: Path,
        *,
        clock: Callable[[], float] = time.time,
        capability_factory: Callable[[], str] = new_capability,
    ) -> None:
        self._path = path
        self._clock = clock
        self._new_capability = capability_factory
        self._sources: dict[str, Source] = {}
        # ThreadingHTTPServer handles requests concurrently: a register and a
        # save must not interleave, and iteration must not race a mutation.
        self._lock = threading.Lock()

    def load(self) -> None:
        try:
            raw = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return
        except OSError as exc:
            raise StoreError(f"cannot read {self._path} ({exc.strerror})") from exc
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise StoreError(f"{self._path} is not valid JSON") from exc
        if not isinstance(data, dict) or data.get("version") != VERSION:
            raise StoreError(f"{self._path} is not a version {VERSION} store")
        sources = data.get("sources")
        if not isinstance(sources, list):
            raise StoreError(f"{self._path} has no source list")
        loaded: dict[str, Source] = {}
        for entry in sources:
            if not isinstance(entry, dict):
                raise StoreError(f"{self._path} holds a non-object source")
            capability = entry.get("capability")
            name = entry.get("name")
            secret = entry.get("secret")
            created = entry.get("created")
            if (
                not isinstance(capability, str)
                or not isinstance(name, str)
                or (secret is not None and not isinstance(secret, str))
                or not isinstance(created, (int, float))
            ):
                raise StoreError(f"{self._path} holds a malformed source")
            if not _NAME.match(name) or not capability:
                raise StoreError(f"{self._path} holds an invalid source")
            loaded[capability] = Source(capability, name, secret, float(created))
        self._sources = loaded

    def register(self, name: str, secret: str | None) -> Source:
        """Create a source, replacing every source with the same name."""
        if not _NAME.match(name):
            raise ValueError(f"invalid source name: {name!r}")
        if secret is not None and (not secret or len(secret) > MAX_SECRET):
            raise ValueError("secret must be 1..256 characters")
        with self._lock:
            self._sources = {
                key: source for key, source in self._sources.items() if source.name != name
            }
            capability = self._new_capability()
            while capability in self._sources:
                capability = self._new_capability()
            source = Source(capability, name, secret, self._clock())
            self._sources[capability] = source
            self._save()
            return source

    def get(self, capability: str) -> Source | None:
        with self._lock:
            return self._sources.get(capability)

    def revoke(self, capability: str) -> bool:
        with self._lock:
            if self._sources.pop(capability, None) is None:
                return False
            self._save()
            return True

    def sources(self) -> Iterable[Source]:
        with self._lock:
            return tuple(self._sources.values())

    def _save(self) -> None:
        data = {
            "version": VERSION,
            "sources": [
                {
                    "capability": source.capability,
                    "name": source.name,
                    "secret": source.secret,
                    "created": source.created,
                }
                for source in sorted(self._sources.values(), key=lambda item: item.created)
            ],
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_name(self._path.name + ".tmp")
        # Write then rename: a crash leaves the previous store intact.
        with open(temporary, "w", encoding="utf-8") as handle:
            os.chmod(handle.name, FILE_MODE)
            json.dump(data, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self._path)
        os.chmod(self._path, FILE_MODE)
