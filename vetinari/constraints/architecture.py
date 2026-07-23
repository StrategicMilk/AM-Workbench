"""Architecture Constraints.

=========================
Defines delegation rules, mode-to-agent validation, and task-type-to-agent
validation.  These constraints enforce the 3-agent factory pipeline:
FOREMAN (orchestration) → WORKER (execution) → INSPECTOR (verification).

Key rules:
- FOREMAN can delegate to WORKER and INSPECTOR, max depth 5.
- No agent may delegate to FOREMAN (prevents circular orchestration).
- INSPECTOR has special permission to request re-work from WORKER
  (maker-checker pattern, max 1 iteration).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from vetinari.guards import GateError
from vetinari.types import AgentType


@dataclass
class ArchitectureConstraint:
    """Delegation and mode constraints for a single agent type."""

    agent_type: str  # AgentType.value
    can_delegate_to: list[str] = field(default_factory=list)
    cannot_delegate_to: list[str] = field(default_factory=list)
    max_delegation_depth: int = 3
    allowed_modes: list[str] = field(default_factory=list)
    allowed_task_types: list[str] = field(default_factory=list)

    def __repr__(self) -> str:
        return (
            f"ArchitectureConstraint(agent_type={self.agent_type!r}, "
            f"can_delegate_to={self.can_delegate_to!r}, "
            f"max_delegation_depth={self.max_delegation_depth!r})"
        )


# ---------------------------------------------------------------------------
# Default architecture constraints (3-agent factory pipeline)
# ---------------------------------------------------------------------------

ARCHITECTURE_CONSTRAINTS: dict[str, ArchitectureConstraint] = {
    # Core orchestration — plans, clarifies, consolidates; delegates to WORKER and INSPECTOR
    AgentType.FOREMAN.value: ArchitectureConstraint(
        agent_type=AgentType.FOREMAN.value,
        can_delegate_to=[AgentType.WORKER.value, AgentType.INSPECTOR.value],
        cannot_delegate_to=[],
        max_delegation_depth=5,
        allowed_modes=["plan", "clarify", "consolidate", "summarise", "prune", "extract"],
        allowed_task_types=["planning", "analysis", "general", "clarification", "memory", "monitoring"],
    ),
    # Execution — handles all implementation, research, and specialist work
    AgentType.WORKER.value: ArchitectureConstraint(
        agent_type=AgentType.WORKER.value,
        can_delegate_to=[],
        cannot_delegate_to=[AgentType.FOREMAN.value],
        max_delegation_depth=3,  # Matches contracts.py AGENT_SPECS canonical value
        allowed_modes=[
            "code_discovery",
            "domain_research",
            "api_lookup",
            "lateral_thinking",
            "ui_design",
            "database",
            "devops",
            "git_workflow",
            "architecture",
            "risk_assessment",
            "ontological_analysis",
            "contrarian_review",
            "suggest",
            "build",
            "image_generation",
            "documentation",
            "creative_writing",
            "cost_analysis",
            "experiment",
            "error_recovery",
            "synthesis",
            "improvement",
            "monitor",
            "devops_ops",
        ],
        allowed_task_types=[
            "implementation",
            "coding",
            "testing",
            "research",
            "analysis",
            "ui",
            "design",
            "data",
            "architecture",
            "infrastructure",
            "documentation",
            "creative",
            "operations",
            "error_recovery",
            "cost_analysis",
        ],
    ),
    # Verification — maker-checker: can request re-work from WORKER
    AgentType.INSPECTOR.value: ArchitectureConstraint(
        agent_type=AgentType.INSPECTOR.value,
        can_delegate_to=[AgentType.WORKER.value],
        cannot_delegate_to=[AgentType.FOREMAN.value],
        max_delegation_depth=2,  # Matches contracts.py AGENT_SPECS canonical value
        allowed_modes=["code_review", "security_audit", "test_generation", "simplification"],
        allowed_task_types=["verification", "code_review", "testing", "security"],
    ),
}


# ---------------------------------------------------------------------------
# Loop prevention and cost-aware delegation constraints
# ---------------------------------------------------------------------------

MAX_AGENT_RETRIES_PER_TASK = 3  # Prevent infinite retry loops
MAX_DELEGATION_CHAIN_LENGTH = 5  # Max depth already enforced per-agent above
PREFER_LOCAL_FOR_SIMPLE_TASKS = True  # Route simple tasks to local models
CLOUD_ESCALATION_THRESHOLD = 0.7  # Escalate to cloud when local quality < 0.7


def validate_delegation(from_agent: str, to_agent: str, current_depth: int = 0) -> tuple[bool, str]:
    """Check if delegation from one agent to another is allowed.

    Returns ``(allowed, reason)`` where *reason* explains the denial.

    Args:
        from_agent: The from agent.
        to_agent: The to agent.
        current_depth: The current depth.

    Returns:
        True if successful, False otherwise.

    Raises:
        GateError: If either agent type is outside the architecture registry.
    """
    constraint = ARCHITECTURE_CONSTRAINTS.get(from_agent)
    if constraint is None:
        raise GateError("architecture_delegation", f"unknown agent type: {from_agent!r}")

    if to_agent in constraint.cannot_delegate_to:
        return False, f"{from_agent} is explicitly forbidden from delegating to {to_agent}"

    if constraint.can_delegate_to and to_agent not in constraint.can_delegate_to:
        return False, f"{to_agent} is not in {from_agent}'s allowed delegation list"

    if current_depth >= constraint.max_delegation_depth:
        return False, f"delegation depth {current_depth} exceeds max {constraint.max_delegation_depth} for {from_agent}"

    return True, "allowed"


def get_constraint(agent_type: str) -> ArchitectureConstraint | None:
    return ARCHITECTURE_CONSTRAINTS.get(agent_type)
