"""Unified Constraint Registry.

==============================
Central registry that aggregates ALL constraint types and provides a single
query API.  Loaded at startup as a singleton; injected into
``base_agent.prepare_task()`` as ``task.context["constraints"]``.

Usage::

    from vetinari.constraints.registry import get_constraint_registry

    registry = get_constraint_registry()
    ok, reason = registry.validate_delegation("WORKER", "INSPECTOR", depth=1)
    gate_ok, gate_reason = registry.check_quality_gate("WORKER", score=0.75)
    resource = registry.get_resource_constraint("WORKER")
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Any

from vetinari.constraints.architecture import (
    ARCHITECTURE_CONSTRAINTS,
    ArchitectureConstraint,
    validate_delegation,
)
from vetinari.constraints.resources import (
    AGENT_RESOURCE_LIMITS,
    ResourceConstraint,
    get_resource_constraint,
)
from vetinari.validation.quality_gates import (
    QUALITY_GATES,
    QualityGate,
    check_quality_gate,
    get_quality_gate,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class AgentConstraints:
    """All constraints applicable to a specific agent+mode combination."""

    agent_type: str
    mode: str | None = None
    architecture: ArchitectureConstraint | None = None
    resources: ResourceConstraint | None = None
    quality_gate: QualityGate | None = None

    def __repr__(self) -> str:
        return f"AgentConstraints(agent_type={self.agent_type!r}, mode={self.mode!r})"

    def to_dict(self) -> dict[str, Any]:
        """Serialize for injection into task context.

        Returns:
            Flat dictionary containing agent type, mode, and whichever
            resource limits, quality gate thresholds, and architecture
            constraints are present on this instance — suitable for
            embedding in ``task.context["constraints"]``.
        """
        result: dict[str, Any] = {"agent_type": self.agent_type, "mode": self.mode}
        if self.resources:
            result["max_tokens"] = self.resources.max_tokens
            result["timeout_seconds"] = self.resources.timeout_seconds
            result["max_retries"] = self.resources.max_retries
            result["max_cost_usd"] = self.resources.max_cost_usd
        if self.quality_gate:
            result["min_verification_score"] = self.quality_gate.min_verification_score
            result["require_schema_validation"] = self.quality_gate.require_schema_validation
        if self.architecture:
            result["max_delegation_depth"] = self.architecture.max_delegation_depth
            result["allowed_modes"] = self.architecture.allowed_modes
        return result


class ConstraintRegistry:
    """Central registry for all constraint types. Loaded at startup."""

    def __init__(self):
        self.architecture: dict[str, ArchitectureConstraint] = dict(ARCHITECTURE_CONSTRAINTS)
        self.resources: dict[str, ResourceConstraint] = dict(AGENT_RESOURCE_LIMITS)
        self.quality_gates: dict[str, QualityGate] = dict(QUALITY_GATES)
        self._violations: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        logger.debug(
            "ConstraintRegistry initialized: %d arch, %d resource, %d quality gate constraints",
            len(self.architecture),
            len(self.resources),
            len(self.quality_gates),
        )

    # ------------------------------------------------------------------
    # Query API
    # ------------------------------------------------------------------

    def get_constraints_for_agent(self, agent_type: str, mode: str | None = None) -> AgentConstraints:
        """Returns all applicable constraints for a specific agent+mode."""
        return AgentConstraints(
            agent_type=agent_type,
            mode=mode,
            architecture=self.architecture.get(agent_type),
            resources=get_resource_constraint(agent_type),
            quality_gate=get_quality_gate(agent_type, mode),
        )

    def get_resource_constraint(self, agent_type: str) -> ResourceConstraint:
        """Get resource constraints for an agent type."""
        return get_resource_constraint(agent_type)

    def validate_delegation(self, from_agent: str, to_agent: str, depth: int = 0) -> tuple[bool, str]:
        """Check if delegation is allowed by architecture constraints.

        Args:
            from_agent: The agent attempting to delegate.
            to_agent: The agent being delegated to.
            depth: Current delegation depth in the call chain.

        Returns:
            Tuple of ``(allowed, reason)`` where ``allowed`` is True when
            the delegation is permitted by architecture constraints, and
            ``reason`` explains the decision or violation.
        """
        allowed, reason = validate_delegation(from_agent, to_agent, depth)
        if not allowed:
            self._record_violation("delegation", from_agent, reason)
        return allowed, reason

    def check_quality_gate(self, agent_type: str, score: float, mode: str | None = None) -> tuple[bool, str]:
        """Check if output passes quality gate. Returns (passed, reason).

        Args:
            agent_type: The agent type to look up the gate for.
            score: The verification score to check (0.0-1.0).
            mode: Optional agent mode; used to select a mode-specific gate.

        Returns:
            Tuple of ``(passed, reason)`` where ``passed`` is True when
            ``score`` meets the minimum threshold for this agent/mode, and
            ``reason`` explains the result.
        """
        passed, reason = check_quality_gate(agent_type, score, mode)
        if not passed:
            self._record_violation("quality_gate", agent_type, reason)
        return passed, reason

    def validate_mode(self, agent_type: str, mode: str) -> tuple[bool, str]:
        """Check if a mode is valid for an agent type.

        Args:
            agent_type: The agent type to check.
            mode: The mode string to validate.

        Returns:
            Tuple of ``(allowed, reason)`` where ``allowed`` is True when
            the mode appears in the agent's ``allowed_modes`` list (or no
            restriction is defined), and ``reason`` explains the decision.
        """
        constraint = self.architecture.get(agent_type)
        if constraint is None:
            return True, "no architecture constraint defined"
        if not constraint.allowed_modes:
            return True, "no mode restrictions"
        if mode in constraint.allowed_modes:
            return True, f"mode '{mode}' is allowed"
        self._record_violation("mode", agent_type, f"mode '{mode}' not in allowed modes {constraint.allowed_modes}")
        return False, f"mode '{mode}' not allowed for {agent_type}"

    def validate_task_type(self, agent_type: str, task_type: str) -> tuple[bool, str]:
        """Check if a task type is valid for an agent.

        Args:
            agent_type: The agent type to check.
            task_type: The task type string to validate.

        Returns:
            Tuple of ``(allowed, reason)`` where ``allowed`` is True when
            the task type appears in the agent's ``allowed_task_types`` list
            (or no restriction is defined), and ``reason`` explains the
            decision.
        """
        constraint = self.architecture.get(agent_type)
        if constraint is None:
            return True, "no architecture constraint defined"
        if not constraint.allowed_task_types:
            return True, "no task type restrictions"
        if task_type in constraint.allowed_task_types:
            return True, f"task type '{task_type}' is allowed"
        self._record_violation(
            "task_type",
            agent_type,
            f"task type '{task_type}' not in allowed types {constraint.allowed_task_types}",
        )
        return False, f"task type '{task_type}' not allowed for {agent_type}"

    # ------------------------------------------------------------------
    # Violation tracking
    # ------------------------------------------------------------------

    def _record_violation(self, constraint_type: str, agent_type: str, details: str) -> None:
        """Record a constraint violation for audit purposes."""
        import time

        violation = {
            "constraint_type": constraint_type,
            "agent_type": agent_type,
            "details": details,
            "timestamp": time.time(),
        }
        with self._lock:
            self._violations.append(violation)
            # Keep last 1000 violations
            if len(self._violations) > 1000:
                self._violations = self._violations[-500:]
        logger.warning("[Constraint violation] %s: %s", constraint_type, details)

    def get_violation_stats(self) -> dict[str, int]:
        """Return violation counts by type.

        Returns:
            Mapping of constraint type (e.g. ``"delegation"``,
            ``"quality_gate"``, ``"mode"``) to the number of violations
            recorded for that type across all history.
        """
        with self._lock:
            stats: dict[str, int] = {}
            for v in self._violations:
                ct = v["constraint_type"]
                stats[ct] = stats.get(ct, 0) + 1
            return stats

    def clear_violations(self) -> None:
        """Clear violation history (for tests)."""
        with self._lock:
            self._violations.clear()


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_registry: ConstraintRegistry | None = None
_registry_lock = threading.Lock()


def get_constraint_registry() -> ConstraintRegistry:
    """Get or create the global constraint registry singleton.

    Returns:
        The shared ConstraintRegistry instance, initialized with all
        architecture, resource, and quality-gate constraints from their
        respective modules.
    """
    global _registry
    if _registry is None:
        with _registry_lock:
            if _registry is None:
                _registry = ConstraintRegistry()
    return _registry


def reset_constraint_registry() -> None:
    """Reset the singleton (for tests)."""
    global _registry
    with _registry_lock:
        _registry = None
