"""Decomposition Engine.

====================
Provides task decomposition services used by the Decomposition Lab UI.
Wraps the ForemanAgent and planning infrastructure.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from vetinari.planning.decomposition_models import (
    _DOD_CRITERIA,
    _DOR_CRITERIA,
    DEFAULT_MAX_DEPTH,
    MAX_MAX_DEPTH,
    MIN_MAX_DEPTH,
    SEED_MIX,
    SEED_RATE,
    DecompositionEvent,
    SubtaskSpec,
)
from vetinari.planning.decomposition_recursive import (
    _FOREMAN_UNINITIALIZED,
    MAX_RECURSIVE_DEPTH,
    _RecursiveDecompositionMixin,
)
from vetinari.planning.decomposition_templates import build_default_templates
from vetinari.planning.delegation_budget import DelegationBudget
from vetinari.planning.plan_graph import PlanGraph
from vetinari.security.redaction import redact_text
from vetinari.types import AgentType

__all__ = [
    "DEFAULT_MAX_DEPTH",
    "MAX_MAX_DEPTH",
    "MAX_RECURSIVE_DEPTH",
    "MIN_MAX_DEPTH",
    "SEED_MIX",
    "SEED_RATE",
    "_DOD_CRITERIA",
    "_DOR_CRITERIA",
    "DecompositionEngine",
    "DecompositionEvent",
    "SubtaskSpec",
    "decomposition_engine",
]

logger = logging.getLogger(__name__)


class DecompositionEngine(_RecursiveDecompositionMixin):
    """Orchestrates task decomposition using the ForemanAgent.

    Used by the Decomposition Lab in the web UI.
    """

    SEED_MIX = SEED_MIX
    SEED_RATE = SEED_RATE
    DEFAULT_MAX_DEPTH = DEFAULT_MAX_DEPTH
    MIN_MAX_DEPTH = MIN_MAX_DEPTH
    MAX_MAX_DEPTH = MAX_MAX_DEPTH

    def __init__(self):
        self._history: list[DecompositionEvent] = []
        self._templates: list[dict[str, Any]] = self._build_default_templates()
        self._foreman: Any = _FOREMAN_UNINITIALIZED
        self._plan_graph = PlanGraph()
        self._delegation_budget = DelegationBudget("decomposition")

    @staticmethod
    def _build_default_templates() -> list[dict[str, Any]]:
        """Build built-in decomposition templates."""
        return build_default_templates()

    def get_templates(
        self,
        keywords: list[str] | None = None,
        agent_type: str | None = None,
        dod_level: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return matching decomposition templates.

        Args:
            keywords: The keywords.
            agent_type: The agent type.
            dod_level: The dod level.

        Returns:
            List of template dicts that satisfy all provided filters.
            Filters are applied conjunctively; passing no filters returns
            all registered templates.
        """
        results = self._templates[:]
        if keywords:
            kw_lower = [k.lower() for k in keywords]
            results = [t for t in results if any(kw in t.get("keywords", []) for kw in kw_lower)]
        if agent_type:
            results = [t for t in results if t.get("agent_type") == agent_type.upper()]
        if dod_level:
            results = [t for t in results if t.get("dod_level") == dod_level]
        return results

    def get_dod_criteria(self, level: str = "Standard") -> list[str]:
        """Return Definition of Done criteria for the given quality level.

        Args:
            level: Quality tier name (e.g. "Standard", "Strict"). Falls back
                to "Standard" when the requested level is not defined.

        Returns:
            List of criterion strings that must be satisfied for task completion.
        """
        return _DOD_CRITERIA.get(level, _DOD_CRITERIA["Standard"])

    def get_dor_criteria(self, level: str = "Standard") -> list[str]:
        """Return Definition of Ready criteria for the given quality level.

        Args:
            level: Quality tier name (e.g. "Standard", "Strict"). Falls back
                to "Standard" when the requested level is not defined.

        Returns:
            List of criterion strings that must be met before a task can start.
        """
        return _DOR_CRITERIA.get(level, _DOR_CRITERIA["Standard"])

    def decompose_task(
        self,
        task_prompt: str,
        parent_task_id: str = "root",
        depth: int = 0,
        max_depth: int = DEFAULT_MAX_DEPTH,
        plan_id: str = "default",
    ) -> list[dict[str, Any]]:
        """Decompose a task into subtasks using the ForemanAgent.

        Falls back to keyword-based decomposition.

        Args:
            task_prompt: The task prompt.
            parent_task_id: The parent task id.
            depth: The depth.
            max_depth: The max depth.
            plan_id: The plan id.

        Returns:
            List of subtask dicts, each containing ``subtask_id``,
            ``parent_task_id``, ``description``, ``agent_type``, ``depth``,
            ``inputs``, ``outputs``, ``dependencies``, and
            ``acceptance_criteria``.  Returns an empty list when
            ``max_depth`` is reached.
        """
        max_depth = max(MIN_MAX_DEPTH, min(max_depth, MAX_MAX_DEPTH))

        if depth >= max_depth:
            logger.warning("Max decomposition depth %s reached for task: %s", max_depth, _safe_task_log(task_prompt))
            return []

        try:
            from vetinari.agents import get_foreman_agent
            from vetinari.agents.contracts import AgentTask

            planner = get_foreman_agent()
            agent_task = AgentTask(
                task_id=f"decomp_{uuid.uuid4().hex[:8]}",
                agent_type=AgentType.FOREMAN,
                description=f"Decompose: {task_prompt}",
                prompt=task_prompt,
                context={"depth": depth, "max_depth": max_depth, "plan_id": plan_id},
            )
            result = planner.execute(agent_task)
            if result.success and isinstance(result.output, dict):
                tasks = result.output.get("tasks", [])
                subtasks = []
                for t in tasks:
                    subtask = {
                        "subtask_id": t.get("id", str(uuid.uuid4())[:8]),
                        "parent_task_id": parent_task_id,
                        "description": t.get("description", ""),
                        "agent_type": t.get("assigned_agent", AgentType.WORKER.value),
                        "depth": depth + 1,
                        "inputs": t.get("inputs", []),
                        "outputs": t.get("outputs", []),
                        "dependencies": t.get("dependencies", []),
                        "acceptance_criteria": t.get("acceptance_criteria", ""),
                    }
                    subtasks.append(subtask)

                # Record history
                self._history.append(
                    DecompositionEvent(
                        event_id=str(uuid.uuid4()),
                        plan_id=plan_id,
                        task_id=parent_task_id,
                        depth=depth,
                        seeds_used=[],
                        subtasks_created=len(subtasks),
                    ),
                )
                return subtasks
        except Exception as e:
            logger.warning("LLM decomposition failed, using keyword fallback: %s", redact_text(str(e)))

        # Keyword fallback
        return self._keyword_decompose(task_prompt, parent_task_id, depth)

    @staticmethod
    def _keyword_decompose(task_prompt: str, parent_task_id: str, depth: int) -> list[dict[str, Any]]:
        """Simple keyword-based decomposition fallback."""
        task_lower = task_prompt.lower()
        subtasks = []

        def make_subtask(desc: str, agent: str, deps: list[str] | None = None) -> dict[str, Any]:
            """Build a subtask dict with a fresh UUID-based ID.

            Args:
                desc: The desc.
                agent: The agent.
                deps: The deps.

            Returns:
                Subtask dict with a unique ``subtask_id``, the given
                ``description`` and ``agent_type``, and the current
                recursion ``depth + 1``.
            """
            sid = f"st_{uuid.uuid4().hex[:6]}"
            return {
                "subtask_id": sid,
                "parent_task_id": parent_task_id,
                "description": desc,
                "agent_type": agent,
                "depth": depth + 1,
                "inputs": [],
                "outputs": [],
                "dependencies": deps or [],
                "acceptance_criteria": f"{desc} is complete",
            }

        s1 = make_subtask("Analyze requirements and define scope", AgentType.WORKER.value)
        subtasks.append(s1)

        if any(kw in task_lower for kw in ["code", "implement", "build", "develop"]):
            s2 = make_subtask("Implement core functionality", AgentType.WORKER.value, [s1["subtask_id"]])
            subtasks.append(s2)
            s3 = make_subtask("Write tests", AgentType.INSPECTOR.value, [s2["subtask_id"]])
            subtasks.append(s3)

        if any(kw in task_lower for kw in ["ui", "frontend", "web", "interface"]):
            prev = subtasks[-1]["subtask_id"] if subtasks else s1["subtask_id"]
            subtasks.append(make_subtask("Design and implement UI", AgentType.WORKER.value, [prev]))

        last = subtasks[-1]["subtask_id"] if subtasks else s1["subtask_id"]
        subtasks.append(make_subtask("Review and document", AgentType.INSPECTOR.value, [last]))

        return subtasks

    def get_decomposition_history(self, plan_id: str | None = None) -> list[DecompositionEvent]:
        """Return decomposition history, optionally filtered by plan_id.

        Returns:
            List of results.
        """
        if plan_id:
            return [e for e in self._history if e.plan_id == plan_id]
        return list(self._history)


# Module-level singleton
_decomposition_engine: DecompositionEngine | None = None


def _get_engine() -> DecompositionEngine:
    global _decomposition_engine
    if _decomposition_engine is None:
        _decomposition_engine = DecompositionEngine()
    return _decomposition_engine


# Exported instance used by web_ui.py
decomposition_engine = _get_engine()


def _safe_task_log(task_prompt: str) -> str:
    return redact_text(task_prompt[:80])
