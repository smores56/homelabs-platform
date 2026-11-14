"""Durable intent journal with an explicit, non-replayable state machine.

An intent records one proposed operation and the state it reached, so a crash or
a lost response can never silently replay an effect. Two rules carry the safety:
OUTCOME_UNKNOWN may not return to APPLYING (an effect that may have landed is
never re-applied blindly; only a recorded result resolves it), and APPLIED and
FAILED are terminal. The transitions are a closed table, not an if-chain, so a
caller cannot invent a shortcut by passing a state the journal never considered.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

from .errors import IdempotencyConflict, OutcomeUnknown, ValidationError
from .identity import canonical_json
from .model import IntentState

_BUSY_TIMEOUT_MS = 5000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS intents (
    intent_id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL,
    capability TEXT NOT NULL,
    state TEXT NOT NULL,
    operation_hash TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    base_revision TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    result JSON,
    error TEXT,
    operation JSON,
    requires_confirmation INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS intents_state_idx ON intents (state, expires_at);
"""


class TransitionError(ValidationError):
    """The journal does not allow that state change."""


class UnknownIntent(ValidationError):
    """No intent is stored under that id."""


ALLOWED_TRANSITIONS: Mapping[IntentState, frozenset[IntentState]] = {
    IntentState.PROPOSED: frozenset(
        {
            IntentState.AWAITING_CONFIRMATION,
            IntentState.APPROVED,
            IntentState.REJECTED,
            IntentState.SUPERSEDED,
            IntentState.EXPIRED,
            IntentState.APPLYING,
            IntentState.FAILED,
        }
    ),
    IntentState.AWAITING_CONFIRMATION: frozenset(
        {
            IntentState.APPROVED,
            IntentState.REJECTED,
            IntentState.SUPERSEDED,
            IntentState.EXPIRED,
        }
    ),
    IntentState.APPROVED: frozenset(
        {
            IntentState.APPLYING,
            IntentState.FAILED,
            IntentState.SUPERSEDED,
            IntentState.EXPIRED,
        }
    ),
    IntentState.APPLYING: frozenset(
        {IntentState.APPLIED, IntentState.FAILED, IntentState.OUTCOME_UNKNOWN}
    ),
    # No APPLYING here: an unknown outcome is reconciled, never replayed.
    IntentState.OUTCOME_UNKNOWN: frozenset({IntentState.APPLIED, IntentState.FAILED}),
    IntentState.APPLIED: frozenset(),
    IntentState.FAILED: frozenset(),
    IntentState.REJECTED: frozenset(),
    IntentState.EXPIRED: frozenset(),
    IntentState.SUPERSEDED: frozenset(),
}

TERMINAL_STATES = frozenset(
    {
        IntentState.APPLIED,
        IntentState.FAILED,
        IntentState.REJECTED,
        IntentState.EXPIRED,
        IntentState.SUPERSEDED,
    }
)

# Only intents that never started applying may expire; an in-flight or unknown
# effect needs reconciliation, not a timeout that hides it.
EXPIRING_STATES = frozenset(
    {
        IntentState.PROPOSED,
        IntentState.AWAITING_CONFIRMATION,
        IntentState.APPROVED,
    }
)


def _require_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{field} must be a non-empty string")
    return value


@dataclass(frozen=True)
class Intent:
    """One proposed operation and the state it reached."""

    intent_id: str
    source_id: str
    capability: str
    state: IntentState
    operation_hash: str
    idempotency_key: str
    base_revision: str | None
    created_at: float
    updated_at: float
    expires_at: float
    attempts: int
    result: dict[str, Any] | None
    error: str | None
    operation: dict[str, Any] | None = None
    requires_confirmation: bool = False

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    def overdue(self, now: float) -> bool:
        return self.state in EXPIRING_STATES and now >= self.expires_at


class IntentStore:
    """A SQLite-backed intent journal shared by every agent of one plane."""

    def __init__(
        self,
        path: str | os.PathLike[str],
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._path = str(path)
        self._clock = clock
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
        columns = {
            str(row[1])
            for row in self._connection.execute("PRAGMA table_info(intents)")
        }
        if "operation" not in columns:
            self._connection.execute("ALTER TABLE intents ADD COLUMN operation JSON")
        if "requires_confirmation" not in columns:
            self._connection.execute(
                "ALTER TABLE intents ADD COLUMN requires_confirmation INTEGER NOT NULL DEFAULT 0"
            )

    def __enter__(self) -> "IntentStore":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._connection.close()

    def propose(
        self,
        source_id: str,
        capability: str,
        operation_hash: str,
        idempotency_key: str,
        base_revision: str | None = None,
        ttl: float = 900,
    ) -> Intent:
        """Journal a new proposal, or return the one this key already proposed.

        Proposing twice with the same key and operation hash is idempotent; the
        same key with a different operation hash is a conflict, because the key
        promised that one operation happens once.
        """

        source_id = _require_text(source_id, "source_id")
        capability = _require_text(capability, "capability")
        operation_hash = _require_text(operation_hash, "operation_hash")
        idempotency_key = _require_text(idempotency_key, "idempotency_key")
        if ttl <= 0:
            raise ValidationError("ttl must be positive")
        if base_revision is not None:
            base_revision = _require_text(base_revision, "base_revision")

        with self._write() as db:
            row = db.execute(
                "SELECT * FROM intents WHERE idempotency_key = ?", (idempotency_key,)
            ).fetchone()
            if row is not None:
                existing = self._intent(row)
                if existing.operation_hash != operation_hash:
                    raise IdempotencyConflict(
                        f"idempotency key {idempotency_key!r} already proposed another"
                        " operation"
                    )
                return existing

            now = float(self._clock())
            intent = Intent(
                intent_id=f"intent-{uuid.uuid4().hex}",
                source_id=source_id,
                capability=capability,
                state=IntentState.PROPOSED,
                operation_hash=operation_hash,
                idempotency_key=idempotency_key,
                base_revision=base_revision,
                created_at=now,
                updated_at=now,
                expires_at=now + float(ttl),
                attempts=0,
                result=None,
                error=None,
            )
            db.execute(
                "INSERT INTO intents (intent_id, source_id, capability, state,"
                " operation_hash, idempotency_key, base_revision, created_at,"
                " updated_at, expires_at, attempts, result, error)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL)",
                (
                    intent.intent_id,
                    intent.source_id,
                    intent.capability,
                    intent.state.value,
                    intent.operation_hash,
                    intent.idempotency_key,
                    intent.base_revision,
                    intent.created_at,
                    intent.updated_at,
                    intent.expires_at,
                    intent.attempts,
                ),
            )
            return intent

    def get(self, intent_id: str) -> Intent:
        with self._lock:
            return self._load(self._connection, intent_id)

    def get_or_none(self, intent_id: str) -> Intent | None:
        """Return the intent or ``None`` instead of raising for an unknown id."""

        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM intents WHERE intent_id = ?", (intent_id,)
            ).fetchone()
        return None if row is None else self._intent(row)

    def create(self, intent: Intent) -> Intent:
        """Insert a fully formed intent (used when restoring or onboarding)."""

        intent_id = _require_text(intent.intent_id, "intent_id")
        idempotency_key = _require_text(intent.idempotency_key, "idempotency_key")
        with self._write() as db:
            clash = db.execute(
                "SELECT 1 FROM intents WHERE intent_id = ? OR idempotency_key = ?",
                (intent_id, idempotency_key),
            ).fetchone()
            if clash is not None:
                raise IdempotencyConflict(
                    f"intent {intent_id!r} or key {idempotency_key!r} already exists"
                )
            db.execute(
                "INSERT INTO intents (intent_id, source_id, capability, state,"
                " operation_hash, idempotency_key, base_revision, created_at,"
                " updated_at, expires_at, attempts, result, error, operation,"
                " requires_confirmation) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)",
                (
                    intent_id,
                    _require_text(intent.source_id, "source_id"),
                    _require_text(intent.capability, "capability"),
                    IntentState(intent.state).value,
                    _require_text(intent.operation_hash, "operation_hash"),
                    idempotency_key,
                    intent.base_revision,
                    float(intent.created_at),
                    float(intent.updated_at),
                    float(intent.expires_at),
                    int(intent.attempts),
                    self._encode(intent.result),
                    None if intent.operation is None else canonical_json(intent.operation).decode("utf-8"),
                    int(bool(intent.requires_confirmation)),
                ),
            )
            return self._load(db, intent_id)

    def update_state(
        self,
        intent_id: str,
        to_state: IntentState | str,
        result: Mapping[str, Any] | None = None,
        error: str | None = None,
        operation: Mapping[str, Any] | None = None,
        requires_confirmation: bool | None = None,
    ) -> Intent:
        """Transition under the table and optionally persist operation metadata."""

        target = self._state(to_state)
        body = self._encode(result)
        if error is not None:
            error = _require_text(error, "error")
        encoded_operation = (
            None if operation is None else canonical_json(dict(operation)).decode("utf-8")
        )
        with self._write() as db:
            intent = self._load(db, intent_id)
            self._check(intent, target, body)
            attempts = intent.attempts + (1 if target is IntentState.APPLYING else 0)
            db.execute(
                "UPDATE intents SET state = ?, attempts = ?, updated_at = ?,"
                " result = COALESCE(?, result), error = ?,"
                " operation = COALESCE(?, operation),"
                " requires_confirmation = COALESCE(?, requires_confirmation)"
                " WHERE intent_id = ?",
                (
                    target.value,
                    attempts,
                    float(self._clock()),
                    body,
                    error,
                    encoded_operation,
                    None if requires_confirmation is None else int(bool(requires_confirmation)),
                    intent_id,
                ),
            )
            return self._load(db, intent_id)

    def find(self, idempotency_key: str) -> Intent | None:
        """Return the intent proposed under ``idempotency_key``, if any."""

        idempotency_key = _require_text(idempotency_key, "idempotency_key")
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM intents WHERE idempotency_key = ?", (idempotency_key,)
            ).fetchone()
        return None if row is None else self._intent(row)

    def transition(
        self,
        intent_id: str,
        to_state: IntentState | str,
        result: Mapping[str, Any] | None = None,
        error: str | None = None,
    ) -> Intent:
        """Move an intent to ``to_state`` under the journal's transition table."""

        target = self._state(to_state)
        body = self._encode(result)
        if error is not None:
            error = _require_text(error, "error")

        with self._write() as db:
            intent = self._load(db, intent_id)
            self._check(intent, target, body)
            attempts = intent.attempts + (1 if target is IntentState.APPLYING else 0)
            db.execute(
                "UPDATE intents SET state = ?, attempts = ?, updated_at = ?,"
                " result = COALESCE(?, result), error = COALESCE(?, error)"
                " WHERE intent_id = ?",
                (
                    target.value,
                    attempts,
                    float(self._clock()),
                    body,
                    error,
                    intent_id,
                ),
            )
            return self._load(db, intent_id)

    def mark_applying(self, intent_id: str) -> Intent:
        return self.transition(intent_id, IntentState.APPLYING)

    def mark_applied(
        self, intent_id: str, result: Mapping[str, Any] | None = None
    ) -> Intent:
        return self.transition(intent_id, IntentState.APPLIED, result=result)

    def mark_failed(self, intent_id: str, error: str) -> Intent:
        return self.transition(intent_id, IntentState.FAILED, error=error)

    def mark_outcome_unknown(self, intent_id: str, error: str | None = None) -> Intent:
        return self.transition(
            intent_id, IntentState.OUTCOME_UNKNOWN, error=error
        )

    def resolve_unknown(
        self, intent_id: str, result: Mapping[str, Any] | None
    ) -> Intent:
        """Resolve an unknown outcome to APPLIED using an observed result.

        This is the only exit from OUTCOME_UNKNOWN that claims success, and it
        demands evidence: a reconciliation result, not a retry.
        """

        if result is None:
            raise ValidationError("resolving an unknown outcome requires a result")
        if self.get(intent_id).state is not IntentState.OUTCOME_UNKNOWN:
            raise TransitionError(
                f"intent {intent_id!r} has no unknown outcome to resolve"
            )
        return self.transition(intent_id, IntentState.APPLIED, result=result)

    def overdue(self, now: float | None = None) -> list[Intent]:
        """Return unstarted intents past their expiry, oldest first."""

        moment = float(self._clock()) if now is None else float(now)
        states = sorted(state.value for state in EXPIRING_STATES)
        placeholders = ", ".join("?" for _ in states)
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM intents WHERE state IN (" + placeholders + ")"
                " AND expires_at <= ? ORDER BY expires_at, intent_id",
                (*states, moment),
            ).fetchall()
        return [self._intent(row) for row in rows]

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

    def _check(
        self, intent: Intent, target: IntentState, result: str | None
    ) -> None:
        current = intent.state
        if current is IntentState.OUTCOME_UNKNOWN and target is IntentState.APPLYING:
            raise OutcomeUnknown(
                f"intent {intent.intent_id} has an unknown outcome; reconcile it"
                " instead of applying again"
            )
        if current in TERMINAL_STATES:
            raise TransitionError(
                f"intent {intent.intent_id} is {current.value} and terminal"
            )
        if target not in ALLOWED_TRANSITIONS[current]:
            raise TransitionError(
                f"intent {intent.intent_id} cannot move from {current.value}"
                f" to {target.value}"
            )
        if (
            current is IntentState.OUTCOME_UNKNOWN
            and target is IntentState.APPLIED
            and result is None
        ):
            raise TransitionError(
                f"intent {intent.intent_id} can only become applied with a result"
            )

    def _load(self, db: sqlite3.Connection, intent_id: str) -> Intent:
        row = db.execute(
            "SELECT * FROM intents WHERE intent_id = ?", (intent_id,)
        ).fetchone()
        if row is None:
            raise UnknownIntent(f"no intent {intent_id!r}")
        return self._intent(row)

    @staticmethod
    def _state(value: IntentState | str) -> IntentState:
        if isinstance(value, IntentState):
            return value
        try:
            return IntentState(value)
        except ValueError as exc:
            raise ValidationError(f"unknown intent state {value!r}") from exc

    @staticmethod
    def _encode(result: Mapping[str, Any] | None) -> str | None:
        if result is None:
            return None
        if not isinstance(result, Mapping):
            raise ValidationError("result must be a mapping")
        return canonical_json(dict(result)).decode("utf-8")

    @staticmethod
    def _intent(row: sqlite3.Row) -> Intent:
        raw = row["result"]
        return Intent(
            intent_id=str(row["intent_id"]),
            source_id=str(row["source_id"]),
            capability=str(row["capability"]),
            state=IntentState(str(row["state"])),
            operation_hash=str(row["operation_hash"]),
            idempotency_key=str(row["idempotency_key"]),
            base_revision=None
            if row["base_revision"] is None
            else str(row["base_revision"]),
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
            expires_at=float(row["expires_at"]),
            attempts=int(row["attempts"]),
            result=None if raw is None else json.loads(raw),
            error=None if row["error"] is None else str(row["error"]),
            operation=None
            if row["operation"] is None
            else json.loads(str(row["operation"])),
            requires_confirmation=bool(row["requires_confirmation"]),
        )
