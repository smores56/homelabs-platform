"""Entrypoint: ``python -m apps.data_plane.run <service>``.

One process runs one service. Configuration comes from flags or the matching
environment variable; a missing DB path or secret file aborts startup instead
of serving a half-configured endpoint. Logs go to stderr so a container can
keep stdout for nothing else and a read-only root filesystem only needs the
database and manifest directories to be writable mounts.

Every backend is local and offline: SQLite files, a fixture snapshotter, and
in-memory destinations. Nothing here shells out to restic, dials a provider, or
reads a live source.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
import threading
import time
from pathlib import Path
from typing import Any, Iterable, Mapping

from data_plane.backup.controller import FanoutController
from data_plane.backup.destinations import InMemoryDestination
from data_plane.backup.generations import read_manifest
from data_plane.approval import (
    MAX_APPROVAL_TTL_SECONDS,
    SignedApprovalVerifier,
    UsedApprovalStore,
)
from data_plane.broker import Broker
from data_plane.broker_journal import BrokerJournal
from data_plane.collectors.relay import Relay
from data_plane.confirmation import ConfirmationGate, NormalizedOperation
from data_plane.errors import DataPlaneError
from data_plane.event_log import EventLog
from data_plane.identity import IdentityIssuer
from data_plane.intents import IntentStore
from data_plane.recovery.portal import write_portal
from data_plane.model import (
    Authority,
    Capability,
    EffectClass,
    FieldAuthority,
    FidelityLimit,
    SourceContract,
    Visibility,
)

from . import (
    backup_service,
    broker_service,
    collector_service,
    common,
    confirmation_service,
    event_service,
    portal_service,
    relay_service,
)

SERVICES = (
    "event",
    "broker",
    "relay",
    "backup",
    "portal",
    "confirmation",
    "collector",
)


class ConfigError(Exception):
    """Startup configuration is missing or invalid."""


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    return default if value is None or not value.strip() else value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m apps.data_plane.run",
        description="Run one offline data-plane HTTP service.",
    )
    parser.add_argument("service", choices=SERVICES)
    parser.add_argument("--host", default=_env("DATA_PLANE_HOST", "127.0.0.1"))
    parser.add_argument(
        "--port", type=int, default=int(_env("DATA_PLANE_PORT", "8080") or "8080")
    )
    parser.add_argument("--db", default=_env("DATA_PLANE_DB"))
    parser.add_argument("--secret-file", default=_env("DATA_PLANE_SECRET_FILE"))
    parser.add_argument("--schema", default=_env("DATA_PLANE_SCHEMA"))
    parser.add_argument("--catalog", default=_env("DATA_PLANE_CATALOG"))
    parser.add_argument(
        "--manifest-dir", default=_env("DATA_PLANE_MANIFEST_DIR", "manifests")
    )
    parser.add_argument(
        "--portal-dir", default=_env("DATA_PLANE_PORTAL_DIR")
    )
    parser.add_argument(
        "--operator-token-file",
        default=_env("DATA_PLANE_OPERATOR_TOKEN_FILE")
        or _env("DATA_PLANE_SECRET_FILE"),
    )
    parser.add_argument(
        "--operator-key-file", default=_env("DATA_PLANE_OPERATOR_KEY_FILE")
    )
    parser.add_argument(
        "--approval-ttl",
        type=int,
        default=int(
            _env("APPROVAL_TTL_SECONDS", str(MAX_APPROVAL_TTL_SECONDS))
            or str(MAX_APPROVAL_TTL_SECONDS)
        ),
    )
    parser.add_argument(
        "--approval-db", default=_env("DATA_PLANE_APPROVAL_DB")
    )
    parser.add_argument(
        "--source", default=_env("DATA_PLANE_SOURCE") or _env("SOURCE_ID")
    )
    parser.add_argument(
        "--proxy-url",
        default=_env("DATA_PLANE_PROXY_URL") or _env("SOURCE_PROXY_URL"),
    )
    parser.add_argument("--relay-url", default=_env("DATA_PLANE_RELAY_URL"))
    parser.add_argument(
        "--consumer-secrets-file", default=_env("DATA_PLANE_CONSUMER_SECRETS_FILE")
    )
    parser.add_argument("--dataset", action="append", dest="datasets")
    parser.add_argument("--group", action="append", dest="groups")
    parser.add_argument("--destination", action="append", dest="destinations")
    parser.add_argument(
        "--max-body-bytes",
        type=int,
        default=int(_env("DATA_PLANE_MAX_BODY_BYTES", str(common.MAX_BODY_BYTES)) or "0"),
    )
    return parser


def _required(value: str | None, name: str) -> str:
    if not value:
        raise ConfigError(f"{name} is required")
    return value


def _read_secret(path: str | None) -> bytes:
    target = Path(_required(path, "--secret-file (DATA_PLANE_SECRET_FILE)"))
    try:
        secret = target.read_bytes().rstrip(b"\r\n")
    except OSError as exc:
        raise ConfigError(f"cannot read secret file {target}: {exc.strerror}") from exc
    if not secret:
        raise ConfigError(f"secret file {target} is empty")
    return secret


def _read_required_file(path: str | None, name: str) -> bytes:
    target = Path(_required(path, name))
    try:
        raw = target.read_bytes().rstrip(b"\r\n")
    except OSError as exc:
        raise ConfigError(f"cannot read {name} file {target}: {exc.strerror}") from exc
    if not raw:
        raise ConfigError(f"{name} file {target} is empty")
    return raw


def _comma_list(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    return tuple(item.strip() for item in value.split(",") if item.strip())


def _load_consumer_secrets(path: str | None) -> dict[str, bytes] | None:
    if not path:
        return None
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"cannot load consumer secrets: {exc}") from exc
    if not isinstance(raw, dict) or not raw:
        raise ConfigError("consumer secrets must be a non-empty JSON object")
    result: dict[str, bytes] = {}
    for name, value in raw.items():
        if (
            not isinstance(name, str)
            or not name.strip()
            or not isinstance(value, str)
            or not value
        ):
            raise ConfigError("each consumer secret must name a non-empty string")
        result[name] = value.encode("utf-8")
    return result


def _source_contract(raw: Mapping[str, Any]) -> SourceContract:
    contract = SourceContract(
        source_id=str(raw["source_id"]),
        visibility=Visibility(str(raw["visibility"])),
        version=int(raw.get("version", 1)),
        recovery_group=str(raw["recovery_group"]),
        stable_id_fields=tuple(str(item) for item in raw.get("stable_id_fields", ())),
        capabilities=tuple(
            Capability(
                name=str(item["name"]),
                effect=EffectClass(str(item["effect"])),
                requires_confirmation=bool(item.get("requires_confirmation", False)),
                reversible=bool(item.get("reversible", True)),
                description=str(item.get("description", "")),
            )
            for item in raw.get("capabilities", ())
        ),
        authority=tuple(
            FieldAuthority(
                field=str(item["field"]),
                authority=Authority(str(item["authority"])),
                exportable=bool(item.get("exportable", True)),
                note=str(item.get("note", "")),
            )
            for item in raw.get("authority", ())
        ),
        revision_field=raw.get("revision_field"),
        tombstone_field=raw.get("tombstone_field"),
        attachment_hash_fields=tuple(
            str(item) for item in raw.get("attachment_hash_fields", ())
        ),
        fidelity_limits=tuple(
            FidelityLimit(field=str(item["field"]), reason=str(item["reason"]))
            for item in raw.get("fidelity_limits", ())
        ),
        epoch=int(raw.get("epoch", 1)),
        notes=str(raw.get("notes", "")),
    )
    contract.validate()
    return contract


def _load_catalog(path: str | None) -> dict[str, SourceContract]:
    target = Path(_required(path, "--catalog (DATA_PLANE_CATALOG)"))
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"cannot read catalog {target}: {exc}") from exc
    items = raw.get("sources") if isinstance(raw, dict) else raw
    if not isinstance(items, list) or not items:
        raise ConfigError("catalog must contain a non-empty 'sources' list")
    sources: dict[str, SourceContract] = {}
    for item in items:
        if not isinstance(item, dict):
            raise ConfigError("catalog sources must be JSON objects")
        try:
            contract = _source_contract(item)
        except (KeyError, TypeError, ValueError, DataPlaneError) as exc:
            raise ConfigError(f"invalid catalog source: {exc}") from exc
        sources[contract.source_id] = contract
    return sources


class FixtureAdapter:
    """In-memory source used until a real adapter is provisioned.

    It implements the broker's four calls with a dict per target and a
    monotonic revision stub. It never contacts the source it stands in for.
    """

    def __init__(self, contract: SourceContract) -> None:
        self._contract = contract
        self._records: dict[str, dict[str, Any]] = {}
        self._writes = 0

    def revision(self, target: str) -> str | None:
        record = self._records.get(target)
        field = self._contract.revision_field
        if record is None or field is None:
            return None
        value = record.get(field)
        return None if value is None else str(value)

    def read(self, target: str) -> dict[str, Any]:
        return dict(self._records.get(target, {}))

    def search(self, query: str) -> list[dict[str, Any]]:
        needle = query.lower()
        return [
            dict(record)
            for record in self._records.values()
            if needle in str(record.get("title", "")).lower()
        ]

    def apply(
        self, operation: NormalizedOperation, idempotency_key: str
    ) -> dict[str, Any]:
        if operation.capability == "delete":
            for target in operation.targets:
                self._records.pop(target, None)
            return {"applied": len(operation.targets)}
        self._writes += 1
        for target in operation.targets:
            record = self._records.setdefault(target, {"id": target})
            for change in operation.field_diff:
                record[change.field] = change.after
            if self._contract.revision_field:
                record[self._contract.revision_field] = f"rev-{self._writes}"
        return {"applied": len(operation.targets)}


class JournalRecorder:
    """Effect recorder that appends to the local event log."""

    def __init__(self, log: EventLog) -> None:
        self._log = log

    def record(self, event: Mapping[str, Any]) -> None:
        self._log.append("broker.effect", str(event.get("intent_id", "")), dict(event))


class FixtureSnapshotter:
    """Snapshotter that fences, checkpoints, and returns fixture snapshot ids."""

    def __init__(self, clock: Any = time.time) -> None:
        self._clock = clock
        self.events: list[tuple[str, str]] = []

    def fence(self, group_id: str) -> None:
        self.events.append(("fence", group_id))

    def checkpoint(self, group_id: str) -> None:
        self.events.append(("checkpoint", group_id))

    def snapshot(self, group_id: str) -> dict[str, str]:
        self.events.append(("snapshot", group_id))
        return {f"fixture-{group_id}": f"snap-{group_id}-{int(self._clock() * 1000)}"}

    def resume(self, group_id: str) -> None:
        self.events.append(("resume", group_id))


def _run_event(args: argparse.Namespace) -> int:
    log = EventLog(_required(args.db, "--db (DATA_PLANE_DB)"))
    try:
        service = event_service.EventService(
            log,
            _read_secret(args.secret_file),
            consumer_secrets=_load_consumer_secrets(args.consumer_secrets_file),
            max_body_bytes=args.max_body_bytes,
        )
        common.serve(
            "event", common.handler_for(service), port=args.port, host=args.host
        )
    finally:
        log.close()
    return 0


def _run_relay(args: argparse.Namespace) -> int:
    secret = _read_secret(args.secret_file)
    try:
        schema = relay_service.load_schema(
            _required(args.schema, "--schema (DATA_PLANE_SCHEMA)")
        )
    except (OSError, ValueError) as exc:
        raise ConfigError(f"cannot load relay schema: {exc}") from exc

    accepted: list[dict[str, Any]] = []

    def sink(record: dict[str, Any]) -> None:
        accepted.append(record)

    service = relay_service.RelayService(
        Relay(
            secret,
            schema,
            sink,
            source_id=_required(args.source, "--source (DATA_PLANE_SOURCE)"),
        ),
        max_body_bytes=args.max_body_bytes,
    )
    common.serve("relay", common.handler_for(service), port=args.port, host=args.host)
    return 0


def _run_broker(args: argparse.Namespace) -> int:
    secret = _read_secret(args.secret_file)
    sources = _load_catalog(args.catalog)
    db = Path(_required(args.db, "--db (DATA_PLANE_DB)"))
    journal = IntentStore(db)
    event_path = db if str(db) == ":memory:" else db.with_suffix(db.suffix + ".events")
    event_log = EventLog(event_path)
    approval_store = None
    approval_verifier = None
    try:
        issuer = IdentityIssuer(sources, secret)
        gate = ConfirmationGate()
        if args.operator_key_file:
            approval_key = _read_required_file(
                args.operator_key_file,
                "--operator-key-file (DATA_PLANE_OPERATOR_KEY_FILE)",
            )
            try:
                approval_store = UsedApprovalStore(args.approval_db or db)
            except (OSError, sqlite3.Error) as exc:
                raise ConfigError(
                    f"cannot open the approval store: {exc}"
                ) from exc
            approval_verifier = SignedApprovalVerifier(
                approval_key, approval_store, max_ttl=args.approval_ttl
            )
        adapters = {
            source_id: FixtureAdapter(contract)
            for source_id, contract in sources.items()
        }
        broker = Broker(
            sources,
            adapters,
            issuer,
            gate,
            BrokerJournal(journal),
            JournalRecorder(event_log),
            approval_verifier=approval_verifier,
        )
        service = broker_service.BrokerService(broker, issuer)
        common.serve(
            "broker", common.handler_for(service), port=args.port, host=args.host
        )
    finally:
        if approval_store is not None:
            approval_store.close()
        event_log.close()
        journal.close()
    return 0


def _run_backup(args: argparse.Namespace) -> int:
    if _env("DATA_PLANE_ALLOW_FIXTURES", "false").lower() not in {"1", "true", "yes"}:
        raise ConfigError(
            "backup service is fixture-only until a real ZFS snapshotter and "
            "restic destinations are configured; set DATA_PLANE_ALLOW_FIXTURES=true "
            "only for a local drill"
        )
    groups = tuple(args.groups or _comma_list(_env("DATA_PLANE_GROUPS"))) or (
        "fixture",
    )
    destinations = tuple(
        args.destinations or _comma_list(_env("DATA_PLANE_DESTINATIONS"))
    ) or ("fixture-a", "fixture-b")
    controller = FanoutController(
        FixtureSnapshotter(),
        [InMemoryDestination(name) for name in destinations],
        Path(args.manifest_dir),
    )
    service = backup_service.BackupService(controller, destinations, groups)
    common.serve("backup", common.handler_for(service), port=args.port, host=args.host)
    return 0


def _run_portal(args: argparse.Namespace) -> int:
    manifest_dir = Path(args.manifest_dir)
    manifests = [
        read_manifest(path) for path in sorted(manifest_dir.glob("*.json"))
    ]
    destinations = tuple(
        args.destinations or _comma_list(_env("DATA_PLANE_DESTINATIONS"))
    )
    datasets = tuple(args.datasets or _comma_list(_env("DATA_PLANE_DATASETS")))
    if not destinations:
        raise ConfigError("DATA_PLANE_DESTINATIONS is required for the portal")
    directory = Path(
        _required(args.portal_dir, "--portal-dir (DATA_PLANE_PORTAL_DIR)")
    )
    write_portal(directory, manifests, destinations, datasets, 1)
    service = portal_service.PortalService(directory)
    common.serve("portal", service.handler(), port=args.port, host=args.host)
    return 0


def _run_confirmation(args: argparse.Namespace) -> int:
    operator_token = _read_required_file(
        args.operator_token_file,
        "--operator-token-file (DATA_PLANE_OPERATOR_TOKEN_FILE)",
    )
    operator_key = _read_required_file(
        args.operator_key_file, "--operator-key-file (DATA_PLANE_OPERATOR_KEY_FILE)"
    )
    sources = _load_catalog(args.catalog)
    db = Path(_required(args.db, "--db (DATA_PLANE_DB)"))
    try:
        store = UsedApprovalStore(args.approval_db or db)
    except (OSError, sqlite3.Error) as exc:
        raise ConfigError(f"cannot open the approval store: {exc}") from exc
    try:
        service = confirmation_service.ConfirmationService(
            operator_key,
            operator_token,
            sources,
            store=store,
            max_ttl=args.approval_ttl,
        )
        common.serve(
            "confirmation",
            common.handler_for(service),
            port=args.port,
            host=args.host,
        )
    finally:
        store.close()
    return 0


def _run_collector(args: argparse.Namespace) -> int:
    secret = _read_secret(args.secret_file)
    source = _required(
        args.source,
        "--source (DATA_PLANE_SOURCE)",
    )
    proxy_url = _required(
        args.proxy_url,
        "--proxy-url (DATA_PLANE_PROXY_URL)",
    )
    relay_url = _required(args.relay_url, "--relay-url (DATA_PLANE_RELAY_URL)")
    try:
        descriptor = collector_service.load_descriptor_source(
            args.schema or source, name=source
        )
    except (OSError, ValueError, DataPlaneError) as exc:
        raise ConfigError(f"cannot load collector schema: {exc}") from exc

    accepted: list[dict[str, Any]] = []
    relay = Relay(
        secret,
        descriptor.relay_schema(),
        accepted.append,
        source_id=descriptor.source_id,
    )
    collector = collector_service.Collector(
        source_id=source,
        descriptor=descriptor,
        proxy_url=proxy_url,
        relay=relay,
        fetcher=collector_service.fetch_json,
        submitter=collector_service.HttpRelaySubmitter(relay_url),
    )
    server = common.build_server(
        "collector",
        common.handler_for(
            relay_service.RelayService(relay, max_body_bytes=args.max_body_bytes)
        ),
        host=args.host,
        port=args.port,
    )
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True
    )
    thread.start()
    try:
        results = collector.collect()
        logging.getLogger("data_plane.collector").info(
            "collected %d records from %s", len(results), descriptor.source_id
        )
        thread.join()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)
    return 0


_RUNNERS = {
    "event": _run_event,
    "broker": _run_broker,
    "relay": _run_relay,
    "backup": _run_backup,
    "portal": _run_portal,
    "confirmation": _run_confirmation,
    "collector": _run_collector,
}


def main(argv: Iterable[str] | None = None) -> int:
    args = _parser().parse_args(list(sys.argv[1:] if argv is None else argv))
    if args.max_body_bytes < 1:
        print("data-plane: --max-body-bytes must be positive", file=sys.stderr)
        return 2
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        return _RUNNERS[args.service](args)
    except (ConfigError, DataPlaneError) as exc:
        print(f"data-plane: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
