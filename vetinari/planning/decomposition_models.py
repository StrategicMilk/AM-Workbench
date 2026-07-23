"""Models and criteria for task decomposition."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

# Decomposition configuration knobs
DEFAULT_MAX_DEPTH = 14
MIN_MAX_DEPTH = 12
MAX_MAX_DEPTH = 16
SEED_RATE = 0.3  # 30% of tasks seeded with refined subtasks
SEED_MIX = 0.5  # Balance between breadth and depth seeding

# Definition of Done criteria per level
_DOD_CRITERIA = {
    "Light": [
        "Code compiles without errors",
        "Basic functionality works",
        "No blocking security issues",
    ],
    "Standard": [
        "Code compiles and lints cleanly",
        "Unit tests pass (>70% coverage)",
        "Security scan passes",
        "Documentation updated",
        "Code reviewed",
    ],
    "Hard": [
        "Code compiles, lints, and type-checks",
        "Unit + integration tests pass (>85% coverage)",
        "Security scan passes with no high/critical findings",
        "Full API documentation generated",
        "Performance benchmarks met",
        "Accessibility audit passed",
        "Peer reviewed and approved",
    ],
}

# Definition of Ready criteria
_DOR_CRITERIA = {
    "Light": [
        "Task description is clear",
        "Inputs are defined",
    ],
    "Standard": [
        "Task description is unambiguous",
        "Inputs and expected outputs defined",
        "Dependencies identified",
        "Estimated effort provided",
    ],
    "Hard": [
        "Task description is unambiguous and reviewed",
        "All inputs, outputs, and side-effects documented",
        "Dependencies fully resolved",
        "Effort estimate reviewed by at least one peer",
        "Acceptance criteria agreed upon",
        "Risk assessment completed",
    ],
}


@dataclass(frozen=True, slots=True)
class SubtaskSpec:
    """A single decomposed subtask."""

    subtask_id: str
    parent_task_id: str
    description: str
    agent_type: str
    depth: int
    inputs: list[str] = field(default_factory=list)
    outputs: list[str] = field(default_factory=list)
    dependencies: list[str] = field(default_factory=list)
    dod_criteria: list[str] = field(default_factory=list)
    dor_criteria: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def __repr__(self) -> str:
        return f"SubtaskSpec(subtask_id={self.subtask_id!r}, agent_type={self.agent_type!r}, depth={self.depth!r})"


@dataclass(frozen=True, slots=True)
class DecompositionEvent:
    """A historical decomposition event."""

    event_id: str
    plan_id: str
    task_id: str
    depth: int
    seeds_used: list[str]
    subtasks_created: int
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def __repr__(self) -> str:
        return (
            f"DecompositionEvent(event_id={self.event_id!r}, plan_id={self.plan_id!r}, "
            f"task_id={self.task_id!r}, depth={self.depth!r}, "
            f"subtasks_created={self.subtasks_created!r})"
        )
