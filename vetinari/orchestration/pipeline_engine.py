"""Core pipeline execution loop — the 8-stage assembly-line orchestration.

When to use this module:
    ``PipelineExecutionEngine`` is the primary execution path for all standard
    Vetinari requests.  It implements the full 8-stage pipeline from intake
    classification through goal verification and learning, and is composed
    into ``FactoryPipelineOrchestrator``.  This is the module to read when tracing
    how a request flows from user input to final output.

Pipeline role: **all 8 stages** — this IS the pipeline.
Compare with ``durable_execution.py`` (adds checkpoint persistence on top of
these stages) and ``pipeline_agent_graph.py`` (handles stage 5 DAG dispatch).

Implements the core pipeline stages:
  0. Intake classification (Configure-to-Order)
  0.5. Production leveling (RequestQueue admission)
  0.9. Pre-execution prevention gate
  1. Input Analysis
  2-3. Plan Generation + Task Decomposition
  4. Model Assignment
  5. Parallel Execution
  5.5. Self-refinement (Custom tier)
  6. Output Review
  7. Final Assembly
  8. Goal Verification + Correction Loop

``PipelineExecutionEngine`` is composed into ``FactoryPipelineOrchestrator`` and delegates
quality and helper calls to the other pipeline components via ``self``.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Callable
from typing import Any

from vetinari.orchestration.pipeline_engine_checkpoints import _save_pipeline_checkpoint
from vetinari.orchestration.pipeline_engine_entry import _PipelineEngineEntryPoint
from vetinari.structured_logging import log_event

from .pipeline_engine_execution import execute_pipeline_impl

logger = logging.getLogger(__name__)


class PipelineExecutionEngine(_PipelineEngineEntryPoint):
    """Full 8-stage assembly-line pipeline execution compatibility facade.

    Mixed into FactoryPipelineOrchestrator. The queue-admitted stage ordering
    lives in ``pipeline_engine_execution`` and ``pipeline_engine_model_assignment``;
    this class preserves the public import path and patch seams used by callers.
    """

    def _execute_pipeline(
        self,
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
    ) -> dict[str, Any]:
        """Execute the full pipeline after queue admission.

        Args:
            goal: The user goal.
            constraints: Optional constraints dict.
            context: Execution context dict.
            stages: Pipeline stages accumulator.
            start_time: Pipeline start timestamp.
            _corr_ctx: CorrelationContext or None.
            _pipeline_span: OTel pipeline span or None.
            _intake_tier: Intake tier classification or None.
            _intake_features: Intake feature extraction or None.
            task_handler: Optional task handler callback.
            project_id: Optional project identifier.
            model_id: Optional model identifier.

        Returns:
            Pipeline result dict.
        """
        return execute_pipeline_impl(
            self,
            goal,
            constraints,
            context,
            stages,
            start_time,
            _corr_ctx,
            _pipeline_span,
            _intake_tier,
            _intake_features,
            task_handler,
            project_id,
            model_id,
            contextlib_module=contextlib,
            log_event_fn=log_event,
            logger=logger,
            logger_name=__name__,
            save_pipeline_checkpoint=_save_pipeline_checkpoint,
        )
