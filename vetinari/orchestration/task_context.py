"""Task Context Manifest — structured context gathering for agent prompts.

Assembles all relevant context for a task (dependency outputs, memory
snippets, constraints, goal metadata) into a single manifest that an agent
can consume as the beginning of its prompt.

The manifest is intentionally bounded — each section is capped to avoid
overwhelming small-context models.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


# Section size limits to keep manifest within context budget
MAX_DEPENDENCY_OUTPUTS = 10  # max dependency outputs to include
MAX_MEMORY_SNIPPETS = 5  # max memory items to include
MAX_OUTPUT_PREVIEW_CHARS = 500  # chars per dependency output preview
MAX_MEMORY_PREVIEW_CHARS = 300  # chars per memory snippet


@dataclass
class TaskContextManifest:
    """Bundled context for a single agent task.

    Carries everything an agent needs to understand its task in isolation:
    goal, dependency results, memory, constraints, and formatting hints.

    Args:
        task_id: Identifier of the target task.
        task_description: Human-readable description of what to do.
        goal: Top-level goal this task contributes to.
        dependency_outputs: Results from tasks this task depends on.
        memory_snippets: Relevant memory entries for this task.
        constraints: Constraints from the plan (budget, policy, etc.).
        context_budget_tokens: Approximate token budget for this context block.
        extra: Free-form extra data forwarded from the plan or request.
    """

    task_id: str
    task_description: str
    goal: str = ""
    dependency_outputs: list[dict[str, Any]] = field(default_factory=list)
    memory_snippets: list[dict[str, Any]] = field(default_factory=list)
    constraints: dict[str, Any] = field(default_factory=dict)
    context_budget_tokens: int = 4096
    extra: dict[str, Any] = field(default_factory=dict)

    def __repr__(self) -> str:
        return (
            f"TaskContextManifest(task_id={self.task_id!r}, "
            f"dependency_outputs={len(self.dependency_outputs)!r}, "
            f"memory_snippets={len(self.memory_snippets)!r})"
        )

    def format_for_prompt(self) -> str:
        """Render the manifest as a structured text block for agent prompts.

        Sections are rendered in order: goal, constraints, dependency
        outputs (capped at MAX_DEPENDENCY_OUTPUTS), memory snippets
        (capped at MAX_MEMORY_SNIPPETS).  Each dependency output is
        truncated to MAX_OUTPUT_PREVIEW_CHARS.

        Returns:
            Multi-line string ready to prepend to an agent's task prompt.
        """
        parts: list[str] = []

        if self.goal:
            parts.append(f"## Goal\n{self.goal}\n")

        parts.append(f"## Task\n{self.task_description}\n")

        if self.constraints:
            parts.append("## Constraints")
            for key, val in self.constraints.items():
                parts.append(f"  - {key}: {val}")
            parts.append("")

        if self.dependency_outputs:
            parts.append(f"## Dependency Outputs ({len(self.dependency_outputs)} items)")
            for i, dep in enumerate(self.dependency_outputs[:MAX_DEPENDENCY_OUTPUTS]):
                dep_id = dep.get("task_id", f"dep_{i}")
                output = str(dep.get("output", ""))
                if len(output) > MAX_OUTPUT_PREVIEW_CHARS:
                    output = output[:MAX_OUTPUT_PREVIEW_CHARS] + " [truncated]"
                parts.append(f"  [{dep_id}]: {output}")
            if len(self.dependency_outputs) > MAX_DEPENDENCY_OUTPUTS:
                omitted = len(self.dependency_outputs) - MAX_DEPENDENCY_OUTPUTS
                parts.append(f"  ... {omitted} more dependency outputs omitted")
            parts.append("")

        if self.memory_snippets:
            parts.append(f"## Relevant Memory ({len(self.memory_snippets)} items)")
            for _i, mem in enumerate(self.memory_snippets[:MAX_MEMORY_SNIPPETS]):
                mem_type = mem.get("type", "memory")
                content = str(mem.get("content", ""))
                if len(content) > MAX_MEMORY_PREVIEW_CHARS:
                    content = content[:MAX_MEMORY_PREVIEW_CHARS] + " [truncated]"
                metadata = self._format_memory_metadata(mem)
                parts.append(f"  [{mem_type}{metadata}]: {content}")
            parts.append("")

        return "\n".join(parts)

    @staticmethod
    def _format_memory_metadata(memory: dict[str, Any]) -> str:
        """Render provenance and quality metadata for a memory snippet."""
        fields: list[str] = []
        for key, label in (
            ("memory_id", "id"),
            ("id", "id"),
            ("source", "source"),
            ("source_ref", "source"),
            ("quality_score", "quality"),
            ("score", "quality"),
            ("timestamp", "timestamp"),
            ("created_at", "timestamp"),
        ):
            value = memory.get(key)
            if value in (None, ""):
                continue
            rendered = f"{label}={value}"
            if rendered not in fields:
                fields.append(rendered)
        return f" {'; '.join(fields)}" if fields else ""

    @classmethod
    def create_for_task(
        cls,
        task_id: str,
        task_description: str,
        goal: str = "",
        completed_results: dict[str, Any] | None = None,
        dependency_ids: list[str] | None = None,
        memory: list[dict[str, Any]] | None = None,
        constraints: dict[str, Any] | None = None,
        context_budget_tokens: int = 4096,
        extra: dict[str, Any] | None = None,
    ) -> TaskContextManifest:
        """Create a manifest by extracting relevant context from completed results.

        Filters ``completed_results`` to only include outputs from tasks in
        ``dependency_ids``, capping at MAX_DEPENDENCY_OUTPUTS.

        Args:
            task_id: The task this manifest is created for.
            task_description: Human-readable task description.
            goal: Top-level goal string.
            completed_results: Dict mapping task_id -> result dict for all
                completed tasks in the plan.
            dependency_ids: List of task IDs this task depends on.
            memory: Memory snippets relevant to this task.
            constraints: Plan-level constraints forwarded to the agent.
            context_budget_tokens: Approximate token budget for the block.
            extra: Additional free-form context.

        Returns:
            Populated TaskContextManifest ready for format_for_prompt().
        """
        completed_results = completed_results or {}
        dependency_ids = dependency_ids or []
        memory = memory or []
        constraints = constraints or {}
        extra = extra or {}

        # Gather outputs only for declared dependencies
        dep_outputs: list[dict[str, Any]] = []
        for dep_id in dependency_ids[:MAX_DEPENDENCY_OUTPUTS]:
            result = completed_results.get(dep_id)
            if result is not None:
                dep_outputs.append({"task_id": dep_id, "output": result})
            else:
                logger.debug(
                    "[TaskContextManifest] Dependency %s not yet in completed_results for task %s",
                    dep_id,
                    task_id,
                )

        logger.debug(
            "[TaskContextManifest] Created manifest for task %s: %d deps, %d memory",
            task_id,
            len(dep_outputs),
            len(memory),
        )

        return cls(
            task_id=task_id,
            task_description=task_description,
            goal=goal,
            dependency_outputs=dep_outputs,
            memory_snippets=memory[:MAX_MEMORY_SNIPPETS],
            constraints=constraints,
            context_budget_tokens=context_budget_tokens,
            extra=extra,
        )


# Backward compatibility alias
TaskManifestContext = TaskContextManifest
TaskContextManifest.build_for_task = TaskContextManifest.create_for_task
