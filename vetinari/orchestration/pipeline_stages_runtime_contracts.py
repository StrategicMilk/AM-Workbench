"""Host protocols for the extracted pipeline stage runtime."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

from vetinari.orchestration.pipeline_events import PipelineStage


class PipelineStageRuntimeOwner(Protocol):
    """Host contract required by the extracted stage runtime mixin."""

    enable_correction_loop: bool
    is_paused: Callable[[], bool]

    def _emit(self, stage: PipelineStage, event_type: str, data: dict[str, Any] | None = None) -> None: ...

    def _validate_stage_boundary(
        self, stage_name: str, stage_output: Any, min_keys: list[str] | None = None
    ) -> tuple[bool, list[str]]: ...

    def _check_stage_constraints(
        self, agent_type: str, mode: str | None, quality_score: float | None = None
    ) -> tuple[bool, list[str]]: ...

    def _sandbox_validate_code_output(self, code: str, language: str = "python") -> tuple[bool, str]: ...

    def _review_outputs(
        self, exec_results: dict[str, Any], goal: str, context: dict[str, Any] | None = None
    ) -> dict[str, Any]: ...

    def _assemble_final_output(self, exec_results: dict[str, Any], review_result: dict[str, Any], goal: str) -> str: ...

    def _execute_corrections(
        self,
        corrective_tasks: list[dict[str, Any]],
        plan: dict[str, Any],
        goal: str,
        context: dict[str, Any] | None = None,
        max_rounds: int | None = None,
    ) -> Any: ...

    def _get_pipeline_event_bus(self) -> Any: ...
