"""Adapter from the broker's intent port to the single SQLite journal.

The broker speaks a small port (``get``/``find``/``save``) so it can be tested
without a database. Production uses this adapter: the durable rows, transition
table, and epoch-aware cursors all live in :mod:`data_plane.intents`.
"""

from __future__ import annotations

from .broker import Intent as BrokerIntent
from .confirmation import NormalizedOperation, operation_hash
from .errors import ValidationError
from .intents import Intent as JournalIntent
from .intents import IntentStore


class BrokerJournal:
    """Persist broker intents, including the full operation, in one journal."""

    def __init__(self, store: IntentStore) -> None:
        self._store = store

    def find(self, idempotency_key: str) -> BrokerIntent | None:
        row = self._store.find(idempotency_key)
        return None if row is None else self._convert(row)

    def get(self, intent_id: str) -> BrokerIntent | None:
        row = self._store.get_or_none(intent_id)
        return None if row is None else self._convert(row)

    def save(self, intent: BrokerIntent) -> None:
        if self._store.get_or_none(intent.intent_id) is None:
            self._store.create(self._to_journal(intent))
            return
        self._store.update_state(
            intent.intent_id,
            intent.state,
            result=intent.effect,
            error=intent.error,
            operation=intent.operation.normalized(),
            requires_confirmation=intent.requires_confirmation,
        )

    @staticmethod
    def _to_journal(intent: BrokerIntent) -> JournalIntent:
        now = float(intent.updated_at)
        return JournalIntent(
            intent_id=intent.intent_id,
            source_id=intent.operation.source_id,
            capability=intent.operation.capability,
            state=intent.state,
            operation_hash=operation_hash(intent.operation),
            idempotency_key=intent.idempotency_key,
            base_revision=intent.operation.base_revision,
            created_at=float(intent.created_at),
            updated_at=now,
            expires_at=now + 900.0,
            attempts=0,
            result=intent.effect,
            error=intent.error,
            operation=intent.operation.normalized(),
            requires_confirmation=intent.requires_confirmation,
        )

    @staticmethod
    def _convert(row: JournalIntent) -> BrokerIntent:
        if row.operation is None:
            raise ValidationError(
                f"intent {row.intent_id!r} has no stored operation; it predates the"
                " broker journal"
            )
        return BrokerIntent(
            intent_id=row.intent_id,
            state=row.state,
            operation=NormalizedOperation.from_normalized(row.operation),
            idempotency_key=row.idempotency_key,
            requires_confirmation=row.requires_confirmation,
            created_at=int(row.created_at),
            updated_at=int(row.updated_at),
            effect=row.result,
            error=row.error,
        )
