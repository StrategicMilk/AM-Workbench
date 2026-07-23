"""Vetinari Agent Contracts.

This module defines the canonical data contracts for Vetinari's hierarchical
multi-agent orchestration system. All agents and the Planner use these contracts.

Version: v0.1.0
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

from vetinari.agents.agent_registry import ACTIVE_AGENT_TYPES, build_agent_registry
from vetinari.agents.evidence_contracts import AttestedArtifact, LLMJudgment, OutcomeSignal, Provenance, ToolEvidence
from vetinari.constants import (
    DEFAULT_MAX_TOKENS,
    DEFAULT_QUALITY_THRESHOLD,
    SANDBOX_TIMEOUT,
)
from vetinari.types import (  # canonical source
    AgentType,
    InferenceStatus,
    StatusEnum,
)
from vetinari.utils.serialization import dataclass_to_dict

__all__ = [
    "ACTIVE_AGENT_TYPES",
    "AGENT_REGISTRY",
    "AgentResult",
    "AgentSpec",
    "AgentTask",
    "AttestedArtifact",
    "DecomposeDecision",
    "ExecutionPlan",
    "LLMJudgment",
    "OutcomeSignal",
    "Plan",
    "Provenance",
    "Task",
    "ToolEvidence",
    "VerificationResult",
    "get_agent_spec",
    "get_all_agent_specs",
    "get_enabled_agents",
]


def _default_model(agent_type: AgentType, mode: str | None = None) -> str:
    """Resolve the current catalog-backed default model for an agent."""
    from vetinari.config.agent_model_defaults import resolve

    return resolve(agent_type, mode=mode)


@dataclass(frozen=True, slots=True)
class AgentSpec:
    """Specification for an agent type."""

    agent_type: AgentType
    name: str
    description: str
    default_model: str
    thinking_variant: str = "medium"
    enabled: bool = True
    system_prompt: str = ""
    version: str = "1.0.0"
    # --- Extended fields (P5.5a) ---
    deprecated: bool = False
    replaced_by: str = ""
    jurisdiction: list[str] = field(default_factory=list)
    modes: list[str] = field(default_factory=list)
    capabilities: list[str] = field(default_factory=list)
    can_delegate_to: list[str] = field(default_factory=list)
    max_delegation_depth: int = 3
    quality_gate_score: float = DEFAULT_QUALITY_THRESHOLD
    max_tokens: int = DEFAULT_MAX_TOKENS
    timeout_seconds: int = SANDBOX_TIMEOUT
    # --- Budget enforcement fields (ADR-0075) ---
    token_budget: int = 32_000  # Maximum tokens per invocation
    iteration_cap: int = 10  # Maximum retry/iteration loops
    cost_budget_usd: float = 1.0  # Maximum cost in USD per invocation
    delegation_budget: int = 5  # Maximum recursive delegation depth
    org_level: int = 0  # Organizational hierarchy level (0=top)
    parent_agent_id: str = ""  # ID of the spawning agent, if any
    scope_id: str = ""  # Scope identifier for budget grouping
    # --- Agent instance tracking fields (plan item 7.1) ---
    agent_instance_id: str = ""  # UUID assigned at registration time; empty until registered
    children_ids: list[str] = field(default_factory=list)  # Instance IDs of spawned child agents

    def __repr__(self) -> str:
        return (
            f"AgentSpec(agent_type={self.agent_type.value!r}, name={self.name!r}, "
            f"model={self.default_model!r}, enabled={self.enabled!r}, version={self.version!r})"
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize the agent specification to a plain dictionary.

        Enum fields are converted to their string values for JSON compatibility.

        Returns:
            Dictionary representation of this AgentSpec with all fields.
        """
        return dataclass_to_dict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AgentSpec:
        """Reconstruct an AgentSpec from a dictionary representation.

        Missing keys fall back to dataclass defaults, allowing forward
        compatibility with older serialized specs.

        Args:
            data: Dictionary containing agent specification fields.
                Must include 'agent_type', 'name', 'description', and
                'default_model'.

        Returns:
            A new AgentSpec instance populated from the dictionary.
        """
        return cls(
            agent_type=AgentType(data["agent_type"]),
            name=data["name"],
            description=data["description"],
            default_model=data["default_model"],
            thinking_variant=data.get("thinking_variant", "medium"),
            enabled=data.get("enabled", True),
            system_prompt=data.get("system_prompt", ""),
            version=data.get("version", "1.0.0"),
            deprecated=data.get("deprecated", False),
            replaced_by=data.get("replaced_by", ""),
            jurisdiction=data.get("jurisdiction", []),
            modes=data.get("modes", []),
            capabilities=data.get("capabilities", []),
            can_delegate_to=data.get("can_delegate_to", []),
            max_delegation_depth=data.get("max_delegation_depth", 3),
            quality_gate_score=data.get("quality_gate_score", DEFAULT_QUALITY_THRESHOLD),
            max_tokens=data.get("max_tokens", DEFAULT_MAX_TOKENS),
            timeout_seconds=data.get("timeout_seconds", SANDBOX_TIMEOUT),
            token_budget=data.get("token_budget", 32_000),
            iteration_cap=data.get("iteration_cap", 10),
            cost_budget_usd=data.get("cost_budget_usd", 1.0),
            delegation_budget=data.get("delegation_budget", 5),
            org_level=data.get("org_level", 0),
            parent_agent_id=data.get("parent_agent_id", ""),
            scope_id=data.get("scope_id", ""),
            agent_instance_id=data.get("agent_instance_id", ""),
            children_ids=data.get("children_ids", []),
        )


@dataclass
class Task:
    """A task in the plan."""

    id: str
    description: str
    inputs: list[str] = field(default_factory=list)
    outputs: list[str] = field(default_factory=list)
    dependencies: list[str] = field(default_factory=list)
    assigned_agent: AgentType = AgentType.FOREMAN
    model_override: str = ""
    depth: int = 0
    parent_id: str = ""
    status: StatusEnum = StatusEnum.PENDING
    metadata: dict[str, Any] = field(default_factory=dict)

    def __repr__(self) -> str:
        return (
            f"Task(id={self.id!r}, status={self.status.value!r}, "
            f"agent={self.assigned_agent.value!r}, depth={self.depth!r})"
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize the task to a plain dictionary.

        Enum fields (assigned_agent, status) are converted to their string
        values for JSON-safe output.

        Returns:
            Dictionary representation of this Task with all fields.
        """
        return dataclass_to_dict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Task:
        """Reconstruct a Task from a dictionary representation.

        Handles enum coercion for assigned_agent and status fields, with
        sensible defaults for any missing optional keys.

        Args:
            data: Dictionary containing task fields. Must include 'id'
                and 'description' at minimum.

        Returns:
            A new Task instance populated from the dictionary.
        """
        return cls(
            id=data["id"],
            description=data["description"],
            inputs=data.get("inputs", []),
            outputs=data.get("outputs", []),
            dependencies=data.get("dependencies", []),
            assigned_agent=AgentType(data.get("assigned_agent", AgentType.FOREMAN.value)),
            model_override=data.get("model_override", ""),
            depth=data.get("depth", 0),
            parent_id=data.get("parent_id", ""),
            status=StatusEnum(data.get("status", StatusEnum.PENDING.value)),
            metadata=data.get("metadata", {}),
        )


@dataclass
class AgentTask:
    """A task assigned to a specific agent for execution."""

    task_id: str
    agent_type: AgentType
    description: str
    prompt: str
    mode: str = ""  # Execution mode hint (e.g. "research", "build", "review")
    status: StatusEnum = StatusEnum.PENDING
    result: Any = None
    error: str = ""
    started_at: str = ""
    completed_at: str = ""
    dependencies: list[str] = field(default_factory=list)
    context: dict[str, Any] = field(default_factory=dict)
    context_budget_tokens: int = 4096  # Max tokens for task context block

    def __repr__(self) -> str:
        return (
            f"AgentTask(task_id={self.task_id!r}, agent_type={self.agent_type.value!r}, "
            f"status={self.status.value!r}, error={self.error!r})"
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize the agent task to a plain dictionary.

        Enum fields (agent_type, status) are converted to their string
        values for JSON-safe output.

        Returns:
            Dictionary representation of this AgentTask with all fields.
        """
        return dataclass_to_dict(self)

    @classmethod
    def from_task(cls, task: Task, prompt: str) -> AgentTask:
        """Create an AgentTask from a Task."""
        return cls(
            task_id=task.id,
            agent_type=task.assigned_agent,
            description=task.description,
            prompt=prompt,
            dependencies=task.dependencies,
        )


@dataclass
class ExecutionPlan:
    """An execution-ready plan produced by the Planner for TwoLayerOrchestrator.

    This is the agent-contracts representation of a plan — it carries tasks
    as ``Task`` objects ready for agent dispatch, plus metadata for tracking
    execution progress (phase, results, completion timestamp).

    For the planning-domain Plan type (richer, includes Subtasks, risk levels,
    and definition-of-done), use ``vetinari.planning.plan_types.Plan``.
    """

    plan_id: str
    version: str = "v0.1.0"
    goal: str = ""
    phase: int = 0
    tasks: list[Task] = field(default_factory=list)
    model_scores: list[dict] = field(default_factory=list)
    notes: str = ""
    warnings: list[str] = field(default_factory=list)
    needs_context: bool = False
    follow_up_question: str = ""
    final_delivery_path: str = ""
    final_delivery_summary: str = ""
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    # Populated after execution with per-task results (task_id -> summary dict)
    results: dict[str, Any] = field(default_factory=dict)
    completed_at: str = ""
    original_plan: Any = None  # Reference to the planning-domain Plan if available

    def __repr__(self) -> str:
        return (
            f"ExecutionPlan(plan_id={self.plan_id!r}, phase={self.phase!r}, "
            f"tasks={len(self.tasks)}, needs_context={self.needs_context!r})"
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize the plan to a plain dictionary.

        Nested Task objects are recursively serialized; enum and datetime
        fields are converted to JSON-safe values.

        Returns:
            Dictionary representation of this ExecutionPlan, including serialized tasks.
        """
        return dataclass_to_dict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ExecutionPlan:
        """Reconstruct an ExecutionPlan from a dictionary representation.

        Nested task dictionaries are deserialized via Task.from_dict.
        Missing optional keys fall back to dataclass defaults.

        Args:
            data: Dictionary containing plan fields. Must include
                'plan_id' at minimum.

        Returns:
            A new ExecutionPlan instance with deserialized Task objects.
        """
        return cls(
            plan_id=data["plan_id"],
            version=data.get("version", "v0.1.0"),
            goal=data.get("goal", ""),
            phase=data.get("phase", 0),
            tasks=[Task.from_dict(t) for t in data.get("tasks", [])],
            model_scores=data.get("model_scores", []),
            notes=data.get("notes", ""),
            warnings=data.get("warnings", []),
            needs_context=data.get("needs_context", False),
            follow_up_question=data.get("follow_up_question", ""),
            final_delivery_path=data.get("final_delivery_path", ""),
            final_delivery_summary=data.get("final_delivery_summary", ""),
            created_at=data.get("created_at", datetime.now(timezone.utc).isoformat()),
            results=data.get("results", {}),
            completed_at=data.get("completed_at", ""),
        )

    @classmethod
    def create_new(cls, goal: str, phase: int = 0) -> ExecutionPlan:
        """Create a new execution plan with a unique ID."""
        return cls(plan_id=f"plan_{uuid.uuid4().hex[:8]}", goal=goal, phase=phase)


# Backward-compatible alias — callers may import Plan from contracts
Plan = ExecutionPlan


@dataclass
class AgentResult:
    """Result from an agent's execution.

    Enhanced with task tracking, status, issue reporting, and metric
    fields to support budget accounting and quality dashboards. Workers
    can also set ``escalated`` with a reason when a task is too large for
    their tier and must be re-judged by Foreman instead of failed directly.
    """

    success: bool
    output: str | dict[str, Any]
    metadata: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    provenance: list[dict] = field(default_factory=list)
    # -- Enhanced fields (session 2A) --
    task_id: str = ""  # Task this result corresponds to
    status: InferenceStatus = InferenceStatus.SUCCESS  # Outcome classification
    issues: list[dict[str, Any]] = field(default_factory=list)  # Quality issues
    metrics: dict[str, Any] = field(default_factory=dict)  # tokens, latency, cost
    output_type: str = ""  # Semantic type: "code", "plan", "report", etc.
    escalated: bool = False  # Worker asks Foreman to re-judge oversized scope
    escalation_reason: str = ""  # Why the Worker escalated instead of executing

    def __repr__(self) -> str:
        return (
            f"AgentResult(success={self.success!r}, task_id={self.task_id!r}, "
            f"status={self.status.value!r}, errors={self.errors!r})"
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize the agent result to a plain dictionary.

        Returns:
            Dictionary containing success status, output, metadata,
            errors, and provenance chain.
        """
        return dataclass_to_dict(self)


@dataclass(frozen=True, slots=True)
class DecomposeDecision:
    """Foreman's typed judgment about recursive decomposition.

    ``execute_here`` keeps the work at the current tier, ``decompose_further``
    authorizes a child plan or deeper decomposition, and ``escalate`` fails
    closed for depth caps, missing confidence signals, or human-review cases.
    Every decision should be recorded in a WorkReceipt by the caller so the
    action, reason, confidence, model id, and provenance can be audited later.

    Raises:
        ValueError: If ``reason`` is empty or ``confidence`` is outside
            the inclusive ``0.0`` to ``1.0`` range.
    """

    action: Literal["execute_here", "decompose_further", "escalate"]
    reason: str
    suggested_agent: AgentType | None = None
    confidence: float = 0.0
    model_id: str = ""
    provenance: Provenance | None = None

    def __post_init__(self) -> None:
        if not self.reason.strip():
            raise ValueError("DecomposeDecision.reason must not be empty")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("DecomposeDecision.confidence must be between 0.0 and 1.0")

    def __repr__(self) -> str:
        return f"DecomposeDecision(action={self.action!r}, reason={self.reason!r}, confidence={self.confidence!r})"


@dataclass
class VerificationResult:
    """Result from verification of an agent's output."""

    passed: bool
    issues: list[dict[str, Any]] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)
    score: float = 0.0

    def __repr__(self) -> str:
        return f"VerificationResult(passed={self.passed!r}, score={self.score!r}, issues={len(self.issues)})"

    def to_dict(self) -> dict[str, Any]:
        """Serialize the verification result to a plain dictionary.

        Returns:
            Dictionary containing pass/fail status, issues found,
            improvement suggestions, and the overall quality score.
        """
        return dataclass_to_dict(self)


AGENT_REGISTRY: dict[AgentType, AgentSpec] = build_agent_registry(AgentSpec, _default_model)


def get_agent_spec(agent_type: AgentType) -> AgentSpec | None:
    """Get the specification for an agent type, or None if not registered."""
    return AGENT_REGISTRY.get(agent_type)


def get_all_agent_specs() -> list[AgentSpec]:
    return list(AGENT_REGISTRY.values())


def get_enabled_agents() -> list[AgentSpec]:
    return [spec for spec in AGENT_REGISTRY.values() if spec.enabled]
