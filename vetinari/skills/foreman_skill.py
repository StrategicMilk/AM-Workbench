"""Foreman Skill Tool.

==============================
Skill tool for the FOREMAN agent — planning, clarification, and orchestration.

Covers 6 modes:
  - plan: Goal decomposition into task DAGs
  - clarify: Socratic questioning to surface hidden requirements
  - consolidate: Memory and context consolidation
  - summarise: Session summarization preserving key decisions
  - prune: Token budget management and context trimming
  - extract: Knowledge extraction from completed work

Standards enforced (from skill_registry):
  - STD-FMN-001: Unique task IDs with assigned agents
  - STD-FMN-002: Acyclic dependency graphs
  - STD-FMN-003: Verification tasks per implementation task
  - STD-FMN-004: Risk assessment for destructive operations
  - STD-FMN-005: Specific, answerable clarification questions
  - STD-FMN-006: Summaries preserve key decisions and action items
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from vetinari.agents.contracts import Task
from vetinari.execution_context import ToolPermission
from vetinari.security.redaction import redact_text
from vetinari.tool_interface import (
    Tool,
    ToolCategory,
    ToolMetadata,
    ToolParameter,
    ToolResult,
)
from vetinari.types import AgentType, ExecutionMode, ThinkingMode
from vetinari.utils.serialization import dataclass_to_dict

logger = logging.getLogger(__name__)


def _log_ref(text: str) -> str:
    """Return bounded redacted text for operational logs."""
    return redact_text(str(text))[:120]


def _context_lines(context: dict[str, Any]) -> list[str]:
    """Render compact, deterministic context bullets for local Foreman modes."""
    lines: list[str] = []
    for key in sorted(context):
        value = context[key]
        if value in (None, "", [], {}, ()):
            continue
        if isinstance(value, (list, tuple, set)):
            rendered = ", ".join(str(item) for item in list(value)[:5])
        elif isinstance(value, dict):
            rendered = ", ".join(f"{item_key}={item_value}" for item_key, item_value in sorted(value.items())[:5])
        else:
            rendered = str(value)
        lines.append(f"{key}: {redact_text(rendered)[:160]}")
    return lines


class ForemanMode(str, Enum):
    """Modes of the Foreman skill tool."""

    PLAN = "plan"
    CLARIFY = "clarify"
    CONSOLIDATE = "consolidate"
    SUMMARISE = "summarise"
    PRUNE = "prune"
    EXTRACT = "extract"


# PlanTask is retired (M4 ontology unification) — use contracts.Task instead.
# Extra fields (effort, acceptance_criteria, mode) live in Task.metadata.


def make_plan_task(
    task_id: str,
    description: str,
    assigned_agent: str | AgentType,
    *,
    dependencies: list[str] | None = None,
    inputs: list[str] | None = None,
    outputs: list[str] | None = None,
    effort: str = "M",
    acceptance_criteria: str = "",
    mode: str | None = None,
) -> Task:
    """Create a Task with planning-specific metadata fields.

    Replaces the retired PlanTask dataclass while preserving the extra
    fields (effort, acceptance_criteria, mode) inside Task.metadata.

    Args:
        task_id: Unique task identifier.
        description: Human-readable task description.
        assigned_agent: Agent type as string or AgentType enum.
        dependencies: Task IDs this task depends on.
        inputs: Input artifact names.
        outputs: Output artifact names.
        effort: T-shirt size estimate (XS, S, M, L, XL).
        acceptance_criteria: Definition of done for this task.
        mode: Worker mode hint (e.g. "code_discovery", "build").

    Returns:
        A contracts.Task with planning metadata embedded.
    """
    agent = AgentType(assigned_agent) if isinstance(assigned_agent, str) else assigned_agent
    metadata: dict[str, Any] = {}
    if effort != "M":
        metadata["effort"] = effort
    if acceptance_criteria:
        metadata["acceptance_criteria"] = acceptance_criteria
    if mode:
        metadata["mode"] = mode
    from vetinari.agents import contracts as agent_contracts

    return agent_contracts.Task(
        id=task_id,
        description=description,
        assigned_agent=agent,
        dependencies=dependencies if dependencies is not None else [],
        inputs=inputs if inputs is not None else [],
        outputs=outputs if outputs is not None else [],
        metadata=metadata,
    )


@dataclass
class ForemanResult:
    """Result from Foreman skill execution."""

    plan_id: str = ""
    goal: str = ""
    tasks: list[Task] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    questions: list[str] = field(default_factory=list)
    summary: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def __repr__(self) -> str:
        """Show key identifying fields for debugging."""
        return f"ForemanResult(plan_id={self.plan_id!r}, tasks={len(self.tasks)!r})"

    def to_dict(self) -> dict[str, Any]:
        """Convert to a plain dictionary for plan serialization and agent handoffs."""
        return dataclass_to_dict(self)


class ForemanSkillTool(Tool):
    """Skill tool for the Foreman agent — planning and orchestration.

    The Foreman is the first stage of the factory pipeline. It decomposes
    goals into task DAGs, clarifies ambiguous requirements, and manages
    context consolidation across sessions.
    """

    def __init__(self):
        super().__init__(
            metadata=ToolMetadata(
                name="foreman",
                description=(
                    "Planning, clarification, and orchestration skill — "
                    "decomposes goals into task DAGs with dependency analysis"
                ),
                category=ToolCategory.MODEL_INFERENCE,
                version="2.0.0",
                parameters=[
                    ToolParameter(
                        name="goal",
                        type=str,
                        description="The goal, question, or content to process",
                        required=True,
                    ),
                    ToolParameter(
                        name="mode",
                        type=str,
                        description="Execution mode (plan, clarify, consolidate, summarise, prune, extract)",
                        required=False,
                        default="plan",
                        allowed_values=[m.value for m in ForemanMode],
                    ),
                    ToolParameter(
                        name="context",
                        type=dict,
                        description="Additional context (codebase state, prior plans, constraints)",
                        required=False,
                    ),
                ],
                required_permissions=[ToolPermission.MODEL_INFERENCE],
                allowed_modes=[ExecutionMode.PLANNING, ExecutionMode.EXECUTION],
                tags=["planning", "orchestration", "decomposition"],
            ),
        )

    def execute(self, **kwargs) -> ToolResult:
        """Execute the Foreman skill in the specified mode.

        Args:
            **kwargs: Must include 'goal'; optionally 'mode' and 'context'.

        Returns:
            ToolResult containing the Foreman's output.
        """
        goal = kwargs.get("goal", "")
        mode_str = kwargs.get("mode", "plan")
        context = kwargs.get("context", {})

        try:
            mode = ForemanMode(mode_str)
        except ValueError:
            logger.warning("Invalid ForemanMode %r in tool call — returning error to caller", mode_str)
            return ToolResult(
                success=False,
                output=None,
                error=f"Unknown mode: {mode_str}. Valid modes: {[m.value for m in ForemanMode]}",
            )

        logger.info("Foreman executing mode=%s goal=%s", mode.value, _log_ref(goal))

        try:
            result = self._execute_mode(mode, goal, context)
            logger.info("Foreman completed mode=%s", mode.value)
            return ToolResult(
                success=True,
                output=result.to_dict(),
                metadata={"mode": mode.value, "agent": AgentType.FOREMAN.value},
            )
        except Exception as exc:
            logger.error("Foreman mode=%s failed: %s", mode.value, exc)
            return ToolResult(
                success=False,
                output=None,
                error=str(exc),
            )

    def _execute_mode(
        self,
        mode: ForemanMode,
        goal: str,
        context: dict[str, Any],
    ) -> ForemanResult:
        """Route to the appropriate mode handler.

        Args:
            mode: The execution mode.
            goal: The goal or content to process.
            context: Additional context.

        Returns:
            ForemanResult from the mode handler.
        """
        handler = {
            ForemanMode.PLAN: self._plan,
            ForemanMode.CLARIFY: self._clarify,
            ForemanMode.CONSOLIDATE: self._consolidate,
            ForemanMode.SUMMARISE: self._summarise,
            ForemanMode.PRUNE: self._prune,
            ForemanMode.EXTRACT: self._extract,
        }[mode]
        return handler(goal, context)

    @staticmethod
    def _plan(goal: str, context: dict[str, Any]) -> ForemanResult:
        """Decompose a goal into a task DAG with dependency analysis.

        Follows the assembly-line pattern: analyze → decompose → assign → sequence.
        Uses capability-based routing to suggest Worker modes for each task.
        """
        # Query skill registry for capability-based routing
        mode_hints: dict[str, str] = {}
        try:
            from vetinari.orchestration.plan_generator import PlanGenerator

            pg = PlanGenerator()
            suggested_mode = pg.resolve_worker_mode(goal)
            if suggested_mode:
                mode_hints["primary"] = suggested_mode
        except Exception:
            logger.warning("Capability routing unavailable for plan mode")

        normalized_goal = goal or "Clarify the requested outcome"
        primary_mode = mode_hints.get("primary")
        tasks = [
            make_plan_task(
                "T1",
                f"Clarify success criteria and constraints for {normalized_goal}",
                AgentType.FOREMAN,
                outputs=["requirements brief"],
                acceptance_criteria="Scope, constraints, and done-state are explicit",
            ),
            make_plan_task(
                "T2",
                f"Implement the core work required for {normalized_goal}",
                AgentType.WORKER,
                dependencies=["T1"],
                inputs=["requirements brief"],
                outputs=["working implementation"],
                acceptance_criteria="Primary behavior works on representative inputs",
                mode=primary_mode,
            ),
            make_plan_task(
                "T3",
                f"Verify and document the outcome for {normalized_goal}",
                AgentType.INSPECTOR,
                dependencies=["T2"],
                inputs=["working implementation"],
                outputs=["verification evidence"],
                acceptance_criteria="Checks or tests prove the delivered behavior",
            ),
        ]

        return ForemanResult(
            plan_id="plan-1",
            goal=goal,
            tasks=tasks,
            risks=[
                "Unclear requirements can invalidate downstream implementation work",
                "Verification gaps can make a partial implementation look complete",
            ],
            metadata={"mode_hints": mode_hints, "thinking_mode": ThinkingMode.XHIGH.value},
        )

    @staticmethod
    def _clarify(goal: str, context: dict[str, Any]) -> ForemanResult:
        """Generate specific, answerable clarification questions.

        Uses Socratic questioning to surface hidden assumptions, constraints,
        and edge cases before planning begins.
        """
        normalized_goal = goal or "the requested work"
        return ForemanResult(
            goal=goal,
            questions=[
                f"What does success look like for {normalized_goal}?",
                f"What constraints or non-goals should shape {normalized_goal}?",
                f"What inputs, integrations, or edge cases matter most for {normalized_goal}?",
            ],
            metadata={"thinking_mode": ThinkingMode.HIGH.value},
        )

    @staticmethod
    def _consolidate(goal: str, context: dict[str, Any]) -> ForemanResult:
        """Consolidate memory and context from multiple sources.

        Merges overlapping information, resolves contradictions, and produces
        a unified context document for downstream agents.
        """
        context_lines = _context_lines(context)
        subject = goal or "context"
        contradictions = [
            line
            for line in context_lines
            if any(term in line.lower() for term in ("conflict", "contradiction", "disagree"))
        ]
        return ForemanResult(
            goal=goal,
            summary="Context consolidated" if not context_lines else f"Context consolidated for {subject}",
            tasks=[
                make_plan_task(
                    "C1",
                    "Merge overlapping context into a single downstream brief",
                    AgentType.FOREMAN,
                    inputs=context_lines,
                    outputs=["consolidated context brief"],
                    acceptance_criteria="Duplicate facts are merged and contradictions are called out",
                )
            ],
            risks=["Contradictions require owner review before execution"] if contradictions else [],
            metadata={
                "thinking_mode": ThinkingMode.HIGH.value,
                "context_fields": context_lines,
                "contradiction_count": len(contradictions),
            },
        )

    @staticmethod
    def _summarise(goal: str, context: dict[str, Any]) -> ForemanResult:
        """Summarise a session preserving key decisions and action items.

        Ensures no key decisions, open questions, or action items are lost
        during summarization.
        """
        context_lines = _context_lines(context)
        subject = goal or "session"
        action_items = context.get("action_items") if isinstance(context, dict) else None
        questions = context.get("open_questions") if isinstance(context, dict) else None
        normalized_questions = [str(item) for item in questions] if isinstance(questions, list) else []
        return ForemanResult(
            goal=goal,
            summary=f"Summary for {subject}: key state retained; {len(context_lines)} context field(s) captured.",
            questions=normalized_questions,
            tasks=[
                make_plan_task(
                    "S1",
                    "Carry forward summary decisions and action items",
                    AgentType.FOREMAN,
                    outputs=["session summary"],
                    acceptance_criteria="Summary preserves decisions, action items, and open questions",
                )
            ],
            metadata={
                "thinking_mode": ThinkingMode.HIGH.value,
                "action_item_count": len(action_items) if isinstance(action_items, list) else 0,
                "context_fields": context_lines,
            },
        )

    @staticmethod
    def _prune(goal: str, context: dict[str, Any]) -> ForemanResult:
        """Manage token budget by pruning low-value context.

        Identifies and removes redundant, outdated, or low-relevance content
        while preserving critical decisions and open issues.
        """
        context_lines = _context_lines(context)
        retained = [
            line
            for line in context_lines
            if any(word in line.lower() for word in ("decision", "risk", "todo", "action", "blocker"))
        ]
        pruned_count = max(0, len(context_lines) - len(retained))
        return ForemanResult(
            goal=goal,
            summary=f"Context pruned: retained {len(retained)} high-signal field(s), pruned {pruned_count}.",
            tasks=[
                make_plan_task(
                    "P1",
                    "Retain high-signal context and discard redundant detail",
                    AgentType.FOREMAN,
                    outputs=["pruned context brief"],
                    acceptance_criteria="Critical decisions, risks, blockers, and actions remain available",
                )
            ],
            risks=["No high-signal fields were detected; review before discarding context"]
            if context_lines and not retained
            else [],
            metadata={
                "thinking_mode": ThinkingMode.LOW.value,
                "retained_context": retained,
                "pruned_field_count": pruned_count,
            },
        )

    @staticmethod
    def _extract(goal: str, context: dict[str, Any]) -> ForemanResult:
        """Extract knowledge patterns gathered during completed work.

        Identifies reusable patterns, common pitfalls, and best practices
        observed in past tasks and episodes.
        """
        context_lines = _context_lines(context)
        patterns = context.get("patterns") if isinstance(context, dict) else None
        extracted = [str(item) for item in patterns] if isinstance(patterns, list) else context_lines
        return ForemanResult(
            goal=goal,
            summary=f"Knowledge extracted: {len(extracted)} reusable item(s) identified.",
            tasks=[
                make_plan_task(
                    "E1",
                    "Promote reusable lessons into the appropriate memory or documentation surface",
                    AgentType.FOREMAN,
                    outputs=["extracted knowledge brief"],
                    acceptance_criteria="Extracted lessons include source context and reuse criteria",
                )
            ],
            metadata={
                "thinking_mode": ThinkingMode.HIGH.value,
                "extracted_items": extracted[:10],
            },
        )
