"""Quality Gates for Vetinari.

Unified quality gate system combining:
- Per-agent quality thresholds for the 3-agent factory pipeline (FOREMAN,
  WORKER, INSPECTOR) — enforcement via ``check_quality_gate()``.
- TESTER agent verification modes as quality gates between execution stages
  — orchestration via ``QualityGateRunner``.

Implementation is split across submodules to stay under the 550-line ceiling:
- ``gate_types``: shared enum/dataclass types (VerificationMode, GateResult, etc.)
- ``gate_checks``: check method implementations (_GateCheckRunner)
- this file: QualityGateRunner orchestration + threshold data + public API

All public names are re-exported here so existing callers do not need to
change their imports.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass
from typing import Any

from vetinari.constants import (
    CRITICAL_QUALITY_THRESHOLD,
    QUALITY_GATE_CRITICAL,
    QUALITY_GATE_HIGH,
    QUALITY_GATE_LOW,
    QUALITY_GATE_MEDIUM,
)
from vetinari.types import AgentType
from vetinari.validation.gate_checks import _GateCheckRunner
from vetinari.validation.gate_types import (
    GateCheckResult,
    GateResult,
    QualityGateConfig,
    VerificationMode,
)

logger = logging.getLogger(__name__)


# Re-export gate_types names so existing ``from vetinari.validation.quality_gates import X``
# callers continue to work without changes.
__all__ = [
    "QUALITY_GATES",
    "QUALITY_GATES_BY_CRITICALITY",
    "TASK_TYPE_CRITICALITY",
    "GateCheckResult",
    "GateResult",
    "QualityGate",
    "QualityGateConfig",
    "QualityGateRunner",
    "VerificationMode",
    "check_quality_gate",
    "get_criticality_for_task_type",
    "get_criticality_gate",
    "get_quality_gate",
]


class QualityGateRunner(_GateCheckRunner):
    """Runs quality gates between pipeline stages.

    Each pipeline stage (post_planning, post_execution, post_testing,
    pre_assembly) has a set of configured quality gates. The runner
    executes each gate's verification checks and collects results.

    The PIPELINE_GATES class variable defines the default gate
    configuration for each stage. Custom gates can be provided at
    construction time to override or extend defaults.

    The actual check implementations live in ``_GateCheckRunner``
    (``gate_checks.py``) to keep each module under 550 lines.
    """

    PIPELINE_GATES: dict[str, list[QualityGateConfig]] = {
        "post_planning": [
            QualityGateConfig(
                "architecture_check",
                VerificationMode.VERIFY_ARCHITECTURE,
                min_score=0.7,
            ),
        ],
        "post_execution": [
            QualityGateConfig(
                "quality_check",
                VerificationMode.VERIFY_QUALITY,
                min_score=0.6,
            ),
            QualityGateConfig(
                "security_check",
                VerificationMode.SECURITY,
                min_score=0.8,
                required=True,
            ),
        ],
        "post_testing": [
            QualityGateConfig(
                "coverage_check",
                VerificationMode.VERIFY_COVERAGE,
                min_score=0.5,
            ),
        ],
        "pre_assembly": [
            QualityGateConfig(
                "final_quality",
                VerificationMode.VERIFY_QUALITY,
                min_score=0.7,
            ),
            QualityGateConfig(
                "final_security",
                VerificationMode.SECURITY,
                min_score=0.9,
            ),
        ],
        "pre_execution": [
            QualityGateConfig(
                "prevention_check",
                VerificationMode.PRE_EXECUTION,
                min_score=0.8,
                required=True,
            ),
        ],
    }

    def __init__(self, custom_gates: dict[str, list[QualityGateConfig]] | None = None):
        """Initialize the runner with optional custom gate configurations.

        Args:
            custom_gates: Optional dict mapping stage names to gate configs.
                          Merges with (and overrides) the default PIPELINE_GATES.
        """
        self._gates: dict[str, list[QualityGateConfig]] = dict(self.PIPELINE_GATES)
        if custom_gates:
            self._gates.update(custom_gates)
        self._history: deque[GateCheckResult] = deque(maxlen=500)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run_gate(self, stage: str, artifacts: dict[str, Any] | None) -> list[GateCheckResult]:
        """Run all gates for a pipeline stage.

        Args:
            stage: Pipeline stage name (e.g. "post_execution").
            artifacts: Dictionary of artifacts to verify. Expected keys
                       depend on the gate mode but typically include
                       "code", "tests", "architecture", etc.

        Returns:
            List of GateCheckResult for each gate in the stage.
            Returns an empty list if the stage has no configured gates.
        """
        gate_configs = self._gates.get(stage, [])
        if not gate_configs:
            logger.debug("No gates configured for stage '%s'", stage)
            return []

        if not isinstance(artifacts, dict) or not artifacts:
            logger.warning("Quality gate '%s' failed closed: artifacts missing or malformed", stage)
            return [
                GateCheckResult(
                    gate_name=config.name,
                    mode=config.mode,
                    result=GateResult.FAILED,
                    score=0.0,
                    issues=[{"severity": "error", "message": "Required gate artifacts missing or malformed"}],
                    suggestions=["Provide non-empty gate artifacts before marking the stage passed"],
                    metadata={"stage": stage, "required": config.required},
                )
                for config in gate_configs
            ]

        results: list[GateCheckResult] = []
        for config in gate_configs:
            start = time.time()
            try:
                result = self._run_single_gate(config, artifacts)
            except Exception as exc:
                logger.error("Gate '%s' raised an exception: %s", config.name, exc)
                result = GateCheckResult(
                    gate_name=config.name,
                    mode=config.mode,
                    result=GateResult.FAILED,
                    score=0.0,
                    issues=[{"severity": "error", "message": f"Gate error: {exc}"}],
                )
            elapsed_ms = int((time.time() - start) * 1000)
            result.metadata["execution_time_ms"] = elapsed_ms
            result.metadata["stage"] = stage
            result.metadata["required"] = config.required

            results.append(result)
            self._history.append(result)

        return results

    def get_history(self) -> list[dict[str, Any]]:
        """Get the full history of gate check results.

        Returns:
            List of serialized GateCheckResult dictionaries.
        """
        return [r.to_dict() for r in self._history]

    def get_gates_for_stage(self, stage: str) -> list[QualityGateConfig]:
        """Return the gate configs for a given stage.

        Args:
            stage: Stage name to look up.

        Returns:
            List of QualityGateConfig for that stage (empty if none configured).
        """
        return list(self._gates.get(stage, []))

    def stage_passed(self, results: list[GateCheckResult]) -> bool:
        """Return True if all *required* gates in the results passed.

        A gate is considered passing if its result is PASSED or WARNING.
        Only gates marked as ``required`` can cause a stage failure.

        Args:
            results: List of GateCheckResult from run_gate().

        Returns:
            True if no required gate failed.
        """
        for r in results:
            is_required = r.metadata.get("required", True)
            if is_required and r.result == GateResult.FAILED:
                return False
        return True


# ---------------------------------------------------------------------------
# Per-agent quality thresholds (consolidated from constraints/quality_gates.py)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class QualityGate:
    """Quality threshold for a specific agent (optionally per-mode).

    Used by the 3-agent factory pipeline (FOREMAN, WORKER, INSPECTOR) to
    enforce minimum output quality before results are accepted.

    Attributes:
        agent_type: The AgentType value string.
        mode: Optional agent mode; when None the gate applies to all modes.
        min_verification_score: Minimum verification score to pass.
        min_heuristic_score: Minimum heuristic score to pass.
        max_retry_on_failure: Maximum retries before giving up.
        require_passing_verification: Whether verification must pass.
        require_schema_validation: Whether schema validation is required.
    """

    agent_type: str
    mode: str | None = None
    min_verification_score: float = 0.5
    min_heuristic_score: float = 0.3
    max_retry_on_failure: int = 2
    require_passing_verification: bool = True
    require_schema_validation: bool = True

    def __repr__(self) -> str:
        """Show key identifying fields for debugging."""
        return (
            f"QualityGate(agent_type={self.agent_type!r},"
            f" mode={self.mode!r},"
            f" min_verification_score={self.min_verification_score!r})"
        )


QUALITY_GATES: dict[str, QualityGate] = {
    AgentType.FOREMAN.value: QualityGate(
        agent_type=AgentType.FOREMAN.value,
        min_verification_score=CRITICAL_QUALITY_THRESHOLD,
        max_retry_on_failure=2,
    ),
    AgentType.WORKER.value: QualityGate(
        agent_type=AgentType.WORKER.value,
        min_verification_score=QUALITY_GATE_MEDIUM,
        max_retry_on_failure=2,
        require_passing_verification=True,
    ),
    AgentType.INSPECTOR.value: QualityGate(
        agent_type=AgentType.INSPECTOR.value,
        min_verification_score=1.0,  # zero-tolerance: any issue fails
        max_retry_on_failure=1,
    ),
    "INSPECTOR:security_audit": QualityGate(
        agent_type=AgentType.INSPECTOR.value,
        mode="security_audit",
        min_verification_score=1.0,  # zero-tolerance: any security issue fails
        max_retry_on_failure=1,
        require_schema_validation=True,
    ),
    "INSPECTOR:code_review": QualityGate(
        agent_type=AgentType.INSPECTOR.value,
        mode="code_review",
        min_verification_score=1.0,  # zero-tolerance: any code issue fails
        max_retry_on_failure=2,
    ),
    "WORKER:creative_writing": QualityGate(
        agent_type=AgentType.WORKER.value,
        mode="creative_writing",
        min_verification_score=QUALITY_GATE_LOW,
        max_retry_on_failure=2,
    ),
    "WORKER:architecture": QualityGate(
        agent_type=AgentType.WORKER.value,
        mode="architecture",
        min_verification_score=QUALITY_GATE_MEDIUM,
        max_retry_on_failure=1,
    ),
}

_DEFAULT_GATE = QualityGate(agent_type="DEFAULT", min_verification_score=QUALITY_GATE_LOW)

# Maps criticality level to quality thresholds and retry settings.
# Security tasks require CRITICAL threshold; simple lookups accept LOW.
QUALITY_GATES_BY_CRITICALITY: dict[str, dict] = {
    "critical": {"min_score": QUALITY_GATE_CRITICAL, "require_human_review": True, "max_retries": 1},
    "high": {"min_score": QUALITY_GATE_HIGH, "require_human_review": False, "max_retries": 2},
    "medium": {"min_score": QUALITY_GATE_MEDIUM, "require_human_review": False, "max_retries": 2},
    "low": {"min_score": QUALITY_GATE_LOW, "require_human_review": False, "max_retries": 3},
}


# Maps task types to criticality levels for per-task confidence thresholds.
# Security tasks require HIGH confidence; simple lookups accept LOW.
TASK_TYPE_CRITICALITY: dict[str, str] = {
    "security": "critical",
    "security_audit": "critical",
    "verification": "high",
    "review": "high",
    "testing": "high",
    "implementation": "medium",
    "analysis": "medium",
    "research": "medium",
    "scaffolding": "medium",
    "documentation": "low",
    "creative": "low",
    "general": "medium",
    "classification": "low",
    "lookup": "low",
}


def get_criticality_for_task_type(task_type: str) -> str:
    """Map a task type string to its criticality level.

    Args:
        task_type: The task type (e.g. ``"security"``, ``"implementation"``).

    Returns:
        Criticality level: ``"critical"``, ``"high"``, ``"medium"``, or ``"low"``.
    """
    return TASK_TYPE_CRITICALITY.get(task_type.lower(), "medium")


def get_criticality_gate(criticality: str) -> dict:
    """Get quality gate thresholds for a given task criticality level.

    Args:
        criticality: One of ``"critical"``, ``"high"``, ``"medium"``, ``"low"``.

    Returns:
        Dict with ``min_score``, ``require_human_review``, ``max_retries``.
    """
    return QUALITY_GATES_BY_CRITICALITY.get(criticality.lower(), QUALITY_GATES_BY_CRITICALITY["medium"])


def get_quality_gate(agent_type: str, mode: str | None = None) -> QualityGate:
    """Get the quality gate for an agent (optionally for a specific mode).

    Mode-specific gates take priority over agent-level gates.

    Args:
        agent_type: The agent type value string.
        mode: Optional mode to look up a mode-specific gate.

    Returns:
        The QualityGate for the given agent and mode.
    """
    if mode:
        mode_key = f"{agent_type}:{mode}"
        if mode_key in QUALITY_GATES:
            return QUALITY_GATES[mode_key]
    return QUALITY_GATES.get(agent_type, _DEFAULT_GATE)


def check_quality_gate(
    agent_type: str,
    score: float,
    mode: str | None = None,
    task_type: str | None = None,
) -> tuple[bool, str]:
    """Check if an output score passes the quality gate.

    When *task_type* is provided, the criticality-based threshold is also
    checked and the stricter of the two thresholds wins. This ensures
    security tasks require higher confidence than simple lookups.

    Args:
        agent_type: The agent type value string.
        score: The score to evaluate (0.0-1.0).
        mode: Optional mode for mode-specific threshold lookup.
        task_type: Optional task type for criticality-based threshold scaling.

    Returns:
        Tuple of (passed, reason_string).
    """
    gate = get_quality_gate(agent_type, mode)
    threshold = gate.min_verification_score

    # Apply criticality-based threshold when task_type is provided
    if task_type:
        criticality = get_criticality_for_task_type(task_type)
        crit_gate = get_criticality_gate(criticality)
        crit_threshold = crit_gate["min_score"]
        # Use the stricter (higher) of the two thresholds
        if crit_threshold > threshold:
            threshold = crit_threshold
            logger.debug(
                "Task type %s (%s criticality) raised threshold to %.2f",
                task_type,
                criticality,
                threshold,
            )

    passed = score >= threshold
    reason = (
        f"score {score:.2f} >= threshold {threshold:.2f}"
        if passed
        else f"score {score:.2f} below threshold {threshold:.2f} for {agent_type}"
    )

    # Log quality gate decision to audit trail (US-023)
    try:
        from vetinari.audit import get_audit_logger

        get_audit_logger().log_decision(
            decision_type="quality_gate",
            choice="pass" if passed else "fail",
            reasoning=reason,
            context={
                "agent_type": agent_type,
                "score": round(score, 3),
                "threshold": round(threshold, 3),
                "mode": mode or "",
                "task_type": task_type or "",
            },
        )
    except Exception:
        logger.warning("Audit logging failed during quality gate", exc_info=True)

    return passed, reason
