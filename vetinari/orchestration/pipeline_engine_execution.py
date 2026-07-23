"""Pipeline execution setup and planning stages.

This module holds the queue-admitted pipeline path before model assignment so
``pipeline_engine.py`` can stay as the public compatibility composition point.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from .pipeline_engine_execution_steps import (
    _execute_pipeline_steps,
    _PipelineExecutionInputs,
)


def execute_pipeline_impl(
    self: Any,
    goal: str,
    constraints: dict[str, Any] | None,
    context: dict[str, Any],
    stages: dict[str, Any],
    start_time: float,
    _corr_ctx: Any,
    _pipeline_span: Any,
    _intake_tier: Any,
    _intake_features: Any,
    task_handler: Callable[..., Any] | None,
    project_id: str | None,
    model_id: str | None,
    *,
    contextlib_module: Any,
    log_event_fn: Callable[..., None],
    logger: logging.Logger,
    logger_name: str,
    save_pipeline_checkpoint: Callable[..., None],
) -> dict[str, Any]:
    """Execute setup, intake, prevention, planning, and decomposition stages.

    Args:
        self: Pipeline engine instance mixed into the orchestrator.
        goal: User goal to execute.
        constraints: Optional planning constraints.
        context: Mutable execution context.
        stages: Mutable stage accumulator.
        start_time: Pipeline start timestamp.
        _corr_ctx: Optional structured logging correlation context.
        _pipeline_span: Optional GenAI tracing span.
        _intake_tier: Optional intake classification tier.
        _intake_features: Optional intake feature extraction result.
        task_handler: Optional task execution callback.
        project_id: Optional project identifier.
        model_id: Optional model identifier retained for signature compatibility.
        contextlib_module: Contextlib module from the public facade patch seam.
        log_event_fn: Structured logging function from the public facade patch seam.
        logger: Logger from the public facade.
        logger_name: Logger name to record in structured events.
        save_pipeline_checkpoint: Checkpoint writer from the public facade patch seam.

    Returns:
        Pipeline result dict.
    """
    return _execute_pipeline_steps(
        _PipelineExecutionInputs(
            self_obj=self,
            goal=goal,
            constraints=constraints,
            context=context,
            stages=stages,
            start_time=start_time,
            corr_ctx=_corr_ctx,
            pipeline_span=_pipeline_span,
            intake_tier=_intake_tier,
            intake_features=_intake_features,
            task_handler=task_handler,
            project_id=project_id,
            model_id=model_id,
            contextlib_module=contextlib_module,
            log_event_fn=log_event_fn,
            logger=logger,
            logger_name=logger_name,
            save_pipeline_checkpoint=save_pipeline_checkpoint,
        )
    )
