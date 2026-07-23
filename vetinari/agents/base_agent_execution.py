"""Safe execution wrapper for BaseAgent.

Pipeline role: Validate -> Prepare -> Execute -> Guardrail -> Complete.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from vetinari.agents.base_agent_execution_steps import _execute_safely_steps
from vetinari.agents.contracts import AgentResult, AgentTask

if TYPE_CHECKING:
    from vetinari.agents.base_agent import BaseAgent


def execute_safely(agent: BaseAgent, task: AgentTask, execute_fn: Callable[[AgentTask], AgentResult]) -> AgentResult:
    """Template wrapper for safe agent execution with validation and error handling.

    Args:
        agent: The BaseAgent instance running the task.
        task: The task to execute.
        execute_fn: Callable that accepts a prepared AgentTask and returns
            the agent's core AgentResult.

    Returns:
        AgentResult with success/failure status, output, and metadata.
    """
    return _execute_safely_steps(agent, task, execute_fn)
