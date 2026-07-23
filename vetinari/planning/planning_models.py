"""Legacy planning dataclasses retained for backward compatibility."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from vetinari.planning.plan_types import Plan as CanonicalPlan
from vetinari.types import PlanStatus, StatusEnum
from vetinari.utils.serialization import dataclass_to_dict

# Decision: WaveStatus consolidated into StatusEnum in vetinari.types (ADR-0075).
# WaveStatus values are a subset of StatusEnum: PENDING, RUNNING, COMPLETED, FAILED, BLOCKED.
WaveStatus = StatusEnum  # Backward-compat alias — use StatusEnum directly in new code


@dataclass
class PlanTask:
    """A unit of work within a plan or wave."""

    task_id: str
    agent_type: str  # AgentType.value string — use AgentType enum at call sites
    description: str
    prompt: str
    status: str = StatusEnum.PENDING.value
    dependencies: list[str] = field(default_factory=list)
    assigned_agent: str = ""
    result: Any = None
    error: str = ""
    planned_start: str = ""
    planned_end: str = ""
    actual_start: str = ""
    actual_end: str = ""
    retry_count: int = 0
    priority: int = 5
    estimated_effort: float = 1.0
    parent_id: str = ""
    depth: int = 0
    max_depth: int = 14
    max_depth_override: int = 0
    subtasks: list[PlanTask] = field(default_factory=list)
    decomposition_seed: str = ""
    dod_level: str = "Standard"
    dor_level: str = "Standard"
    wave_id: str = ""

    def __repr__(self) -> str:
        """Show key identifying fields for debugging."""
        return f"PlanTask(task_id={self.task_id!r}, agent_type={self.agent_type!r}, status={self.status!r})"

    def to_dict(self) -> dict:
        """Serialize all fields to a JSON-serializable dict, with nested subtasks recursively expanded."""
        return dataclass_to_dict(self)

    @classmethod
    def from_dict(cls, data: dict) -> PlanTask:
        """From dict.

        Returns:
            The PlanTask result.
        """
        subtasks = [PlanTask.from_dict(t) for t in data.get("subtasks", [])]
        return cls(
            task_id=data.get("task_id", ""),
            agent_type=data.get("agent_type", "builder"),
            description=data.get("description", ""),
            prompt=data.get("prompt", ""),
            status=data.get("status", StatusEnum.PENDING.value),
            dependencies=data.get("dependencies", []),
            assigned_agent=data.get("assigned_agent", ""),
            result=data.get("result"),
            error=data.get("error", ""),
            planned_start=data.get("planned_start", ""),
            planned_end=data.get("planned_end", ""),
            actual_start=data.get("actual_start", ""),
            actual_end=data.get("actual_end", ""),
            retry_count=data.get("retry_count", 0),
            priority=data.get("priority", 5),
            estimated_effort=data.get("estimated_effort", 1.0),
            parent_id=data.get("parent_id", ""),
            depth=data.get("depth", 0),
            max_depth=data.get("max_depth", 14),
            max_depth_override=data.get("max_depth_override", 0),
            subtasks=subtasks,
            decomposition_seed=data.get("decomposition_seed", ""),
            dod_level=data.get("dod_level", "Standard"),
            dor_level=data.get("dor_level", "Standard"),
            wave_id=data.get("wave_id", ""),
        )


@dataclass
class Wave:
    """A group of tasks executed in parallel."""

    wave_id: str
    milestone: str
    description: str
    order: int
    status: str = WaveStatus.PENDING.value
    tasks: list[PlanTask] = field(default_factory=list)
    dependencies: list[str] = field(default_factory=list)

    def __repr__(self) -> str:
        """Show key identifying fields for debugging."""
        return f"Wave(wave_id={self.wave_id!r}, status={self.status!r}, order={self.order!r})"

    def to_dict(self) -> dict:
        """Serialize all fields to a JSON-serializable dict, with nested tasks recursively expanded."""
        return dataclass_to_dict(self)

    @classmethod
    def from_dict(cls, data: dict) -> Wave:
        """From dict.

        Returns:
            The Wave result.
        """
        tasks = [PlanTask.from_dict(t) for t in data.get("tasks", [])]
        return cls(
            wave_id=data.get("wave_id", ""),
            milestone=data.get("milestone", ""),
            description=data.get("description", ""),
            order=data.get("order", 1),
            status=data.get("status", WaveStatus.PENDING.value),
            tasks=tasks,
            dependencies=data.get("dependencies", []),
        )

    @property
    def completed_count(self) -> int:
        """Count the number of tasks in this wave that have completed successfully.

        Returns:
            Number of tasks with a completed status.
        """
        return sum(1 for t in self.tasks if t.status == StatusEnum.COMPLETED.value)

    @property
    def total_count(self) -> int:
        """Return the total number of tasks assigned to this wave.

        Returns:
            Total task count in the wave.
        """
        return len(self.tasks)


@dataclass
class PlanningExecutionPlan:
    """An execution plan containing ordered waves of tasks."""

    plan_id: str
    title: str
    prompt: str
    created_by: str
    created_at: str
    updated_at: str
    status: str = PlanStatus.PENDING.value
    waves: list[Wave] = field(default_factory=list)
    max_depth_override: int = 0
    seed_mix: str = "50% Oracle, 25% Researcher, 25% Explorer"
    seed_rate: int = 2
    decomposed_depth: int = 0
    adr_history: list[dict] = field(default_factory=list)
    template_version: str = "v1"

    def __repr__(self) -> str:
        """Show key identifying fields for debugging."""
        return f"ExecutionPlan(plan_id={self.plan_id!r}, status={self.status!r}, waves={len(self.waves)!r})"

    def to_dict(self) -> dict:
        """Serialize all dataclass fields plus computed progress metrics to a JSON-serializable dict.

        Returns:
            Dictionary of all persisted fields merged with ``total_tasks``,
            ``completed_tasks``, and ``progress_percent`` computed at call time.
        """
        data = dataclass_to_dict(self)
        data["total_tasks"] = self.total_tasks
        data["completed_tasks"] = self.completed_tasks
        data["progress_percent"] = self.progress_percent
        return data

    @classmethod
    def from_dict(cls, data: dict) -> PlanningExecutionPlan:
        """From dict.

        Returns:
            The ExecutionPlan result.
        """
        waves = [Wave.from_dict(w) for w in data.get("waves", [])]
        return cls(
            plan_id=data.get("plan_id", ""),
            title=data.get("title", ""),
            prompt=data.get("prompt", ""),
            created_by=data.get("created_by", ""),
            created_at=data.get("created_at", ""),
            updated_at=data.get("updated_at", ""),
            status=data.get("status", PlanStatus.PENDING.value),
            waves=waves,
            max_depth_override=data.get("max_depth_override", 0),
            seed_mix=data.get("seed_mix", "50% Oracle, 25% Researcher, 25% Explorer"),
            seed_rate=data.get("seed_rate", 2),
            decomposed_depth=data.get("decomposed_depth", 0),
            adr_history=data.get("adr_history", []),
            template_version=data.get("template_version", "v1"),
        )

    @property
    def total_tasks(self) -> int:
        """Return the total number of tasks across all waves in the plan.

        Returns:
            Aggregate task count summed from every wave.
        """
        return sum(len(w.tasks) for w in self.waves)

    @property
    def completed_tasks(self) -> int:
        """Return the number of completed tasks across all waves in the plan.

        Returns:
            Aggregate count of tasks with a completed status.
        """
        return sum(w.completed_count for w in self.waves)

    @property
    def progress_percent(self) -> float:
        """Calculate overall plan completion as a percentage.

        Returns:
            Percentage of completed tasks rounded to one decimal place,
            or 0.0 if the plan has no tasks.
        """
        if self.total_tasks == 0:
            return 0.0
        return round((self.completed_tasks / self.total_tasks) * 100, 1)

    @property
    def current_wave(self) -> Wave | None:
        """Return the wave that is currently running, if any.

        Returns:
            The first wave with a running status, or None if no wave is active.
        """
        for wave in self.waves:
            if wave.status == WaveStatus.RUNNING.value:
                return wave
        return None

    @property
    def effective_max_depth(self) -> int:
        """Return the effective maximum decomposition depth for this plan.

        When a max_depth_override is set, it is clamped to the range 12-16.
        Otherwise the default depth of 14 is used.

        Returns:
            The clamped override depth or the default depth of 14.
        """
        if self.max_depth_override > 0:
            return max(12, min(16, self.max_depth_override))
        return 14

    def add_adr(self, adr_id: str, title: str, context: str, decision: str, status: str = "proposed") -> dict[str, str]:
        """Record an Architecture Decision Record reference in this plan's history.

        Args:
            adr_id: Unique ADR identifier (e.g. ``ADR-0042``).
            title: Short human-readable title of the decision.
            context: Problem statement and constraints that prompted the decision.
            decision: The chosen approach.
            status: ADR lifecycle status (default ``proposed``).

        Returns:
            The newly created ADR dictionary with all fields plus a timestamp.
        """
        adr = {
            "adr_id": adr_id,
            "title": title,
            "context": context,
            "decision": decision,
            "status": status,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        self.adr_history.append(adr)
        return adr


# Backward-compatible export for callers that historically imported
# ``vetinari.planning.planning.Plan`` after the canonical Plan migration.
Plan = CanonicalPlan
