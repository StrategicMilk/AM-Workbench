"""Workflow Quality Gates.

======================
Formal quality gates between workflow stages, inspired by manufacturing
stage-gate processes.  Each gate defines criteria that must be satisfied
before work proceeds to the next stage, along with a prescribed failure
action (re-plan, retry, escalate, or block).
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from vetinari.utils.bounded_collections import BoundedList

logger = logging.getLogger(__name__)

MAX_GATE_HISTORY = 1_000


# ---------------------------------------------------------------------------
# Enums & data classes
# ---------------------------------------------------------------------------


class GateAction(Enum):
    """Action to take when a quality gate fails."""

    RE_PLAN = "re_plan"
    RETRY = "retry"
    ESCALATE = "escalate"
    BLOCK = "block"
    CONTINUE = "continue"


@dataclass
class WorkflowGate:
    """A formal quality gate between workflow stages.

    Parameters
    ----------
    name:
        Human-readable identifier for the gate.
    stage:
        Pipeline stage this gate guards
        (``post_decomposition``, ``post_execution``,
        ``post_verification``, ``pre_assembly``).
    criteria:
        Key/value pairs that incoming metrics must satisfy.
        Numeric values are treated as *minimum* thresholds;
        boolean values must match exactly.
    failure_action:
        What to do when the gate is not passed.
    """

    name: str
    stage: str
    criteria: dict[str, Any]
    failure_action: GateAction

    def __repr__(self) -> str:
        """Show key identifying fields for debugging."""
        return f"WorkflowGate(name={self.name!r}, stage={self.stage!r}, failure_action={self.failure_action!r})"


# ---------------------------------------------------------------------------
# Default gate definitions
# ---------------------------------------------------------------------------

WORKFLOW_GATES: dict[str, WorkflowGate] = {
    "post_decomposition": WorkflowGate(
        name="decomposition_gate",
        stage="post_decomposition",
        criteria={
            "min_tasks": 1,
            "max_tasks": 50,
            "dag_valid": True,
            "goal_adherence": 0.7,
        },
        failure_action=GateAction.RE_PLAN,
    ),
    "post_execution": WorkflowGate(
        name="execution_gate",
        stage="post_execution",
        criteria={
            "quality_score": 0.6,
            "no_critical_security": True,
        },
        failure_action=GateAction.RETRY,
    ),
    "post_verification": WorkflowGate(
        name="verification_gate",
        stage="post_verification",
        criteria={
            "verification_passed": True,
            "drift_score_max": 0.5,
        },
        failure_action=GateAction.ESCALATE,
    ),
    "pre_assembly": WorkflowGate(
        name="assembly_gate",
        stage="pre_assembly",
        criteria={
            "all_dependencies_met": True,
            "no_blocked_tasks": True,
        },
        failure_action=GateAction.BLOCK,
    ),
}


def _gate_violations(gate: WorkflowGate, metrics: dict[str, Any]) -> list[str]:
    def violation_for(key: str, threshold: Any) -> str | None:
        value = metrics.get(key)
        if value is None:
            return f"Missing metric: {key}"
        if isinstance(threshold, bool):
            if value != threshold:
                return f"{key}: expected {threshold}, got {value}"
        elif isinstance(threshold, (int, float)):
            if "max" in key and value > threshold:
                return f"{key}: {value} exceeds maximum {threshold}"
            if "max" not in key and value < threshold:
                return f"{key}: {value} below minimum {threshold}"
        return None

    return [
        violation
        for key, threshold in gate.criteria.items()
        if (violation := violation_for(key, threshold)) is not None
    ]


def _record_bounded_history(history: BoundedList[dict[str, Any]], entry: dict[str, Any]) -> None:
    history.append(entry)


# ---------------------------------------------------------------------------
# Gate runner
# ---------------------------------------------------------------------------


class WorkflowGateRunner:
    """Evaluates workflow gates and determines actions.

    Usage::

        runner = WorkflowGateRunner()
        passed, action, violations = runner.evaluate(
            "post_execution",
            {"quality_score": 0.8, "no_critical_security": True},
        )
    """

    def __init__(self, gates: dict[str, WorkflowGate] | None = None):
        self._gates: dict[str, WorkflowGate] = gates if gates is not None else dict(WORKFLOW_GATES)
        self._history: BoundedList[dict[str, Any]] = BoundedList(MAX_GATE_HISTORY)

    # -- public API ---------------------------------------------------------

    def evaluate(
        self,
        stage: str,
        metrics: dict[str, Any],
    ) -> tuple[bool, GateAction, list[str]]:
        """Evaluate the gate for *stage* against *metrics*.

        Returns:
        -------
        passed : bool
            ``True`` when every criterion is satisfied.
        action : GateAction
            ``CONTINUE`` on success, otherwise the gate's ``failure_action``.
        violations : list[str]
            Human-readable descriptions of each failed criterion.

        Args:
            stage: The stage.
            metrics: The metrics.
        """
        gate = self._gates.get(stage)
        if gate is None:
            return self._unknown_stage_result(stage, metrics)

        violations = _gate_violations(gate, metrics)
        passed = len(violations) == 0
        action = GateAction.CONTINUE if passed else gate.failure_action
        self._record_gate_result(stage, gate.name, passed, action, violations, metrics)
        return passed, action, violations

    def _unknown_stage_result(self, stage: str, metrics: dict[str, Any]) -> tuple[bool, GateAction, list[str]]:
        violations = [f"Unknown workflow stage: {stage}"]
        self._record_gate_result(stage, None, False, GateAction.BLOCK, violations, metrics)
        logger.error("No gate defined for stage %r -- blocking by default", stage)
        return False, GateAction.BLOCK, violations

    def _record_gate_result(
        self,
        stage: str,
        gate_name: str | None,
        passed: bool,
        action: GateAction,
        violations: list[str],
        metrics: dict[str, Any],
    ) -> None:
        _record_bounded_history(
            self._history,
            {
                "stage": stage,
                "gate": gate_name,
                "passed": passed,
                "action": action.value,
                "violations": list(violations),
                "metrics": dict(metrics),
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        )
        if passed:
            logger.info("Gate %s PASSED for stage %s", gate_name, stage)
        else:
            logger.warning("Gate %s FAILED for stage %s: %s -> %s", gate_name, stage, violations, action.value)

    def get_history(self) -> list[dict[str, Any]]:
        """Return an ordered list of all gate evaluation results."""
        return list(self._history)

    # -- customisation helpers ----------------------------------------------

    def add_gate(self, stage: str, gate: WorkflowGate) -> None:
        """Register (or replace) a gate for *stage*.

        Args:
            stage: The stage.
            gate: The gate.
        """
        self._gates[stage] = gate

    def remove_gate(self, stage: str) -> WorkflowGate | None:
        """Remove and return the gate for *stage*, or ``None``."""
        return self._gates.pop(stage, None)

    @property
    def gates(self) -> dict[str, WorkflowGate]:
        """Read-only view of the current gate map."""
        return dict(self._gates)


# ---------------------------------------------------------------------------
# Singleton access
# ---------------------------------------------------------------------------

_gate_runner: WorkflowGateRunner | None = None
_gate_runner_lock = threading.Lock()


def get_gate_runner() -> WorkflowGateRunner:
    """Return the shared WorkflowGateRunner singleton (thread-safe).

    Creates the instance with the default WORKFLOW_GATES map on first call.
    Subsequent calls return the same runner, preserving any gates added via
    ``add_gate`` or removed via ``remove_gate`` at runtime.

    Returns:
        The shared WorkflowGateRunner instance.
    """
    global _gate_runner
    if _gate_runner is None:
        with _gate_runner_lock:
            if _gate_runner is None:
                _gate_runner = WorkflowGateRunner()
    return _gate_runner
