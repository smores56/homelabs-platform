"""Command line for the migration gates: preflight, verify, rollback-plan.

Every command is offline. It reads one JSON evidence file and prints JSON; no
command contacts a host, opens a repository, or executes a rollback step.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..errors import DataPlaneError
from .phases import MigrationError
from .preflight import check_preflight, parse_timestamp
from .rollback import plan_rollback
from .verify import compare_evidence


def _load(path: str | Path) -> Mapping[str, Any]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        raise MigrationError(f"cannot read evidence {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise MigrationError(f"evidence {path} is not valid JSON: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise MigrationError(f"evidence {path} is not a JSON object")
    return payload


def _print(payload: Mapping[str, Any]) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def _cmd_preflight(args: argparse.Namespace) -> int:
    now = None
    if args.now is not None:
        now = parse_timestamp(args.now)
        if now is None:
            raise MigrationError("--now is not an epoch timestamp or ISO 8601")
    report = check_preflight(_load(args.evidence), now=now)
    _print(report.to_dict())
    return 0 if report.ok else 1


def _cmd_verify(args: argparse.Namespace) -> int:
    comparison = compare_evidence(_load(args.evidence))
    _print(comparison.to_dict())
    return 0 if comparison.ok else 1


def _cmd_rollback_plan(args: argparse.Namespace) -> int:
    plan = plan_rollback(_load(args.evidence), dry_run=True)
    _print(plan.to_dict())
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m data_plane.migration",
        description=__doc__,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    preflight = sub.add_parser(
        "preflight", help="check every pre-migration evidence gate"
    )
    preflight.add_argument("evidence")
    preflight.add_argument(
        "--now",
        help="override the evidence clock (epoch seconds or ISO 8601)",
    )
    preflight.set_defaults(handler=_cmd_preflight)

    verify = sub.add_parser(
        "verify", help="compare source and replica snapshots for data loss"
    )
    verify.add_argument("evidence")
    verify.set_defaults(handler=_cmd_verify)

    rollback = sub.add_parser(
        "rollback-plan", help="print the old-system rollback plan (dry run)"
    )
    rollback.add_argument("evidence")
    rollback.set_defaults(handler=_cmd_rollback_plan)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return args.handler(args)
    except DataPlaneError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
