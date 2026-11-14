"""HTTP wrapper over :class:`data_plane.event_log.EventLog`.

The service is a single-writer front end: appends, replay reads, a long-polling
tail, and durable acknowledgement. Auth is one shared HMAC token header bound
to the method, the full request target, and the body digest; ``/healthz`` is
unauthenticated so a probe needs no secret.

The long poll waits in the request thread; ``ThreadingHTTPServer`` keeps the
other routes serving while it blocks. The tail does not acknowledge anything:
the consumer calls ``POST /events/ack`` once it has handled the event.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from data_plane.event_log import Event, EventLog

from . import common

TOKEN_HEADER = "x-event-token"
IDEMPOTENCY_HEADER = "x-idempotency-key"
MAX_TAIL_SECONDS = 30.0
MAX_READ_LIMIT = 1000


def _event(event: Event) -> dict[str, Any]:
    return {
        "position": event.position,
        "event_id": event.event_id,
        "epoch": event.epoch,
        "kind": event.kind,
        "entity": event.entity,
        "payload": event.payload,
        "created_at": event.created_at,
    }


@dataclass(frozen=True)
class EventService:
    """Routes for one ``EventLog`` database."""

    log: EventLog
    secret: bytes | str
    consumer_secrets: Mapping[str, bytes | str] | None = None
    max_body_bytes: int = common.MAX_BODY_BYTES

    @property
    def routes(self) -> common.Routes:
        return {
            ("POST", "/events"): self.append,
            ("GET", "/events"): self.read,
            ("GET", "/events/tail"): self.tail,
            ("POST", "/events/ack"): self.ack,
            ("GET", "/healthz"): self.health,
        }

    def _authorize(self, request: Any, body: bytes = b"") -> None:
        common.require_request_token(request, self.secret, TOKEN_HEADER, body)

    def append(self, request: Any, query: common.Query) -> tuple[int, Any]:
        body = common.read_body(request, self.max_body_bytes)
        self._authorize(request, body)
        data = common.parse_json_object(body)
        if "event_id" in data:
            # Event ids are server-assigned so a client cannot squat a position.
            raise common.HttpError(
                400, "validation", "event_id is server-assigned and must not be sent"
            )
        idempotency_key = common.field_optional_str(data, "idempotency_key") or (
            request.headers.get(IDEMPOTENCY_HEADER, "").strip() or None
        )
        if idempotency_key is None:
            raise common.HttpError(
                400, "validation", "an idempotency_key is required for an append"
            )
        payload = data.get("payload")
        if payload is not None and not isinstance(payload, dict):
            raise common.HttpError(400, "validation", "payload must be a JSON object")
        event = self.log.append(
            common.field_str(data, "kind"),
            common.field_str(data, "entity"),
            payload,
            idempotency_key=idempotency_key,
        )
        return 201, _event(event)

    def read(self, request: Any, query: common.Query) -> tuple[int, Any]:
        self._authorize(request)
        after = common.query_int(query, "after", default=0, minimum=0)
        limit = common.query_int(
            query, "limit", default=100, minimum=0, maximum=MAX_READ_LIMIT
        )
        events = self.log.read(after=int(after), limit=int(limit))
        return 200, {
            "epoch": self.log.epoch,
            "events": [_event(event) for event in events],
        }

    def tail(self, request: Any, query: common.Query) -> tuple[int, Any]:
        self._authorize(request)
        consumer = common.query_str(query, "consumer", required=True)
        timeout = common.query_float(
            query, "timeout", default=0.0, minimum=0.0, maximum=MAX_TAIL_SECONDS
        )
        subscription = self.log.subscribe(str(consumer), timeout=timeout)
        try:
            event = next(subscription)
        except StopIteration:
            return 204, None
        finally:
            subscription.close()
        return 200, {"event": _event(event)}

    def ack(self, request: Any, query: common.Query) -> tuple[int, Any]:
        body = common.read_body(request, self.max_body_bytes)
        data = common.parse_json_object(body)
        consumer = common.field_str(data, "consumer")
        position = common.field_int(data, "position")
        consumer_secret = (self.consumer_secrets or {}).get(consumer)
        if consumer_secret is None:
            raise common.HttpError(
                403,
                "forbidden",
                "no consumer credential is configured for this consumer",
            )
        # The ack MAC is computed with the consumer's own secret, so a caller
        # holding another consumer's credential cannot move this cursor.
        common.require_request_token(request, consumer_secret, TOKEN_HEADER, body)
        acknowledged = self.log.ack(consumer, position)
        return 200, {
            "consumer": consumer,
            "position": acknowledged,
            "epoch": self.log.epoch,
        }

    def health(self, request: Any, query: common.Query) -> tuple[int, Any]:
        # Unauthenticated probe: the epoch is state, not liveness, and is only
        # served to an authenticated caller.
        return 200, {"status": "ok", "service": "event"}
