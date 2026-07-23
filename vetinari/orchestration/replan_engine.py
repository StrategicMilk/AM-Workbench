"""Mid-execution DAG replanning capabilities for AgentGraph.

This module provides ``ReplanEngine``, a mixin class that adds replanning logic
to ``AgentGraph`` without bloating the main orchestration file.  The mixin
accesses ``self._agents`` (dict[AgentType, Any]) which AgentGraph populates
during initialization.

Replanning is triggered when an agent's result metadata signals that the
remaining plan should change — for example when Researcher discovers scope
changes, Oracle raises an architectural concern, or Builder hits unexpected
complexity.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from vetinari.agents.contracts import AgentResult, AgentTask, Task
from vetinari.constants import TRUNCATE_OUTPUT_SUMMARY
from vetinari.orchestration.graph_types import ExecutionDAG, ReplanResult, TaskNode
from vetinari.types import AgentType, StatusEnum

logger = logging.getLogger(__name__)


class ReplanEngine:
    """Mid-execution replanning capabilities (mixin for AgentGraph).

    Requires the host class to expose:
        self._agents: dict[AgentType, Any]
    """

    # The _agents attribute is declared on AgentGraph; type checkers see it via
    # the mixin contract, not a field definition here.

    if TYPE_CHECKING:
        _agents: dict[AgentType, Any]

    @staticmethod
    def _should_replan(task: Task, result: AgentResult) -> bool:
        """Check whether intermediate results warrant DAG replanning.

        Evaluates metadata flags set by agents during execution to determine
        if the remaining task graph needs adjustment.

        Args:
            task: The task that just completed.
            result: The result of the completed task.

        Returns:
            True if replanning is warranted.
        """
        metadata = result.metadata if hasattr(result, "metadata") and result.metadata else {}

        # 1. Researcher discovered something that changes scope
        if (
            hasattr(task, "assigned_agent")
            and task.assigned_agent == AgentType.WORKER
            and metadata.get("scope_changed")
        ):
            logger.info(
                "[Replan] Worker scope change detected for task %s",
                task.id,
            )
            return True

        # 2. Oracle identified an architectural concern
        if (
            hasattr(task, "assigned_agent")
            and task.assigned_agent == AgentType.WORKER
            and metadata.get("architecture_concern")
        ):
            logger.info(
                "[Replan] Worker architecture concern for task %s",
                task.id,
            )
            return True

        # 3. Builder's partial work revealed unexpected complexity
        if (
            hasattr(task, "assigned_agent")
            and task.assigned_agent == AgentType.WORKER
            and metadata.get("complexity_exceeded")
        ):
            logger.info(
                "[Replan] Worker complexity exceeded for task %s",
                task.id,
            )
            return True

        # 4. Foreman identified a planning concern that requires a new DAG.
        if (
            hasattr(task, "assigned_agent")
            and task.assigned_agent == AgentType.FOREMAN
            and (metadata.get("architecture_concern") or metadata.get("complexity_exceeded"))
        ):
            logger.info(
                "[Replan] Foreman planning concern for task %s",
                task.id,
            )
            return True

        # 5. Inspector found verification scope drift.
        if (
            hasattr(task, "assigned_agent")
            and task.assigned_agent == AgentType.INSPECTOR
            and metadata.get("scope_changed")
        ):
            logger.info(
                "[Replan] Inspector scope change detected for task %s",
                task.id,
            )
            return True

        # 6. An agent requested delegation
        if metadata.get("delegate_to"):
            logger.info(
                "[Replan] Delegation request from task %s to %s",
                task.id,
                metadata["delegate_to"],
            )
            return True

        return False

    def _trigger_replan(
        self,
        completed_task: Task,
        result: AgentResult,
        remaining_tasks: list[Task],
    ) -> ReplanResult:
        """Ask the Planner to adjust remaining tasks based on intermediate results.

        Args:
            completed_task: The task that triggered replanning.
            result: The result of the completed task.
            remaining_tasks: Tasks that haven't been executed yet.

        Returns:
            A ReplanResult with the new list of tasks (may be same, modified,
            or completely different from the remaining tasks).
        """
        if AgentType.FOREMAN not in self._agents:
            logger.warning("[Replan] No Foreman agent registered — skipping replan")
            return ReplanResult(new_tasks=remaining_tasks)

        planner = self._agents[AgentType.FOREMAN]
        replan_task = AgentTask(
            task_id=f"replan_{completed_task.id}",
            agent_type=AgentType.FOREMAN,
            description="Adjust remaining plan based on intermediate results",
            prompt=(
                f"Review what was learned from task '{completed_task.description}' "
                f"and adjust the remaining {len(remaining_tasks)} tasks if needed. "
                "You may add, remove, reorder, or modify tasks. "
                "Return structured output with a 'tasks' key containing the "
                "updated task list as dicts with 'id', 'description', and "
                "'assigned_agent' keys."
            ),
            context={
                "completed_task_id": str(completed_task.id),
                "completed_result": str(result.output)[:500] if result.output else "",
                "remaining_task_count": len(remaining_tasks),
                "remaining_task_descriptions": [t.description for t in remaining_tasks[:10]],
                "instruction": (
                    "Review what was learned and adjust remaining tasks if needed. "
                    "Return the updated task list as structured data."
                ),
            },
        )

        try:
            replan_result = planner.execute(replan_task)
            if replan_result.success:
                logger.info(
                    "[Replan] Planner adjusted plan after task %s",
                    completed_task.id,
                )
                replan_output = str(replan_result.output)[:TRUNCATE_OUTPUT_SUMMARY] if replan_result.output else ""

                # Attempt to parse structured task adjustments from Planner output
                parsed_tasks = self._parse_replan_tasks(replan_result.output)
                if parsed_tasks is not None:
                    logger.info(
                        "[Replan] Parsed %d tasks from Planner output",
                        len(parsed_tasks),
                    )
                    return ReplanResult(
                        new_tasks=parsed_tasks,
                        replan_output=replan_output,
                    )

                # Planner output was not structured — keep existing tasks
                return ReplanResult(
                    new_tasks=remaining_tasks,
                    replan_output=replan_output,
                )
        except Exception:  # Broad: agent.execute() failure modes are indeterminate (network, inference, value errors)
            logger.warning(
                "[Replan] Planner replan failed for task %s",
                completed_task.id,
                exc_info=True,
            )

        return ReplanResult(new_tasks=remaining_tasks)

    @staticmethod
    def _parse_replan_tasks(output: Any) -> list[Task] | None:
        """Attempt to parse structured task list from Planner replan output.

        Accepts output as a dict with a 'tasks' key containing a list
        of task dicts, each with 'id', 'description', and optionally
        'assigned_agent'. Returns None if output is not parseable.

        Args:
            output: The Planner's raw output (dict, str, or other).

        Returns:
            List of Task objects if parsing succeeded, None otherwise.
        """
        if not isinstance(output, dict):
            return None

        tasks_data = output.get("tasks")
        if not isinstance(tasks_data, list) or not tasks_data:
            return None

        parsed: list[Task] = []
        for item in tasks_data:
            if not isinstance(item, dict) or "description" not in item:
                continue
            agent_str = item.get("assigned_agent", AgentType.WORKER.value).upper()
            try:
                agent_type = AgentType[agent_str]
            except KeyError:
                agent_type = AgentType.WORKER

            task = Task(
                id=item.get("id", f"replan_{len(parsed)}"),
                description=item["description"],
                assigned_agent=agent_type,
                dependencies=item.get("dependencies", []),
                inputs=item.get("inputs", []),
                outputs=[],
            )
            parsed.append(task)

        return parsed or None

    @staticmethod
    def _replace_remaining_tasks(
        exec_plan: ExecutionDAG,
        new_tasks: list[Task],
    ) -> None:
        """Replace all PENDING tasks in an execution plan with new tasks.

        Removes tasks that haven't started and adds the new tasks
        with proper dependencies.

        Args:
            exec_plan: The execution plan to modify.
            new_tasks: The replacement tasks.
        """
        # Remove all PENDING tasks from the plan
        pending_ids = [tid for tid, node in exec_plan.nodes.items() if node.status == StatusEnum.PENDING]
        for tid in pending_ids:
            del exec_plan.nodes[tid]

        # Remove from execution order
        exec_plan.execution_order = [tid for tid in exec_plan.execution_order if tid not in pending_ids]

        # Add new tasks
        for task in new_tasks:
            task_id = str(task.id)
            node = TaskNode(task=task, status=StatusEnum.PENDING)
            exec_plan.nodes[task_id] = node
            exec_plan.execution_order.append(task_id)

        logger.info(
            "[Replan] Replaced %d pending tasks with %d new tasks",
            len(pending_ids),
            len(new_tasks),
        )
