"""Ordered rollback to the old system, dry-run by default.

The rollback boundary exists only while the 970 still holds the old system.
Before that boundary is crossed, rollback means: stop the new writers, boot the
old target, restore the latest verified old generation, and prove an SSH rescue
path again. After the 970 is converted, this plan is invalid and recovery
depends on the new system's independently verified generations.

Planning is offline and returns argv-only steps. ``plan_rollback`` defaults to
a dry run; ``apply_rollback`` needs both a non-dry plan and the literal
confirmation ``rollback``. Nothing here executes a command by itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol, Sequence

from .phases import MigrationError

ROLLBACK_CONFIRMATION = "rollback"
ROLLBACK_STEP_NAMES: tuple[str, ...] = (
    "stop_new_writers",
    "restore_old_boot_target",
    "restore_old_generation",
    "enable_rescue_ssh",
)


class RollbackError(MigrationError):
    """The old-system rollback cannot be planned or applied safely."""


@dataclass(frozen=True)
class RollbackStep:
    """One argv-only action. No step passes through a shell."""

    order: int
    name: str
    description: str
    argv: tuple[str, ...]
    environment: tuple[tuple[str, str], ...] = ()
    irreversible: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "argv", tuple(self.argv))
        object.__setattr__(self, "environment", tuple(self.environment))
        if self.name not in ROLLBACK_STEP_NAMES:
            raise RollbackError(f"unknown rollback step {self.name!r}")
        if not self.argv or any(not part for part in self.argv):
            raise RollbackError(f"rollback step {self.name!r} has an empty argv")
        if self.order < 1:
            raise RollbackError(f"rollback step {self.name!r} has no order")

    def to_dict(self) -> dict[str, Any]:
        return {
            "order": self.order,
            "name": self.name,
            "description": self.description,
            "argv": list(self.argv),
            "environment": {key: value for key, value in self.environment},
            "irreversible": self.irreversible,
        }


@dataclass(frozen=True)
class RollbackPlan:
    """The reviewed rollback sequence. It performs nothing until applied."""

    steps: tuple[RollbackStep, ...]
    dry_run: bool = True
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "steps", tuple(self.steps))
        object.__setattr__(self, "warnings", tuple(self.warnings))
        names = tuple(step.name for step in self.steps)
        if names != ROLLBACK_STEP_NAMES:
            raise RollbackError(
                "rollback plan must follow " + " -> ".join(ROLLBACK_STEP_NAMES)
            )
        if [step.order for step in self.steps] != list(
            range(1, len(self.steps) + 1)
        ):
            raise RollbackError("rollback steps are not numbered in order")

    def commands(self) -> list[list[str]]:
        return [list(step.argv) for step in self.steps]

    def to_dict(self) -> dict[str, Any]:
        return {
            "dry_run": self.dry_run,
            "confirmation_required": ROLLBACK_CONFIRMATION,
            "warnings": list(self.warnings),
            "steps": [step.to_dict() for step in self.steps],
        }


class RollbackBackend(Protocol):
    """Runs one approved rollback step. The caller owns authorization."""

    def run(
        self, argv: Sequence[str], *, environment: Mapping[str, str] | None = None
    ) -> None:
        """Execute ``argv`` with a non-secret environment."""


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _units(value: Any, label: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or not value:
        raise RollbackError(f"{label} must be a non-empty list")
    units = tuple(_text(item) for item in value)
    if any(not unit for unit in units):
        raise RollbackError(f"{label} contains an empty unit name")
    if len(set(units)) != len(units):
        raise RollbackError(f"{label} repeats a unit")
    return units


def _ssh_warning(evidence: Mapping[str, Any]) -> str | None:
    paths = evidence.get("ssh_paths")
    if not isinstance(paths, (list, tuple)):
        return "no ssh_paths evidence; re-establish and verify both rescue paths"
    proven = [
        item
        for item in paths
        if isinstance(item, Mapping)
        and item.get("verified") is True
        and item.get("independent_of_data_plane") is True
    ]
    if len(proven) < 2:
        return (
            "fewer than two verified independent SSH paths; the final step must "
            "re-establish one before rollback is declared complete"
        )
    return None


def plan_rollback(
    evidence: Mapping[str, Any], *, dry_run: bool = True
) -> RollbackPlan:
    """Build the four-step rollback plan from reviewed evidence.

    Required evidence: ``new_writer_units``, ``old_boot_entry``, and an
    ``old_generation`` mapping naming a verified generation and its manifest.
    ``rescue_ssh_units`` defaults to ``ssh.service``.
    """

    if not isinstance(evidence, Mapping):
        raise RollbackError("rollback evidence is not an object")
    if not isinstance(dry_run, bool):
        raise RollbackError("dry_run must be a boolean")

    disk = evidence.get("rollback_disk")
    if isinstance(disk, Mapping) and (
        disk.get("converted") is True or disk.get("wiped") is True
    ):
        raise RollbackError(
            "the rollback disk was converted; the old-system rollback "
            "boundary has passed"
        )

    writers = _units(evidence.get("new_writer_units"), "new_writer_units")
    boot_entry = _text(evidence.get("old_boot_entry"))
    if not boot_entry:
        raise RollbackError("old_boot_entry is empty")

    generation = evidence.get("old_generation")
    if not isinstance(generation, Mapping):
        raise RollbackError("old_generation is not an object")
    if generation.get("verified") is not True:
        raise RollbackError(
            "old_generation is not verified; refusing to plan a restore from it"
        )
    generation_id = _text(generation.get("generation_id"))
    manifest = _text(generation.get("manifest"))
    if not generation_id:
        raise RollbackError("old_generation.generation_id is empty")
    if not manifest.startswith("/"):
        raise RollbackError("old_generation.manifest must be an absolute path")

    ssh_units = (
        _units(evidence.get("rescue_ssh_units"), "rescue_ssh_units")
        if "rescue_ssh_units" in evidence
        else ("ssh.service",)
    )

    warnings: list[str] = []
    warning = _ssh_warning(evidence)
    if warning:
        warnings.append(warning)
    if not dry_run:
        warnings.append(
            "applying this plan overwrites new-system data with the old "
            f"generation {generation_id}; approval is not reversible"
        )

    steps = (
        RollbackStep(
            order=1,
            name="stop_new_writers",
            description=(
                "Disable the new writers so nothing advances while the old "
                "system is restored; SSH is not among these units."
            ),
            argv=("systemctl", "disable", "--now", *writers),
        ),
        RollbackStep(
            order=2,
            name="restore_old_boot_target",
            description=(
                f"Make the old boot entry {boot_entry!r} the default so the "
                "next boot is the pre-migration system."
            ),
            argv=("bootctl", "set-default", boot_entry),
        ),
        RollbackStep(
            order=3,
            name="restore_old_generation",
            description=(
                f"Restore the latest verified old generation {generation_id} "
                "through the clean-room entrypoint."
            ),
            argv=("/usr/local/sbin/homelab-clean-room-restore",),
            environment=(
                ("HOMELAB_GENERATION_MANIFEST", manifest),
                ("HOMELAB_RESTORE_CONFIRM", "restore"),
                ("HOMELAB_RESTORE_APPLY", "1"),
            ),
            irreversible=True,
        ),
        RollbackStep(
            order=4,
            name="enable_rescue_ssh",
            description=(
                "Re-enable the independent SSH unit and verify every declared "
                "path before declaring rollback complete."
            ),
            argv=("systemctl", "enable", "--now", *ssh_units),
        ),
    )
    return RollbackPlan(steps=steps, dry_run=dry_run, warnings=tuple(warnings))


def apply_rollback(
    plan: RollbackPlan,
    backend: RollbackBackend | None,
    confirm: str,
) -> tuple[str, ...]:
    """Run a non-dry plan through ``backend`` after the literal confirmation."""

    if not isinstance(plan, RollbackPlan):
        raise RollbackError("apply_rollback needs a RollbackPlan")
    if plan.dry_run:
        raise RollbackError("this rollback plan is a dry run; plan with dry_run=False")
    if confirm != ROLLBACK_CONFIRMATION:
        raise RollbackError(
            f"rollback needs the literal confirmation {ROLLBACK_CONFIRMATION!r}"
        )
    runner = getattr(backend, "run", None)
    if not callable(runner):
        raise RollbackError(
            "rollback execution needs a backend with run(argv, environment=...)"
        )
    executed: list[str] = []
    for step in plan.steps:
        runner(step.argv, environment=dict(step.environment))
        executed.append(step.name)
    return tuple(executed)


def load_plan(evidence: Mapping[str, Any]) -> RollbackPlan:
    """Convenience wrapper used by the CLI: always a dry-run plan."""

    return plan_rollback(evidence, dry_run=True)
