"""Pre-migration evidence checks that fail closed.

``check_preflight`` reads one evidence mapping and returns a structured report.
It never raises for missing or malformed evidence: a caller that cannot prove a
condition gets a failure naming the check and the subject, so an operator sees
every gap at once. The only requirement is that the conditions are provable
from the evidence, not asserted in prose.

Evidence shape (all timestamps are epoch seconds or ISO 8601)::

    {
      "now": "2026-11-14T06:00:00+00:00",
      "required_destinations": ["primary", "secondary", "r2"],
      "required_groups": ["notes", "photos"],
      "backup_max_age_hours": 24,
      "restore_max_age_hours": 24,
      "ssh_max_age_hours": 24,
      "backups": [
        {"group": "notes", "destination": "primary",
         "created_at": "...", "verified": true, "complete": true}
      ],
      "restore_verification": {
        "generation_id": "notes-...", "storage": "scratch",
        "verified": true, "verified_at": "..."
      },
      "rollback_disk": {
        "device": "/dev/disk/by-id/nvme-Samsung_970...",
        "present": true, "boot_tested": true, "converted": false
      },
      "ssh_paths": [
        {"name": "lan", "kind": "direct", "verified": true,
         "independent_of_data_plane": true, "verified_at": "..."},
        {"name": "tunnel", "kind": "cloudflared-rescue", "verified": true,
         "independent_of_data_plane": true, "verified_at": "..."}
      ],
      "operator_confirmations": {
        "cutover": {"confirmed": true, "by": "smores", "at": "..."},
        "retire_old": {"confirmed": true, "by": "smores", "at": "..."}
      }
    }

A backup counts only when it is fresh, verified, and complete. A restore
counts only when it was verified on scratch storage. Two SSH paths count only
when both are verified recently and independent of the data plane and k3s.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping

from .phases import (
    FRESH_BACKUP,
    RESTORE_VERIFIED,
    ROLLBACK_DISK_BOOT_TESTED,
    SSH_PATHS_INDEPENDENT,
    MigrationError,
    confirmation_missing,
    confirmation_phases,
    required_evidence_keys,
)

DEFAULT_MAX_AGE_HOURS = 24.0


class PreflightError(MigrationError):
    """Preflight evidence does not allow a destructive migration step."""

    def __init__(self, failures: "tuple[PreflightFailure, ...]") -> None:
        self.failures = tuple(failures)
        detail = "; ".join(
            f"{failure.check}[{failure.subject}]: {failure.detail}"
            for failure in self.failures
        )
        super().__init__(f"preflight failed: {detail}")


@dataclass(frozen=True)
class PreflightFailure:
    """One failed condition, with enough detail to fix the evidence file."""

    check: str
    subject: str
    detail: str

    def to_dict(self) -> dict[str, str]:
        return {
            "check": self.check,
            "subject": self.subject,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class PreflightReport:
    """Everything preflight proved and everything it could not."""

    failures: tuple[PreflightFailure, ...] = ()
    evidence: Mapping[str, Any] = field(default_factory=dict)
    checked: tuple[str, ...] = ()
    now: float = 0.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "failures", tuple(self.failures))
        object.__setattr__(self, "checked", tuple(self.checked))
        object.__setattr__(self, "evidence", dict(self.evidence))

    @property
    def ok(self) -> bool:
        return not self.failures

    def require(self) -> "PreflightReport":
        """Raise ``PreflightError`` unless every check passed."""

        if not self.ok:
            raise PreflightError(self.failures)
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "now": self.now,
            "checked": list(self.checked),
            "failures": [failure.to_dict() for failure in self.failures],
            "evidence": dict(self.evidence),
        }


def _epoch(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            pass
        try:
            return datetime.fromisoformat(
                text[:-1] + "+00:00" if text.endswith("Z") else text
            ).timestamp()
        except ValueError:
            return None
    return None


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, (list, tuple)) else []


def _names(value: Any) -> tuple[list[str], str | None]:
    items = _list(value)
    if not items:
        return [], "is not a non-empty list"
    names = [_text(item) for item in items]
    if any(not name for name in names):
        return names, "contains an empty name"
    if len(set(names)) != len(names):
        return names, "contains duplicate names"
    return names, None


def _hours(evidence: Mapping[str, Any], key: str) -> float | None:
    value = evidence.get(key, DEFAULT_MAX_AGE_HOURS)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return None
    return float(value)


class _Collector:
    def __init__(self) -> None:
        self.failures: list[PreflightFailure] = []
        self.checked: list[str] = []

    def fail(self, check: str, subject: str, detail: str) -> None:
        self.failures.append(
            PreflightFailure(check=check, subject=str(subject), detail=detail)
        )

    def check(self, name: str) -> None:
        if name not in self.checked:
            self.checked.append(name)


def _check_backups(
    evidence: Mapping[str, Any], now: float, out: _Collector
) -> bool:
    out.check("backup_freshness")
    destinations, problem = _names(evidence.get("required_destinations"))
    if problem:
        out.fail("backup_freshness", "required_destinations", problem)
        return False

    groups: list[str] = []
    if "required_groups" in evidence:
        groups, problem = _names(evidence.get("required_groups"))
        if problem:
            out.fail("backup_freshness", "required_groups", problem)
            return False

    max_age = _hours(evidence, "backup_max_age_hours")
    if max_age is None:
        out.fail(
            "backup_freshness",
            "backup_max_age_hours",
            "must be a positive number of hours",
        )
        return False
    limit = max_age * 3600.0

    raw_backups = evidence.get("backups")
    if not isinstance(raw_backups, (list, tuple)):
        out.fail("backup_freshness", "backups", "is not a list")
        return False

    # Latest *qualified* capture per (destination, group), plus why the newest
    # capture of a destination did not qualify.
    eligible: dict[tuple[str, str], float] = {}
    rejected: dict[tuple[str, str], str] = {}
    for index, raw in enumerate(raw_backups):
        if not isinstance(raw, Mapping):
            out.fail("backup_freshness", f"backups[{index}]", "is not an object")
            continue
        destination = _text(raw.get("destination"))
        group = _text(raw.get("group"))
        subject = f"{destination or '?'}/{group or '?'}"
        if not destination or not group:
            out.fail(
                "backup_freshness",
                f"backups[{index}]",
                "needs a non-empty destination and group",
            )
            continue
        created = _epoch(raw.get("created_at"))
        if created is None:
            out.fail(
                "backup_freshness",
                subject,
                "created_at is missing or not a timestamp",
            )
            continue
        key = (destination, group)
        if created > now:
            rejected[key] = "backup is dated in the future"
            continue
        if raw.get("verified") is not True:
            rejected[key] = "backup is not verified"
            continue
        if raw.get("complete", True) is not True:
            rejected[key] = "backup generation is incomplete"
            continue
        age = now - created
        if age > limit:
            rejected[key] = (
                f"backup is stale by {age / 3600:.1f}h "
                f"(limit {max_age:.0f}h)"
            )
            continue
        if created > eligible.get(key, -1.0):
            eligible[key] = created

    if groups:
        pairs = [(destination, group) for destination in destinations for group in groups]
    else:
        pairs = [(destination, None) for destination in destinations]

    ok = True
    for destination, group in pairs:
        if group is None:
            matches = [
                created
                for (candidate, _), created in eligible.items()
                if candidate == destination
            ]
            subject = destination
        else:
            matches = (
                [eligible[(destination, group)]]
                if (destination, group) in eligible
                else []
            )
            subject = f"{destination}/{group}"
        if matches:
            continue
        ok = False
        reason = (
            rejected.get((destination, group))
            if group is not None
            else next(
                (
                    detail
                    for (candidate, _), detail in rejected.items()
                    if candidate == destination
                ),
                None,
            )
        )
        out.fail(
            "backup_freshness",
            subject,
            reason or "no backup is recorded for this destination",
        )
    return ok


def _check_restore(evidence: Mapping[str, Any], now: float, out: _Collector) -> bool:
    out.check("restore_verification")
    restore = evidence.get("restore_verification")
    if not isinstance(restore, Mapping):
        out.fail("restore_verification", "restore_verification", "is not an object")
        return False

    max_age = _hours(evidence, "restore_max_age_hours")
    if max_age is None:
        out.fail(
            "restore_verification",
            "restore_max_age_hours",
            "must be a positive number of hours",
        )
        return False

    ok = True
    if restore.get("verified") is not True:
        out.fail("restore_verification", "verified", "the restore is not verified")
        ok = False
    storage = _text(restore.get("storage"))
    if storage != "scratch":
        out.fail(
            "restore_verification",
            "storage",
            "the verified restore must target scratch storage",
        )
        ok = False
    if not _text(restore.get("generation_id")):
        out.fail(
            "restore_verification",
            "generation_id",
            "the restore must name the generation it verified",
        )
        ok = False
    verified_at = _epoch(restore.get("verified_at"))
    if verified_at is None:
        out.fail(
            "restore_verification",
            "verified_at",
            "is missing or not a timestamp",
        )
        ok = False
    elif verified_at > now:
        out.fail("restore_verification", "verified_at", "is dated in the future")
        ok = False
    elif now - verified_at > max_age * 3600.0:
        out.fail(
            "restore_verification",
            "verified_at",
            f"restore proof is stale by {(now - verified_at) / 3600:.1f}h",
        )
        ok = False
    return ok


def _check_rollback_disk(evidence: Mapping[str, Any], out: _Collector) -> bool:
    out.check("rollback_disk")
    disk = evidence.get("rollback_disk")
    if not isinstance(disk, Mapping):
        out.fail("rollback_disk", "rollback_disk", "is not an object")
        return False

    ok = True
    if disk.get("present") is not True:
        out.fail("rollback_disk", "present", "the 970 rollback disk is not present")
        ok = False
    if disk.get("boot_tested") is not True:
        out.fail(
            "rollback_disk",
            "boot_tested",
            "the old system has not been booted from the rollback disk",
        )
        ok = False
    device = _text(disk.get("device"))
    if not device.startswith("/") or len(device) < 2:
        out.fail(
            "rollback_disk",
            "device",
            "name an absolute device path, preferably /dev/disk/by-id/...",
        )
        ok = False
    for key in ("converted", "wiped"):
        if disk.get(key) is True:
            out.fail(
                "rollback_disk",
                key,
                "the rollback disk was already converted; rollback is no longer possible",
            )
            ok = False
    return ok


def _check_ssh_paths(evidence: Mapping[str, Any], now: float, out: _Collector) -> bool:
    out.check("ssh_paths")
    paths = evidence.get("ssh_paths")
    if not isinstance(paths, (list, tuple)):
        out.fail("ssh_paths", "ssh_paths", "is not a list")
        return False

    max_age = _hours(evidence, "ssh_max_age_hours")
    if max_age is None:
        out.fail(
            "ssh_paths",
            "ssh_max_age_hours",
            "must be a positive number of hours",
        )
        return False

    proven: list[str] = []
    names: list[str] = []
    for index, raw in enumerate(paths):
        if not isinstance(raw, Mapping):
            out.fail("ssh_paths", f"ssh_paths[{index}]", "is not an object")
            continue
        name = _text(raw.get("name"))
        kind = _text(raw.get("kind"))
        subject = name or f"ssh_paths[{index}]"
        if not name:
            out.fail("ssh_paths", subject, "needs a non-empty name")
            continue
        if name in names:
            out.fail("ssh_paths", subject, "repeats an earlier path name")
            continue
        names.append(name)
        if not kind:
            out.fail("ssh_paths", subject, "needs a non-empty kind")
            continue
        if raw.get("independent_of_data_plane") is not True:
            out.fail(
                "ssh_paths",
                subject,
                "must be independent of k3s and the data plane",
            )
            continue
        if raw.get("verified") is not True:
            out.fail("ssh_paths", subject, "path is not verified")
            continue
        verified_at = _epoch(raw.get("verified_at"))
        if verified_at is None:
            out.fail("ssh_paths", subject, "verified_at is missing or invalid")
            continue
        if verified_at > now:
            out.fail("ssh_paths", subject, "verified_at is dated in the future")
            continue
        if now - verified_at > max_age * 3600.0:
            out.fail(
                "ssh_paths",
                subject,
                f"verification is stale by {(now - verified_at) / 3600:.1f}h",
            )
            continue
        proven.append(name)

    if len(proven) < 2:
        out.fail(
            "ssh_paths",
            "independent_paths",
            f"need at least two verified independent paths, have {len(proven)}"
            + (f" ({', '.join(proven)})" if proven else ""),
        )
        return False
    return True


def _check_confirmations(evidence: Mapping[str, Any], out: _Collector) -> bool:
    out.check("operator_confirmation")
    ok = True
    confirmations = evidence.get("operator_confirmations")
    if confirmations is not None and not isinstance(confirmations, Mapping):
        out.fail(
            "operator_confirmation",
            "operator_confirmations",
            "is not an object keyed by phase",
        )
        return False
    if isinstance(confirmations, Mapping):
        known = set(confirmation_phases())
        for name in confirmations:
            if name not in known:
                out.fail(
                    "operator_confirmation",
                    str(name),
                    "confirmation names a phase that needs none",
                )
                ok = False
    for name in confirmation_phases():
        missing = confirmation_missing(name, evidence)
        if missing:
            ok = False
            out.fail(
                "operator_confirmation",
                name,
                "missing explicit confirmation fields: " + ", ".join(missing),
            )
    return ok


def check_preflight(
    evidence: Mapping[str, Any], *, now: float | None = None
) -> PreflightReport:
    """Check every precondition and return all failures, never just the first."""

    out = _Collector()
    if not isinstance(evidence, Mapping):
        out.fail("evidence", "root", "evidence is not an object")
        return PreflightReport(
            failures=tuple(out.failures),
            evidence={},
            checked=tuple(out.checked),
            now=float(now or 0.0),
        )

    moment = float(now) if now is not None else _epoch(evidence.get("now"))
    if moment is None:
        moment = time.time()

    backups_ok = _check_backups(evidence, moment, out)
    restore_ok = _check_restore(evidence, moment, out)
    disk_ok = _check_rollback_disk(evidence, out)
    ssh_ok = _check_ssh_paths(evidence, moment, out)
    confirmations_ok = _check_confirmations(evidence, out)

    summary: dict[str, Any] = {
        FRESH_BACKUP: backups_ok,
        RESTORE_VERIFIED: restore_ok,
        ROLLBACK_DISK_BOOT_TESTED: disk_ok,
        SSH_PATHS_INDEPENDENT: ssh_ok,
    }
    # Raw gate keys are carried through so ``phases.advance`` can consume the
    # same report without a translation table.
    for key in required_evidence_keys():
        if key not in summary:
            summary[key] = bool(evidence.get(key))
    summary["operator_confirmation"] = confirmations_ok
    confirmations = evidence.get("operator_confirmations")
    if isinstance(confirmations, Mapping):
        summary["operator_confirmations"] = dict(confirmations)

    return PreflightReport(
        failures=tuple(out.failures),
        evidence=summary,
        checked=tuple(out.checked),
        now=moment,
    )


def parse_timestamp(value: Any) -> float | None:
    """Parse epoch seconds or ISO 8601; ``None`` when the value is unusable."""

    return _epoch(value)
