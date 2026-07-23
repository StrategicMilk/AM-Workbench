"""Shared protocols for pipeline quality helper mixins."""

from __future__ import annotations

from typing import Any, Protocol


class _AgentResultLike(Protocol):
    """Agent result fields consumed by pipeline quality helpers."""

    success: bool
    output: Any
    errors: Any


class _AgentLike(Protocol):
    """Agent execution surface consumed by pipeline quality helpers."""

    def execute(self, task: Any) -> _AgentResultLike:
        """Execute an agent task."""


class _PipelineQualityOwner(Protocol):
    """Host contract required by pipeline quality helpers."""

    correction_loop_max_rounds: int

    def _get_agent(self, agent_type_str: str) -> _AgentLike | None:
        """Return an agent instance for the requested type, if available."""
