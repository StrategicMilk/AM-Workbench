"""Durable execution database compatibility types.

Checkpoint storage is owned by :mod:`vetinari.orchestration.checkpoint_store`.
This module keeps the older import surface for ``DurableExecutionEngine`` and
tests without carrying a second SQLite wrapper implementation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from vetinari.orchestration.checkpoint_store import _SCHEMA_SQL
from vetinari.orchestration.checkpoint_store import (
    CheckpointDatabaseManager as _DatabaseManager,
)

__all__ = [
    "_SCHEMA_SQL",
    "CheckpointSnapshot",
    "ExecutionEventRecord",
    "_DatabaseManager",
]


@dataclass(frozen=True, slots=True)
class ExecutionEventRecord:
    """An immutable event in the execution history.

    Attributes:
        event_id: Unique UUID for this event.
        event_type: Category string (e.g. task_started, task_completed, task_failed).
        task_id: The task this event relates to.
        timestamp: ISO-8601 UTC creation timestamp.
        data: Additional event payload.
    """

    event_id: str
    event_type: str
    task_id: str
    timestamp: str
    data: dict[str, Any] = field(default_factory=dict)

    def __repr__(self) -> str:
        return (
            f"ExecutionEventRecord(event_id={self.event_id!r}, event_type={self.event_type!r}, "
            f"task_id={self.task_id!r}, timestamp={self.timestamp!r})"
        )


@dataclass
class CheckpointSnapshot:
    """A snapshot of execution state for crash recovery.

    Attributes:
        checkpoint_id: Unique checkpoint identifier.
        plan_id: The plan this checkpoint belongs to.
        created_at: ISO-8601 creation timestamp.
        graph_state: Full serialized ExecutionGraph dict.
        completed_tasks: IDs of tasks that have finished.
        running_tasks: IDs of tasks currently in-flight.
        metadata: Optional extra data for debugging.
    """

    checkpoint_id: str
    plan_id: str
    created_at: str
    graph_state: dict[str, Any]
    completed_tasks: list[str]
    running_tasks: list[str]
    metadata: dict[str, Any] = field(default_factory=dict)

    def __repr__(self) -> str:
        return (
            f"CheckpointSnapshot(checkpoint_id={self.checkpoint_id!r}, plan_id={self.plan_id!r}, "
            f"completed_tasks={len(self.completed_tasks)}, "
            f"running_tasks={len(self.running_tasks)})"
        )
