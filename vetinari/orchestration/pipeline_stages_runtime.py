"""Stage runtime for pipeline execution stages 5-8."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from vetinari.orchestration.pipeline_stages_runtime_steps import (
    _run_runtime_stages,
    _StageRuntimeInputs,
)


def _run_execution_stages_impl(
    runner: Any,
    goal: str,
    graph: Any,
    context: dict[str, Any],
    stages: dict[str, Any],
    start_time: float,
    _corr_ctx: Any,
    _pipeline_span: Any,
    task_handler: Callable[..., Any] | None,
    project_id: str | None,
    _intake_tier: Any,
    _intake_features: Any,
) -> dict[str, Any]:
    """Execute stages 5-8 through extracted runtime helpers."""
    return _run_runtime_stages(
        _StageRuntimeInputs(
            runner=runner,
            goal=goal,
            graph=graph,
            context=context,
            stages=stages,
            start_time=start_time,
            corr_ctx=_corr_ctx,
            pipeline_span=_pipeline_span,
            task_handler=task_handler,
            project_id=project_id,
            intake_tier=_intake_tier,
            intake_features=_intake_features,
        )
    )


class _PipelineStageRuntime:
    """Run pipeline execution stages after planning/model assignment."""

    def _run_execution_stages(
        self,
        goal: str,
        graph: Any,
        context: dict[str, Any],
        stages: dict[str, Any],
        start_time: float,
        _corr_ctx: Any,
        _pipeline_span: Any,
        task_handler: Callable[..., Any] | None,
        project_id: str | None,
        _intake_tier: Any,
        _intake_features: Any,
    ) -> dict[str, Any]:
        """Execute stages 5-8 through the extracted runtime helper."""
        return _run_execution_stages_impl(
            self,
            goal,
            graph,
            context,
            stages,
            start_time,
            _corr_ctx,
            _pipeline_span,
            task_handler,
            project_id,
            _intake_tier,
            _intake_features,
        )
