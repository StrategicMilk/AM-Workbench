"""In-memory pipeline checkpoint storage."""

from __future__ import annotations

from typing import Any


class InMemoryPipelineCheckpointStore:
    """Store named pipeline checkpoints in memory."""

    def __init__(self, name: str, store: dict[str, dict[str, Any]] | None = None) -> None:
        self.name = name
        self._store: dict[str, dict[str, Any]] = store if store is not None else {}

    def checkpoint(self, step: str, data: dict[str, Any]) -> None:
        """Record checkpoint data for a pipeline step.

        Args:
            step: Pipeline step identifier.
            data: Checkpoint payload.
        """
        self._store[step] = data

    def get_checkpoint(self, step: str) -> dict[str, Any] | None:
        """Return checkpoint data for a step, if present."""
        return self._store.get(step)


PipelineCheckpointStore = InMemoryPipelineCheckpointStore

__all__ = ["InMemoryPipelineCheckpointStore", "PipelineCheckpointStore"]
