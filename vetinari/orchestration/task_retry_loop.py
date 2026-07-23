"""Retry loop and post-execution logic for per-task graph execution.

Pipeline role: Plan -> Graph -> TaskRetryLoop (attempt, verify, delegate) -> Result.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from vetinari.orchestration.task_retry_loop_steps import _run_task_attempt_loop_steps
from vetinari.types import AgentType

if TYPE_CHECKING:
    from vetinari.agents.contracts import AgentResult, AgentTask


class TaskRetryLoopRunner:
    """Retry loop and post-verification logic for task execution."""

    def _run_task_attempt_loop(
        self,
        node: Any,
        agent: Any,
        agent_type: AgentType,
        agent_task: AgentTask,
        _monitor: Any,
        _cb: Any,
        _agent_cb: Any,
        _emit_task_done: Any,
    ) -> AgentResult:
        """Execute the retry loop for a single task attempt.

        Args:
            node: The TaskNode with retry state.
            agent: The agent instance to execute.
            agent_type: The AgentType enum value for this agent.
            agent_task: The prepared AgentTask to pass to the agent.
            _monitor: Optional AgentMonitor instance.
            _cb: Optional backend-level CircuitBreaker instance.
            _agent_cb: Optional agent-level AgentCircuitBreaker instance.
            _emit_task_done: Callback that releases WIP slots and emits the final event.

        Returns:
            The final AgentResult for the task.
        """
        return _run_task_attempt_loop_steps(
            runner=self,
            node=node,
            agent=agent,
            agent_type=agent_type,
            agent_task=agent_task,
            monitor=_monitor,
            circuit_breaker=_cb,
            agent_circuit_breaker=_agent_cb,
            emit_task_done=_emit_task_done,
        )
