"""Migration gates: ordered phases, preflight evidence, verification, rollback.

The package never contacts a host and never executes a destructive step. It
exists so the migration plan, the pre-migration evidence, the data-loss gate,
and the old-system rollback can be reviewed and tested before an operator runs
anything.
"""

from __future__ import annotations

from .phases import (
    PHASES,
    PHASE_NAMES,
    Gate,
    GateBlocked,
    GateFailure,
    MigrationError,
    Phase,
    advance,
    check_advance,
    confirmation_missing,
    confirmation_phases,
    evidence_keys_for,
    irreversible_phases,
    next_phase,
    phase,
    required_evidence_keys,
)
from .preflight import (
    PreflightError,
    PreflightFailure,
    PreflightReport,
    check_preflight,
    parse_timestamp,
)
from .rollback import (
    ROLLBACK_CONFIRMATION,
    ROLLBACK_STEP_NAMES,
    RollbackBackend,
    RollbackError,
    RollbackPlan,
    RollbackStep,
    apply_rollback,
    load_plan,
    plan_rollback,
)
from .verify import (
    Comparison,
    DataLossError,
    Snapshot,
    SnapshotEntry,
    Tombstone,
    assert_no_data_loss,
    compare,
    compare_evidence,
)

__all__ = [
    "Comparison",
    "DataLossError",
    "Gate",
    "GateBlocked",
    "GateFailure",
    "MigrationError",
    "PHASES",
    "PHASE_NAMES",
    "Phase",
    "PreflightError",
    "PreflightFailure",
    "PreflightReport",
    "ROLLBACK_CONFIRMATION",
    "ROLLBACK_STEP_NAMES",
    "RollbackBackend",
    "RollbackError",
    "RollbackPlan",
    "RollbackStep",
    "Snapshot",
    "SnapshotEntry",
    "Tombstone",
    "advance",
    "apply_rollback",
    "assert_no_data_loss",
    "check_advance",
    "check_preflight",
    "compare",
    "compare_evidence",
    "confirmation_missing",
    "confirmation_phases",
    "evidence_keys_for",
    "irreversible_phases",
    "load_plan",
    "next_phase",
    "parse_timestamp",
    "phase",
    "plan_rollback",
    "required_evidence_keys",
]
