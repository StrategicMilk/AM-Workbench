"""Dispatch behavior for :mod:`vetinari.a2a.executor`."""

from __future__ import annotations

import logging
from typing import Any

from vetinari.a2a.executor_models import STATUS_ACKNOWLEDGED, A2ATask
from vetinari.security.fail_closed import sanitize_untrusted_text
from vetinari.types import AgentType

logger = logging.getLogger(__name__)


class A2ADispatchMixin:
    """Provide orchestrator dispatch behavior for the A2A executor facade."""

    def _dispatch(self, agent_type: AgentType, mode: str, task: A2ATask) -> dict[str, Any]:
        """Dispatch a task to the internal Vetinari pipeline.

        Attempts to invoke the ``TwoLayerOrchestrator`` for real execution.
        When the orchestrator is unavailable, returns a structured
        acknowledgement dict flagged with ``"_is_acknowledgement_only": True``.

        Args:
            agent_type: Which pipeline agent should handle the task.
            mode: The specific mode within that agent.
            task: The original :class:`A2ATask` being executed.

        Returns:
            Output data dict to embed in the :class:`A2AResult`.

        Raises:
            Exception: Any exception raised by the orchestrator propagates to
                the caller so it can be recorded as failed.
        """
        logger.info(
            "Dispatching task id=%s to agent=%s mode=%s",
            task.task_id,
            agent_type.value,
            mode,
        )
        from vetinari.a2a import executor as executor_module

        task_description = sanitize_untrusted_text(
            task.input_data.get("description", task.task_type),
            max_length=4_000,
        )
        task_prompt = sanitize_untrusted_text(
            task.input_data.get("goal", task_description),
            max_length=20_000,
        )
        orch = executor_module.get_two_layer_orchestrator()
        if orch is not None:
            agent_task = executor_module.AgentTask(
                task_id=task.task_id,
                description=task_description,
                prompt=task_prompt,
                agent_type=agent_type,
                context=task.input_data,
            )
            result = orch.execute_task(agent_task)
            return {
                "agent": agent_type.value,
                "mode": mode,
                "task_id": task.task_id,
                "output": result.output if hasattr(result, "output") else str(result),
                "success": result.success if hasattr(result, "success") else True,
            }

        logger.info(
            "Orchestrator unavailable for task id=%s; returning acknowledgement only (degraded mode)",
            task.task_id,
        )
        return {
            "agent": agent_type.value,
            "mode": mode,
            "task_id": task.task_id,
            "status": STATUS_ACKNOWLEDGED,
            "_is_acknowledgement_only": True,
            "input_summary": {
                sanitize_untrusted_text(key, max_length=120): sanitize_untrusted_text(value, max_length=100)
                for key, value in task.input_data.items()
            },
        }
