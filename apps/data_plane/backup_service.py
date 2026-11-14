"""HTTP wrapper over :class:`data_plane.backup.controller.FanoutController`.

There is no restic here and no destination that touches the network. The
entrypoint wires the controller to a fixture snapshotter and in-memory or
directory metadata destinations, so ``POST /run`` exercises the fence,
checkpoint, snapshot-once, independent-capture, verify, and publish contract
without executing a backup tool.

``POST /run`` takes an optional ``{"group": ..., "destinations": [...]}``; a
group or destination not configured at startup is a validation error.
``GET /healthz`` reports the configured destinations.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from data_plane.backup.controller import FanoutController

from . import common


@dataclass(frozen=True)
class BackupService:
    """Routes for one configured fan-out controller."""

    controller: FanoutController
    destinations: tuple[str, ...]
    groups: tuple[str, ...] = ()

    @property
    def routes(self) -> common.Routes:
        return {
            ("POST", "/run"): self.run,
            ("GET", "/healthz"): self.health,
        }

    def run(self, request: Any, query: common.Query) -> tuple[int, Any]:
        empty = request.headers.get("Content-Length", "").strip() in ("", "0")
        data = {} if empty else common.read_json_object(request)
        group = common.field_optional_str(data, "group")
        if group is None:
            if len(self.groups) != 1:
                raise common.HttpError(
                    400,
                    "validation",
                    "group is required when more than one group is configured",
                )
            group = self.groups[0]
        if self.groups and group not in self.groups:
            raise common.HttpError(400, "validation", f"unknown group {group!r}")
        destinations = common.field_str_list(data, "destinations") or list(
            self.destinations
        )
        if not destinations:
            raise common.HttpError(400, "validation", "no destinations configured")
        unknown = sorted(set(destinations) - set(self.destinations))
        if unknown:
            raise common.HttpError(
                400, "validation", f"unknown destinations: {', '.join(unknown)}"
            )
        manifest = self.controller.run(group, destinations)
        return 200, manifest.to_dict()

    def health(self, request: Any, query: common.Query) -> tuple[int, Any]:
        return 200, {
            "status": "ok",
            "service": "backup",
            "destinations": list(self.destinations),
            "groups": list(self.groups),
        }
