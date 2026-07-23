"""Registry data for the three-agent factory pipeline."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from vetinari.constants import AGENT_QUALITY_GATE_STRICT, QUALITY_GATE_HIGH
from vetinari.types import AgentType

ACTIVE_AGENT_TYPES: frozenset[AgentType] = frozenset({
    AgentType.FOREMAN,
    AgentType.WORKER,
    AgentType.INSPECTOR,
})

FOREMAN_MAX_TOKENS = 8192
WORKER_MAX_TOKENS = 32768
INSPECTOR_MAX_TOKENS = 4096


def build_agent_registry(
    agent_spec_type: type[Any],
    default_model: Callable[[AgentType, str | None], str],
) -> dict[AgentType, Any]:
    """Build AgentSpec instances without making registry data own the type."""
    return {
        AgentType.FOREMAN: _build_foreman_spec(agent_spec_type, default_model),
        AgentType.WORKER: _build_worker_spec(agent_spec_type, default_model),
        AgentType.INSPECTOR: _build_inspector_spec(agent_spec_type, default_model),
    }


def _build_foreman_spec(
    agent_spec_type: type[Any],
    default_model: Callable[[AgentType, str | None], str],
) -> Any:
    return agent_spec_type(
        agent_type=AgentType.FOREMAN,
        name="Foreman",
        description=("Planning, goal decomposition, Worker assignment, user interaction, context management"),
        default_model=default_model(AgentType.FOREMAN, None),
        thinking_variant="xhigh",
        modes=["plan", "clarify", "consolidate", "summarise", "prune", "extract"],
        jurisdiction=[
            "vetinari/agents/planner_agent.py",
            "vetinari/agents/contracts.py",
            "vetinari/core/",
        ],
        capabilities=[
            "goal_decomposition",
            "task_sequencing",
            "context_management",
            "user_clarification",
            "plan_consolidation",
            "dependency_resolution",
        ],
        can_delegate_to=[AgentType.WORKER.value, AgentType.INSPECTOR.value],
        max_delegation_depth=5,
        quality_gate_score=AGENT_QUALITY_GATE_STRICT,
        max_tokens=FOREMAN_MAX_TOKENS,
        timeout_seconds=600,
    )


def _build_worker_spec(
    agent_spec_type: type[Any],
    default_model: Callable[[AgentType, str | None], str],
) -> Any:
    return agent_spec_type(
        agent_type=AgentType.WORKER,
        name="Worker",
        description=(
            "Unified execution agent - research, architecture, build, and operations across 24 modes in 4 groups"
        ),
        default_model=default_model(AgentType.WORKER, None),
        thinking_variant="high",
        modes=[
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
        jurisdiction=[
            "vetinari/agents/builder_agent.py",
            "vetinari/agents/consolidated/",
            "vetinari/research/",
            "vetinari/architecture/",
            "vetinari/templates/",
            "docs/",
        ],
        capabilities=[
            "code_pattern_search",
            "domain_analysis",
            "api_documentation_lookup",
            "lateral_thinking",
            "ui_ux_design",
            "database_schema_design",
            "devops_pipeline_design",
            "git_workflow_analysis",
            "architecture_decision_support",
            "risk_and_tradeoff_analysis",
            "ontological_analysis",
            "contrarian_review",
            "code_scaffolding",
            "image_generation",
            "documentation_generation",
            "creative_writing",
            "cost_analysis",
            "experiment_management",
            "error_recovery",
            "synthesis",
            "improvement_suggestions",
            "monitoring",
            "reporting",
        ],
        can_delegate_to=[AgentType.INSPECTOR.value],
        max_delegation_depth=3,
        quality_gate_score=QUALITY_GATE_HIGH,
        max_tokens=WORKER_MAX_TOKENS,
        timeout_seconds=600,
    )


def _build_inspector_spec(
    agent_spec_type: type[Any],
    default_model: Callable[[AgentType, str | None], str],
) -> Any:
    return agent_spec_type(
        agent_type=AgentType.INSPECTOR,
        name="Inspector",
        description=(
            "Independent quality gate - code review, security audit, "
            "test generation, simplification. Gate decisions are authoritative."
        ),
        default_model=default_model(AgentType.INSPECTOR, "deep_audit"),
        thinking_variant="high",
        modes=[
            "code_review",
            "security_audit",
            "test_generation",
            "simplification",
        ],
        jurisdiction=[
            "vetinari/agents/consolidated/quality_agent.py",
            "tests/",
        ],
        capabilities=[
            "code_review",
            "security_audit",
            "test_generation",
            "code_simplification",
        ],
        can_delegate_to=[AgentType.WORKER.value],
        max_delegation_depth=2,
        quality_gate_score=AGENT_QUALITY_GATE_STRICT,
        max_tokens=INSPECTOR_MAX_TOKENS,
        timeout_seconds=300,
    )
