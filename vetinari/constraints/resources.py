"""Per-Agent Resource Constraints.

================================
Defines token limits, retry budgets, timeouts, cost caps, and parallelism
limits for each agent type in the 3-agent factory pipeline
(FOREMAN, WORKER, INSPECTOR).

Enforcement points:
- ``base_agent.prepare_task()``: inject resource constraints into task context
- ``base_agent._infer()``: check token limit before LLM call
- ``DurableExecutionEngine``: check timeout + cost budget per task node
- ``TwoLayerOrchestrator``: check delegation call count per agent
"""

from __future__ import annotations

from dataclasses import dataclass

from vetinari.constants import MAX_TOKENS_FOREMAN, MAX_TOKENS_INSPECTOR, RESOURCE_MAX_TOKENS_WORKER
from vetinari.types import AgentType


@dataclass(frozen=True, slots=True)
class ResourceConstraint:
    """Per-agent resource limits."""

    agent_type: str  # AgentType.value
    max_tokens: int = 4096
    max_retries: int = 3
    timeout_seconds: int = 120
    max_cost_usd: float = 0.50
    max_delegation_calls: int = 5
    max_parallel_tasks: int = 3

    def __repr__(self) -> str:
        return f"ResourceConstraint(agent_type={self.agent_type!r}, max_tokens={self.max_tokens!r}, timeout_seconds={self.timeout_seconds!r})"


# ---------------------------------------------------------------------------
# Default resource limits per agent (3-agent factory pipeline)
# ---------------------------------------------------------------------------

AGENT_RESOURCE_LIMITS: dict[str, ResourceConstraint] = {
    # Orchestration — needs reasonable context for planning and coordination
    AgentType.FOREMAN.value: ResourceConstraint(
        agent_type=AgentType.FOREMAN.value,
        max_tokens=MAX_TOKENS_FOREMAN,
        timeout_seconds=180,
        max_delegation_calls=10,
        max_parallel_tasks=1,
    ),
    # Execution — generous limits since Worker handles all implementation work
    AgentType.WORKER.value: ResourceConstraint(
        agent_type=AgentType.WORKER.value,
        max_tokens=RESOURCE_MAX_TOKENS_WORKER,
        max_retries=3,
        timeout_seconds=300,
        max_delegation_calls=3,
        max_parallel_tasks=3,
    ),
    # Verification — tighter limits; Inspector reviews, not generates
    AgentType.INSPECTOR.value: ResourceConstraint(
        agent_type=AgentType.INSPECTOR.value,
        max_tokens=MAX_TOKENS_INSPECTOR,
        timeout_seconds=120,
        max_retries=2,
        max_delegation_calls=2,
        max_parallel_tasks=2,
    ),
}

# Default for any agent type not explicitly listed
_DEFAULT_CONSTRAINT = ResourceConstraint(agent_type="DEFAULT")


def get_resource_constraint(agent_type: str) -> ResourceConstraint:
    """Get resource constraints for an agent type (returns defaults if unlisted)."""
    return AGENT_RESOURCE_LIMITS.get(agent_type, _DEFAULT_CONSTRAINT)
