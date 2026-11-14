"""HTTP wrapper over :class:`data_plane.broker.Broker`.

Identity is a signed workload token in ``X-Workload-Token``: the service only
verifies the claims the identity library already issues and scopes. It never
mints a workload token and never mints an operator confirmation token.

The confirmation surface is external and operator-controlled. A destructive or
cross-domain write leaves the intent in ``awaiting_confirmation``; an operator
reviews the rendered operation outside this service and returns the token in
the request body. Without that token ``POST /apply`` refuses the write with
``403 confirmation_required``. This service has no route that issues one.

``POST /reconcile`` settles an ``outcome_unknown`` intent by reading the
adapter. It is authenticated as a workload action but carries no confirmation
token: observing an outcome is not a second effect.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from data_plane.broker import Broker, Intent, WriteRequest
from data_plane.confirmation import FieldChange
from data_plane.identity import IdentityIssuer

from . import common

WORKLOAD_TOKEN_HEADER = "x-workload-token"


def _intent(intent: Intent) -> dict[str, Any]:
    return {
        "intent_id": intent.intent_id,
        "state": intent.state.value,
        "idempotency_key": intent.idempotency_key,
        "requires_confirmation": intent.requires_confirmation,
        "created_at": intent.created_at,
        "updated_at": intent.updated_at,
        "effect": intent.effect,
        "error": intent.error,
        "operation": intent.operation.normalized(),
    }


def _field_diff(body: dict[str, Any]) -> tuple[FieldChange, ...]:
    raw = body.get("field_diff", [])
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise common.HttpError(400, "validation", "field_diff must be a list")
    changes: list[FieldChange] = []
    for item in raw:
        if not isinstance(item, list) or len(item) != 3:
            raise common.HttpError(
                400, "validation", "field_diff entries must be [field, before, after]"
            )
        field, before, after = item
        if not isinstance(field, str) or not field.strip():
            raise common.HttpError(
                400, "validation", "field_diff field must be a non-empty string"
            )
        changes.append(FieldChange(field=field, before=before, after=after))
    return tuple(changes)


def write_request(body: dict[str, Any]) -> WriteRequest:
    """Rebuild a broker ``WriteRequest`` from the wire form."""

    targets = common.field_str_list(body, "targets")
    if not targets:
        raise common.HttpError(400, "validation", "targets must not be empty")
    return WriteRequest(
        source_id=common.field_str(body, "source_id"),
        capability=common.field_str(body, "capability"),
        targets=tuple(targets),
        idempotency_key=common.field_str(body, "idempotency_key"),
        field_diff=_field_diff(body),
        base_revision=common.field_optional_str(body, "base_revision"),
        recurrence=common.field_optional_str(body, "recurrence"),
        attendee_scope=tuple(common.field_str_list(body, "attendee_scope")),
        destination=common.field_optional_str(body, "destination"),
        epoch=common.field_optional_int(body, "epoch"),
    )


@dataclass(frozen=True)
class BrokerService:
    """Routes for one configured broker and its identity issuer."""

    broker: Broker
    issuer: IdentityIssuer

    @property
    def routes(self) -> common.Routes:
        return {
            ("POST", "/propose"): self.propose,
            ("POST", "/confirm"): self.confirm,
            ("POST", "/apply"): self.apply,
            ("POST", "/reconcile"): self.reconcile,
            ("GET", "/healthz"): self.health,
        }

    def _claims(self, request: Any) -> Any:
        token = request.headers.get(WORKLOAD_TOKEN_HEADER, "").strip()
        if not token:
            raise common.HttpError(401, "unauthorized", "missing workload token")
        return self.issuer.verify(token)

    def propose(self, request: Any, query: common.Query) -> tuple[int, Any]:
        claims = self._claims(request)
        data = common.read_json_object(request)
        intent, operation = self.broker.propose(claims, write_request(data))
        return 201, {"intent": _intent(intent), "operation": operation.normalized()}

    def confirm(self, request: Any, query: common.Query) -> tuple[int, Any]:
        claims = self._claims(request)
        data = common.read_json_object(request)
        # The token comes from the external operator surface; this service
        # never issues it.
        token = common.field_str(data, "confirmation_token")
        intent = self.broker.confirm(claims, write_request(data), token)
        return 200, {"intent": _intent(intent)}

    def apply(self, request: Any, query: common.Query) -> tuple[int, Any]:
        claims = self._claims(request)
        data = common.read_json_object(request)
        token = common.field_optional_str(data, "confirmation_token")
        effect = self.broker.apply(claims, write_request(data), token)
        return 200, {"effect": effect}

    def reconcile(self, request: Any, query: common.Query) -> tuple[int, Any]:
        self._claims(request)
        data = common.read_json_object(request)
        intent = self.broker.reconcile(common.field_str(data, "intent_id"))
        return 200, {"intent": _intent(intent)}

    def health(self, request: Any, query: common.Query) -> tuple[int, Any]:
        return 200, {"status": "ok", "service": "broker"}
