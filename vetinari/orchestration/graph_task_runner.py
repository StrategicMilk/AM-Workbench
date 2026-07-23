"""Per-task execution logic for the AgentGraph.

Pause state is owned by ``vetinari.orchestration.agent_control``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from vetinari.orchestration.graph_task_runner_execution_steps import _execute_task_node_steps
from vetinari.orchestration.graph_task_runner_helpers import _get_agent_control_state_fn, _get_execution_context_api
from vetinari.orchestration.graph_types import TaskNode
from vetinari.orchestration.task_retry_loop import TaskRetryLoopRunner

if TYPE_CHECKING:
    from vetinari.agents.contracts import AgentResult

__all__ = ["GraphTaskRunner", "_get_agent_control_state_fn", "_get_execution_context_api"]


class GraphTaskRunner(TaskRetryLoopRunner):
    """Per-task execution mixin for AgentGraph."""

    def _execute_task_node(
        self,
        node: TaskNode,
        prior_results: dict[str, AgentResult] | None = None,
    ) -> AgentResult:
        """Execute a single task with retries and a self-correction loop."""
        return _execute_task_node_steps(self, node, prior_results)
