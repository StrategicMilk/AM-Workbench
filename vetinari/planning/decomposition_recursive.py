"""Recursive decomposition helpers for the public decomposition engine."""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING, Any

from vetinari.agents.contracts import Task
from vetinari.planning.delegation_budget import DelegationBudget
from vetinari.planning.plan_graph import PlanGraph
from vetinari.planning.spec_frame import SpecFrame
from vetinari.types import AgentType

logger = logging.getLogger(__name__)

_FOREMAN_UNINITIALIZED = object()

# Recursive decomposition limit (ADR-0081) - prevents unbounded Foreman chains.
MAX_RECURSIVE_DEPTH = 3


class _RecursiveDecompositionMixin:
    """Adds recursive decomposition and Foreman judgment behavior."""

    if TYPE_CHECKING:
        decompose_task: Any

    _foreman: Any
    _plan_graph: PlanGraph
    _delegation_budget: DelegationBudget

    def decompose_recursive(
        self,
        task_prompt: str,
        parent_task_id: str = "root",
        plan_id: str = "default",
        recursive_depth: int = 0,
        complexity_threshold: int = 4,
    ) -> list[dict[str, Any]]:
        """Recursively decompose a task up to MAX_RECURSIVE_DEPTH levels.

        Performs an initial decomposition then recurses into any subtask
        only when Foreman's ``judge_decomposability`` returns
        ``decompose_further``.  Each recursion decrements the available depth,
        and a hard stop at MAX_RECURSIVE_DEPTH prevents unbounded Foreman
        chains (ADR-0081).

        Args:
            task_prompt: The task or goal description to decompose.
            parent_task_id: The ID of the parent task.
            plan_id: Plan identifier for history tracking.
            recursive_depth: Current recursion depth (0 = top-level call).
            complexity_threshold: Deprecated compatibility parameter. It is
                no longer consulted by the recursive gate.

        Returns:
            Flat list of subtask dicts, including all recursively generated
            subtasks with updated ``parent_task_id`` linkages.
        """
        if recursive_depth >= MAX_RECURSIVE_DEPTH:
            logger.debug(
                "[DecompositionEngine] MAX_RECURSIVE_DEPTH=%d reached for task: %s",
                MAX_RECURSIVE_DEPTH,
                task_prompt[:60],
            )
            return self.decompose_task(
                task_prompt=task_prompt,
                parent_task_id=parent_task_id,
                depth=0,
                plan_id=plan_id,
            )

        first_level = self.decompose_task(
            task_prompt=task_prompt,
            parent_task_id=parent_task_id,
            depth=0,
            plan_id=plan_id,
        )

        all_subtasks: list[dict[str, Any]] = []
        for subtask in first_level:
            all_subtasks.append(subtask)
            desc = subtask.get("description", "")
            decision = self._judge_subtask(subtask, plan_id, recursive_depth)
            if decision is None:
                continue
            subtask["decompose_decision_action"] = decision.action
            subtask["decompose_decision_reason"] = decision.reason
            subtask["decompose_decision_confidence"] = decision.confidence
            if decision.action == "decompose_further":
                child_tasks = self.decompose_recursive(
                    task_prompt=desc,
                    parent_task_id=subtask.get("subtask_id", parent_task_id),
                    plan_id=plan_id,
                    recursive_depth=recursive_depth + 1,
                    complexity_threshold=complexity_threshold,
                )
                all_subtasks.extend(child_tasks)

        logger.info(
            "[DecompositionEngine] Recursive decomp depth=%d produced %d total subtasks for: %s",
            recursive_depth,
            len(all_subtasks),
            task_prompt[:60],
        )
        return all_subtasks

    def _resolve_foreman(self) -> Any:
        """Return a Foreman agent that can judge recursive decomposition."""
        if self._foreman is not _FOREMAN_UNINITIALIZED:
            return self._foreman
        try:
            from vetinari.agents import get_foreman_agent
            from vetinari.agents.consolidated.foreman import install_foreman_judgment

            install_foreman_judgment()
            self._foreman = get_foreman_agent()
        except Exception as exc:
            logger.warning("Foreman judge unavailable; recursive decomposition will not recurse: %s", exc)
            self._foreman = None
        return self._foreman

    def _judge_subtask(self, subtask: dict[str, Any], plan_id: str, recursive_depth: int) -> Any:
        """Ask Foreman whether a subtask should be decomposed further."""
        foreman = self._resolve_foreman()
        if foreman is None or not hasattr(foreman, "judge_decomposability"):
            return None
        task = Task(
            id=str(subtask.get("subtask_id") or uuid.uuid4().hex[:8]),
            description=str(subtask.get("description") or ""),
            inputs=list(subtask.get("inputs") or []),
            outputs=list(subtask.get("outputs") or []),
            dependencies=list(subtask.get("dependencies") or []),
            assigned_agent=AgentType(subtask.get("agent_type") or AgentType.WORKER.value),
            depth=int(subtask.get("depth") or recursive_depth),
            parent_id=str(subtask.get("parent_task_id") or ""),
            metadata={"plan_id": plan_id, "owned_write_scope": subtask.get("outputs", [])},
        )
        spec_frame = SpecFrame(goal=task.description, in_scope=tuple(task.outputs))
        try:
            return foreman.judge_decomposability(
                task=task,
                plan_graph=self._plan_graph,
                delegation_budget=self._delegation_budget,
                spec_frame=spec_frame,
                recursive_depth=recursive_depth,
            )
        except Exception as exc:
            logger.warning("Foreman judge failed; preserving subtask without recursion: %s", exc)
            return None
