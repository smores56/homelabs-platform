"""Ordered migration phases and the evidence gates that hold them.

A phase is a boundary, not a command. The operator may cross it only when the
gate's evidence is present; ``protect_old`` and ``cutover`` are the hard gates
that stand between a working old system and a new one that cannot yet be
recovered. Evidence is a plain mapping so the same object can come from the
preflight report, a reviewed JSON file, or a test fixture.

Nothing here executes a command, reads a secret, or contacts a host.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from ..errors import DataPlaneError

# Evidence keys shared by the preflight report and the phase gates.
FRESH_BACKUP = "fresh_backup"
RESTORE_VERIFIED = "restore_verified"
ROLLBACK_DISK_BOOT_TESTED = "rollback_disk_boot_tested"
SSH_PATHS_INDEPENDENT = "ssh_paths_independent"
CUTOVER_REHEARSAL_PASSED = "cutover_rehearsal_passed"


class MigrationError(DataPlaneError):
    """A migration gate, verification, or rollback cannot be trusted."""


@dataclass(frozen=True)
class GateFailure:
    """One missing piece of evidence, tied to the gate that requires it."""

    phase: str
    gate: str
    missing: tuple[str, ...]
    detail: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "missing", tuple(self.missing))

    @property
    def message(self) -> str:
        return self.detail


class GateBlocked(MigrationError):
    """Advancing would cross a gate whose evidence or confirmation is absent."""

    def __init__(self, phase: str, failures: Iterable[GateFailure]) -> None:
        self.phase = phase
        self.failures = tuple(failures)
        detail = "; ".join(failure.message for failure in self.failures)
        super().__init__(f"cannot advance past {phase!r}: {detail}")


def _present(value: Any) -> bool:
    """Evidence is satisfied by a truthy value, not merely by a key."""

    if value is None or value is False:
        return False
    if isinstance(value, (str, bytes, list, tuple, dict, set, frozenset)):
        return len(value) > 0
    return bool(value)


@dataclass(frozen=True)
class Gate:
    """The evidence a phase must present before the plan will move on."""

    name: str
    evidence: tuple[str, ...]
    irreversible: bool = False
    requires_operator_confirmation: bool = False
    description: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "evidence", tuple(self.evidence))
        if not self.name.strip():
            raise MigrationError("gate has an empty name")
        if not self.evidence:
            raise MigrationError(f"gate {self.name!r} requires no evidence")
        if any(not item.strip() for item in self.evidence):
            raise MigrationError(f"gate {self.name!r} has an empty evidence key")
        if len(set(self.evidence)) != len(self.evidence):
            raise MigrationError(f"gate {self.name!r} repeats an evidence key")
        # An irreversible gate without a human confirmation would let the plan
        # pass a point of no return on machine evidence alone.
        if self.irreversible and not self.requires_operator_confirmation:
            raise MigrationError(
                f"gate {self.name!r} is irreversible and must require confirmation"
            )

    def missing(self, evidence: Mapping[str, Any]) -> tuple[str, ...]:
        return tuple(key for key in self.evidence if not _present(evidence.get(key)))


@dataclass(frozen=True)
class Phase:
    """One ordered phase in the migration, guarded by exactly one gate."""

    name: str
    description: str
    gate: Gate

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise MigrationError("phase has an empty name")


_PROTECT_EVIDENCE = (
    FRESH_BACKUP,
    RESTORE_VERIFIED,
    ROLLBACK_DISK_BOOT_TESTED,
    SSH_PATHS_INDEPENDENT,
)

PHASES: tuple[Phase, ...] = (
    Phase(
        name="inventory",
        description=(
            "Record the read-only machine, source, disk, credential, and local "
            "Git state before anything is copied."
        ),
        gate=Gate(
            name="inventory-complete",
            evidence=("machine_inventory", "source_inventory", "git_inventory"),
            description="Every target has a recorded baseline.",
        ),
    ),
    Phase(
        name="protect_old",
        description=(
            "Complete the old system's HDD/R2 backups, populate the recovery "
            "kit, prove an old-system restore onto scratch storage, and boot the "
            "970 rollback disk."
        ),
        gate=Gate(
            name="old-system-protected",
            evidence=_PROTECT_EVIDENCE,
            description=(
                "A fresh verified backup exists on every required destination, "
                "a restore is verified on scratch storage, the 970 boots, and "
                "two independent SSH paths are proven."
            ),
        ),
    ),
    Phase(
        name="pilot",
        description=(
            "Pilot one repository with ./dev and the Fedora Atomic/dotfiles/ghq "
            "client contract on a nonproduction target."
        ),
        gate=Gate(
            name="pilot-signed-off",
            evidence=("pilot_client_signed_off",),
            description="The pilot target is accepted and its old home is read-only.",
        ),
    ),
    Phase(
        name="scratch_stack",
        description=(
            "Build the Ubuntu/k3s/ZFS/data-plane stack with synthetic data and "
            "canary credentials, and complete the recovery spikes."
        ),
        gate=Gate(
            name="scratch-stack-verified",
            evidence=("scratch_stack_verified", "recovery_spike_passed"),
            description="Scratch boot, atomic snapshots, and a cold restore pass.",
        ),
    ),
    Phase(
        name="shadow_backfill",
        description=(
            "Create namespaced repositories, run shadow backups, and backfill "
            "read-only replicas while recording fidelity gaps."
        ),
        gate=Gate(
            name="shadow-reconciled",
            evidence=("shadow_reconciled", "fidelity_gaps_recorded"),
            description="Replica deltas and unrecoverable historical gaps are known.",
        ),
    ),
    Phase(
        name="cutover",
        description=(
            "Pause each authoritative writer, capture the final delta, switch "
            "its route, and keep the old service read-only for the rollback "
            "window."
        ),
        gate=Gate(
            name="cutover-ready",
            evidence=_PROTECT_EVIDENCE + (CUTOVER_REHEARSAL_PASSED,),
            requires_operator_confirmation=True,
            description=(
                "The protection evidence still holds, a rollback rehearsal "
                "passed, and the operator confirms the cutover."
            ),
        ),
    ),
    Phase(
        name="retire_old",
        description=(
            "Delete retired data and Graphiti as approved, convert the 970 to a "
            "Restic destination, and archive the legacy repositories."
        ),
        gate=Gate(
            name="retirement-approved",
            evidence=(
                "retired_writers_stopped",
                "new_generation_verified",
                "rollback_no_longer_required",
            ),
            irreversible=True,
            requires_operator_confirmation=True,
            description=(
                "Wiping the 970 ends the old-system rollback boundary; it needs "
                "a verified new generation and an explicit human approval."
            ),
        ),
    ),
)

PHASE_NAMES: tuple[str, ...] = tuple(phase.name for phase in PHASES)
_BY_NAME: dict[str, Phase] = {phase.name: phase for phase in PHASES}


def phase(name: str) -> Phase:
    try:
        return _BY_NAME[name]
    except KeyError as exc:
        raise MigrationError(
            f"unknown migration phase {name!r}; expected one of "
            + ", ".join(PHASE_NAMES)
        ) from exc


def next_phase(name: str) -> Phase | None:
    """The phase after ``name``, or ``None`` after the final phase."""

    ordered = phase(name)
    index = PHASES.index(ordered)
    if index + 1 == len(PHASES):
        return None
    return PHASES[index + 1]


def confirmation_phases() -> tuple[str, ...]:
    """Phases whose gate needs a recorded human confirmation."""

    return tuple(
        item.name
        for item in PHASES
        if item.gate.requires_operator_confirmation or item.gate.irreversible
    )


def irreversible_phases() -> tuple[str, ...]:
    return tuple(item.name for item in PHASES if item.gate.irreversible)


def required_evidence_keys() -> tuple[str, ...]:
    """Every gate evidence key, in first-seen order."""

    seen: list[str] = []
    for item in PHASES:
        for key in item.gate.evidence:
            if key not in seen:
                seen.append(key)
    return tuple(seen)


def confirmation_missing(
    phase_name: str, evidence: Mapping[str, Any]
) -> tuple[str, ...]:
    """Return the missing confirmation fields for ``phase_name``.

    A confirmation is an explicit mapping, never a bare boolean: the operator,
    the time, and the word ``confirmed`` all have to be present so a later
    review can tell who approved which irreversible action.
    """

    confirmations = evidence.get("operator_confirmations")
    if not isinstance(confirmations, Mapping):
        return (f"operator_confirmations.{phase_name}",)
    entry = confirmations.get(phase_name)
    if not isinstance(entry, Mapping):
        return (f"operator_confirmations.{phase_name}",)
    missing: list[str] = []
    if entry.get("confirmed") is not True:
        missing.append(f"operator_confirmations.{phase_name}.confirmed")
    for field in ("by", "at"):
        value = entry.get(field)
        if not isinstance(value, str) or not value.strip():
            missing.append(f"operator_confirmations.{phase_name}.{field}")
    return tuple(missing)


def check_advance(
    current: str, evidence: Mapping[str, Any]
) -> tuple[GateFailure, ...]:
    """Structured reasons ``current`` cannot be left, empty when it can."""

    item = phase(current)
    if not isinstance(evidence, Mapping):
        return (
            GateFailure(
                phase=item.name,
                gate=item.gate.name,
                missing=item.gate.evidence,
                detail=f"evidence for {item.name!r} is not a mapping",
            ),
        )

    failures: list[GateFailure] = []
    missing = item.gate.missing(evidence)
    if missing:
        failures.append(
            GateFailure(
                phase=item.name,
                gate=item.gate.name,
                missing=missing,
                detail=(
                    f"gate {item.gate.name!r} after {item.name!r} is missing: "
                    + ", ".join(missing)
                ),
            )
        )
    if item.gate.requires_operator_confirmation or item.gate.irreversible:
        confirmation = confirmation_missing(item.name, evidence)
        if confirmation:
            failures.append(
                GateFailure(
                    phase=item.name,
                    gate=item.gate.name,
                    missing=confirmation,
                    detail=(
                        f"gate {item.gate.name!r} after {item.name!r} needs an "
                        "explicit operator confirmation: "
                        + ", ".join(confirmation)
                    ),
                )
            )
    return tuple(failures)


def advance(current: str, evidence: Mapping[str, Any]) -> Phase | None:
    """Cross out of ``current``; raise ``GateBlocked`` when evidence is short.

    The function is pure: it returns the next phase and never changes state.
    Callers own persistence of where the migration actually is.
    """

    failures = check_advance(current, evidence)
    if failures:
        raise GateBlocked(current, failures)
    return next_phase(current)


def evidence_keys_for(name: str) -> tuple[str, ...]:
    return phase(name).gate.evidence
