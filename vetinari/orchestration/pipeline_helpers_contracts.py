"""Shared protocols and helpers for pipeline support services."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol


def _candidate_sort_key(candidate: str) -> tuple[int, str]:
    """FSA-0544 deterministic tiebreaker for equal best-of-N candidate scores."""
    return (len(candidate), candidate)


class PipelineVariantManagerLike(Protocol):
    """Variant manager operations used by pipeline support services."""

    def get_config(self) -> PipelineVariantConfigLike:
        """Return the active variant configuration."""

    def set_level(self, level: str) -> PipelineVariantConfigLike:
        """Switch the active variant level."""


class PipelineVariantConfigLike(Protocol):
    """Variant configuration fields consumed by orchestration helpers."""

    max_context_tokens: int
    max_planning_depth: int
    enable_verification: bool
    enable_self_improvement: bool


class PipelineExecutionEngineLike(Protocol):
    """Execution engine operations used by pipeline support services."""

    def register_handler(self, task_type: str, handler: Callable[..., Any]) -> None:
        """Register a task handler.

        Args:
            task_type: Task type string routed to this handler.
            handler: Callable that executes matching tasks.
        """


class RouterModelLike(Protocol):
    """Router model fields consumed by pipeline support services."""

    id: str


class ConfidenceLevelLike(Protocol):
    """Confidence level fields consumed by pipeline support services."""

    value: str


class ConfidenceResultLike(Protocol):
    """Model-selection confidence fields consumed by pipeline support services."""

    score: float
    level: ConfidenceLevelLike
    explanation: str


class UnknownSituationValueLike(Protocol):
    """Unknown-situation enum fields consumed by pipeline support services."""

    value: str


class UnknownSituationLike(Protocol):
    """Unknown-situation protocol fields consumed by pipeline support services."""

    situation: UnknownSituationValueLike
    message: str
    action: Any


class ModelSelectionLike(Protocol):
    """Model-selection fields consumed by pipeline support services."""

    model: RouterModelLike
    confidence_result: ConfidenceResultLike | None
    unknown_situations: list[UnknownSituationLike]


class ModelRouterLike(Protocol):
    """Model router operations used by pipeline support services."""

    def select_model(self, task_type: Any) -> ModelSelectionLike:
        """Select a model for the task type."""
