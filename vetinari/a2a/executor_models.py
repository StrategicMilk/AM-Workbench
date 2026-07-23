"""Shared data models and status constants for the A2A executor."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from vetinari.types import AgentType, StatusEnum

STATUS_COMPLETED = StatusEnum.COMPLETED.value
STATUS_FAILED = StatusEnum.FAILED.value
STATUS_PENDING = StatusEnum.PENDING.value
STATUS_RUNNING = StatusEnum.RUNNING.value
STATUS_ACKNOWLEDGED = StatusEnum.ACKNOWLEDGED.value
STATUS_DEGRADED_UNRECOVERABLE = "degraded_unrecoverable"

# A2A-local recovery terminal state for tasks that were ACKNOWLEDGED on a
# previous run and cannot be re-executed on restart. This state is intentionally
# not added to StatusEnum because it is only meaningful inside A2A recovery.
STATUS_ORPHANED = "orphaned"


@dataclass
class A2ATask:
    """An incoming A2A task received from an external agent or caller.

    Attributes:
        task_id: Unique identifier for this task. Auto-generated if not provided.
        task_type: A2A task type string, such as ``"plan"`` or ``"build"``.
        input_data: Arbitrary input payload for the task.
        metadata: Optional caller-supplied metadata, such as headers or trace IDs.
        status: Current lifecycle status of the task.
    """

    task_type: str
    input_data: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    task_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    status: str = STATUS_PENDING

    def __repr__(self) -> str:
        return f"A2ATask(task_id={self.task_id!r}, task_type={self.task_type!r}, status={self.status!r})"


@dataclass
class A2AResult:
    """The result produced by executing an :class:`A2ATask`.

    Attributes:
        task_id: Identifier of the task that produced this result.
        status: Final lifecycle status.
        output_data: Structured output from the agent.
        error: Human-readable error description when execution fails.
    """

    task_id: str
    status: str
    output_data: dict[str, Any] = field(default_factory=dict)
    error: str = ""

    def __repr__(self) -> str:
        return f"A2AResult(task_id={self.task_id!r}, status={self.status!r})"

    def to_dict(self) -> dict[str, Any]:
        """Serialise this result to a plain dict for JSON transport.

        Returns:
            Dictionary representation of the result.
        """
        return {
            "taskId": self.task_id,
            "status": self.status,
            "outputData": self.output_data,
            "error": self.error,
        }


_RouteEntry = tuple[AgentType, str]
_RoutingTable = dict[str, _RouteEntry]
