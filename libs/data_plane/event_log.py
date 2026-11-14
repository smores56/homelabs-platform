"""Append-only SQLite event log for the local and external data planes.

The log is what a consumer replays: a monotonic position per event, an epoch
that invalidates every consumer when the source is rebuilt, a durable cursor per
consumer, and an effect ledger keyed by operation. One writer per database file
keeps SQLite's single-writer contract explicit; WAL lets readers keep reading
while a write commits. Long-polling reads wait for work without holding a
transaction open, so a crashed consumer simply resumes from its cursor.
"""

from __future__ import annotations

import json
import os
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping

from .errors import (
    CursorError,
    EpochError,
    IdempotencyConflict,
    ValidationError,
)
from .identity import canonical_json

_BUSY_TIMEOUT_MS = 5000
_SCHEMA_VERSION = "1"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    position INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    epoch INTEGER NOT NULL,
    kind TEXT NOT NULL,
    entity TEXT NOT NULL,
    payload JSON NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cursors (
    consumer TEXT PRIMARY KEY,
    position INTEGER NOT NULL,
    epoch INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS effects (
    key TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    result JSON,
    created_at TEXT NOT NULL
);
"""


def _utc(timestamp: float) -> str:
    return (
        datetime.fromtimestamp(timestamp, timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _require_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{field} must be a non-empty string")
    return value


def _require_position(value: Any, field: str = "position") -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError(f"{field} must be an integer")
    if value < 0:
        raise ValidationError(f"{field} must not be negative")
    return value


def _encode(result: Mapping[str, Any] | None) -> str | None:
    if result is None:
        return None
    if not isinstance(result, Mapping):
        raise ValidationError("result must be a mapping")
    return canonical_json(dict(result)).decode("utf-8")


def _decode(raw: str | None) -> dict[str, Any] | None:
    return None if raw is None else json.loads(raw)


def _idempotency_meta_key(idempotency_key: str) -> str:
    return f"idem:{idempotency_key}"


@dataclass(frozen=True)
class Event:
    """One immutable event at a monotonic position in the log."""

    position: int
    event_id: str
    epoch: int
    kind: str
    entity: str
    payload: dict[str, Any]
    created_at: str


@dataclass(frozen=True)
class Effect:
    """The recorded outcome of one side effect, keyed by operation."""

    key: str
    status: str
    result: dict[str, Any] | None
    created_at: str


class EventLog:
    """A single-writer, multi-reader event log backed by SQLite."""

    def __init__(
        self, path: str | os.PathLike[str], epoch: int | None = None
    ) -> None:
        self._path = str(path)
        self._lock = threading.RLock()
        self._closed = False
        if self._path != ":memory:":
            Path(self._path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(
            self._path, check_same_thread=False, isolation_level=None
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=NORMAL")
        self._connection.executescript(_SCHEMA)
        with self._write() as db:
            self._initialize_epoch(db, epoch)

    def __enter__(self) -> "EventLog":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._connection.close()

    @property
    def epoch(self) -> int:
        with self._lock:
            return self._stored_epoch(self._connection)

    def append(
        self,
        kind: str,
        entity: str,
        payload: Mapping[str, Any] | None = None,
        event_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> Event:
        """Append one event, or return the event an earlier identical write made.

        An idempotency key (or an explicit event id) is a promise that the
        operation happens at most once: replaying it with the same kind, entity,
        and payload returns the stored event, and replaying it with anything
        else raises IdempotencyConflict instead of appending a second effect.
        """

        kind = _require_text(kind, "kind")
        entity = _require_text(entity, "entity")
        if payload is not None and not isinstance(payload, Mapping):
            raise ValidationError("payload must be a mapping")
        body = {} if payload is None else dict(payload)
        serialized = canonical_json(body).decode("utf-8")
        if event_id is not None:
            event_id = _require_text(event_id, "event_id")
        if idempotency_key is not None:
            idempotency_key = _require_text(idempotency_key, "idempotency_key")

        with self._write() as db:
            existing = self._replay(db, idempotency_key, event_id)
            if existing is not None:
                if (
                    existing.kind != kind
                    or existing.entity != entity
                    or canonical_json(existing.payload).decode("utf-8") != serialized
                ):
                    raise IdempotencyConflict(
                        f"event {existing.event_id} already records a different operation"
                    )
                return existing

            if event_id is None:
                event_id = f"evt-{secrets.token_hex(16)}"
            created_at = _utc(time.time())
            cursor = db.execute(
                "INSERT INTO events (event_id, epoch, kind, entity, payload, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    event_id,
                    self._stored_epoch(db),
                    kind,
                    entity,
                    serialized,
                    created_at,
                ),
            )
            position = int(cursor.lastrowid)
            if idempotency_key is not None:
                db.execute(
                    "INSERT INTO meta (key, value) VALUES (?, ?)",
                    (_idempotency_meta_key(idempotency_key), event_id),
                )
            return Event(
                position=position,
                event_id=event_id,
                epoch=self._stored_epoch(db),
                kind=kind,
                entity=entity,
                payload=body,
                created_at=created_at,
            )

    def read(self, after: int = 0, limit: int = 100) -> list[Event]:
        """Return events ordered by position, strictly after ``after``."""

        after = _require_position(after, "after")
        limit = _require_position(limit, "limit")
        if limit == 0:
            return []
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM events WHERE position > ? ORDER BY position LIMIT ?",
                (after, limit),
            ).fetchall()
        return [self._event(row) for row in rows]

    def latest(self) -> Event | None:
        """Return the most recently appended event, or None on an empty log."""

        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM events ORDER BY position DESC LIMIT 1"
            ).fetchone()
        return None if row is None else self._event(row)

    def cursor(self, consumer: str) -> tuple[int, int]:
        """Return the durable (position, epoch) for a consumer."""

        consumer = _require_text(consumer, "consumer")
        with self._lock:
            row = self._connection.execute(
                "SELECT position, epoch FROM cursors WHERE consumer = ?", (consumer,)
            ).fetchone()
            if row is None:
                return 0, self._stored_epoch(self._connection)
            return int(row["position"]), int(row["epoch"])

    def ack(self, consumer: str, position: int) -> int:
        """Advance a consumer's cursor to ``position`` and return it.

        The cursor is monotonic and never skips a gap. A cursor left over from
        another epoch raises EpochError: the consumer must reconcile before it
        may acknowledge anything again.
        """

        consumer = _require_text(consumer, "consumer")
        position = _require_position(position)
        with self._write() as db:
            epoch = self._stored_epoch(db)
            latest = self._latest_position(db)
            if position > latest:
                raise CursorError(
                    f"cursor would skip a gap: latest position is {latest}"
                )
            row = db.execute(
                "SELECT position, epoch FROM cursors WHERE consumer = ?", (consumer,)
            ).fetchone()
            if row is not None:
                if int(row["epoch"]) != epoch:
                    raise EpochError(
                        f"consumer {consumer!r} cursor is at epoch {int(row['epoch'])},"
                        f" log is at epoch {epoch}; reconcile and reset the cursor"
                    )
                if position < int(row["position"]):
                    raise CursorError(
                        f"cursor would move backwards from {int(row['position'])}"
                    )
            db.execute(
                "INSERT INTO cursors (consumer, position, epoch, updated_at)"
                " VALUES (?, ?, ?, ?)"
                " ON CONFLICT(consumer) DO UPDATE SET"
                " position = excluded.position,"
                " epoch = excluded.epoch,"
                " updated_at = excluded.updated_at",
                (consumer, position, epoch, _utc(time.time())),
            )
        return position

    def reset_cursor(self, consumer: str, position: int = 0) -> None:
        """Rewind a cursor to the current epoch after a rebuild.

        This is the only way past EpochError, and it is deliberately explicit:
        the consumer states where reconciliation resumed.
        """

        consumer = _require_text(consumer, "consumer")
        position = _require_position(position)
        with self._write() as db:
            latest = self._latest_position(db)
            if position > latest:
                raise CursorError(
                    f"cursor would skip a gap: latest position is {latest}"
                )
            db.execute(
                "INSERT INTO cursors (consumer, position, epoch, updated_at)"
                " VALUES (?, ?, ?, ?)"
                " ON CONFLICT(consumer) DO UPDATE SET"
                " position = excluded.position,"
                " epoch = excluded.epoch,"
                " updated_at = excluded.updated_at",
                (consumer, position, self._stored_epoch(db), _utc(time.time())),
            )

    def subscribe(
        self, consumer: str, timeout: float = 0.0, poll_interval: float = 0.05
    ) -> Iterator[Event]:
        """Yield events after the durable cursor, long-polling for ``timeout``.

        The cursor is read but never acknowledged here: the caller decides when
        an event has been durably handled.
        """

        consumer = _require_text(consumer, "consumer")
        if timeout < 0:
            raise ValidationError("timeout must not be negative")
        if poll_interval <= 0:
            raise ValidationError("poll_interval must be positive")
        position, cursor_epoch = self.cursor(consumer)
        current = self.epoch
        if cursor_epoch != current:
            raise EpochError(
                f"consumer {consumer!r} cursor is at epoch {cursor_epoch},"
                f" log is at epoch {current}; reconcile and reset the cursor"
            )
        deadline = time.monotonic() + timeout
        while True:
            events = self.read(after=position)
            if events:
                for event in events:
                    position = event.position
                    yield event
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(poll_interval, remaining))

    def rebuild(self, new_epoch: int) -> int:
        """Move the log to ``new_epoch`` so every consumer must reconcile."""

        new_epoch = _require_position(new_epoch, "new_epoch")
        with self._write() as db:
            current = self._stored_epoch(db)
            if new_epoch <= current:
                raise EpochError(
                    f"new epoch {new_epoch} must be greater than the log epoch {current}"
                )
            db.execute("UPDATE meta SET value = ? WHERE key = 'epoch'", (str(new_epoch),))
        return new_epoch

    def record_effect(
        self, key: str, status: str, result: Mapping[str, Any] | None = None
    ) -> Effect:
        """Record or update the outcome of one side effect."""

        key = _require_text(key, "effect key")
        status = _require_text(status, "status")
        serialized = _encode(result)
        with self._write() as db:
            db.execute(
                "INSERT INTO effects (key, status, result, created_at)"
                " VALUES (?, ?, ?, ?)"
                " ON CONFLICT(key) DO UPDATE SET"
                " status = excluded.status, result = excluded.result",
                (key, status, serialized, _utc(time.time())),
            )
            row = db.execute("SELECT * FROM effects WHERE key = ?", (key,)).fetchone()
        return self._effect(row)

    def effect(self, key: str) -> Effect | None:
        """Return the recorded effect for ``key``, if any."""

        key = _require_text(key, "effect key")
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM effects WHERE key = ?", (key,)
            ).fetchone()
        return None if row is None else self._effect(row)

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                yield self._connection
            except BaseException:
                self._connection.execute("ROLLBACK")
                raise
            self._connection.execute("COMMIT")

    def _initialize_epoch(
        self, db: sqlite3.Connection, epoch: int | None
    ) -> None:
        row = db.execute("SELECT value FROM meta WHERE key = 'epoch'").fetchone()
        if row is None:
            requested = 1 if epoch is None else _require_position(epoch, "epoch")
            if requested < 1:
                raise EpochError("epoch must be at least 1")
            db.execute(
                "INSERT INTO meta (key, value) VALUES ('epoch', ?)", (str(requested),)
            )
            db.execute(
                "INSERT INTO meta (key, value) VALUES ('schema_version', ?)",
                (_SCHEMA_VERSION,),
            )
            return
        stored = int(row["value"])
        if epoch is not None and int(epoch) != stored:
            raise EpochError(
                f"log is at epoch {stored}, not the requested epoch {epoch}"
            )

    def _stored_epoch(self, db: sqlite3.Connection) -> int:
        row = db.execute("SELECT value FROM meta WHERE key = 'epoch'").fetchone()
        return int(row["value"])

    def _latest_position(self, db: sqlite3.Connection) -> int:
        row = db.execute("SELECT COALESCE(MAX(position), 0) AS latest FROM events").fetchone()
        return int(row["latest"])

    def _replay(
        self,
        db: sqlite3.Connection,
        idempotency_key: str | None,
        event_id: str | None,
    ) -> Event | None:
        event_ids: list[str] = []
        if idempotency_key is not None:
            row = db.execute(
                "SELECT value FROM meta WHERE key = ?",
                (_idempotency_meta_key(idempotency_key),),
            ).fetchone()
            if row is not None:
                event_ids.append(str(row["value"]))
        if event_id is not None:
            row = db.execute(
                "SELECT event_id FROM events WHERE event_id = ?", (event_id,)
            ).fetchone()
            if row is not None:
                event_ids.append(str(row["event_id"]))
        unique = sorted(set(event_ids))
        if not unique:
            return None
        if len(unique) > 1:
            raise IdempotencyConflict(
                "idempotency key and event id already refer to different events"
            )
        row = db.execute(
            "SELECT * FROM events WHERE event_id = ?", (unique[0],)
        ).fetchone()
        return self._event(row)

    @staticmethod
    def _event(row: sqlite3.Row) -> Event:
        return Event(
            position=int(row["position"]),
            event_id=str(row["event_id"]),
            epoch=int(row["epoch"]),
            kind=str(row["kind"]),
            entity=str(row["entity"]),
            payload=json.loads(row["payload"]),
            created_at=str(row["created_at"]),
        )

    @staticmethod
    def _effect(row: sqlite3.Row) -> Effect:
        return Effect(
            key=str(row["key"]),
            status=str(row["status"]),
            result=_decode(row["result"]),
            created_at=str(row["created_at"]),
        )
