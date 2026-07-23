"""Checkpoint and recovery helpers for DurableExecutionEngine.

Extracted from ``durable_execution.py`` to keep that file under 550 lines.

Pipeline role: Plan -> DurableExecution -> **Recovery/Checkpoint** -> Verify.
"""

from __future__ import annotations

from vetinari.orchestration.durable_execution_checkpointing import (
    emit_event,
    handle_layer_failure,
    load_checkpoint,
    record_learning,
    save_checkpoint,
)
from vetinari.orchestration.durable_execution_lifecycle import (
    answer_paused_questions,
    cleanup_completed,
    get_execution_status,
    get_paused_questions,
    list_checkpoints,
    recover_execution,
    recover_incomplete_executions,
    save_paused_questions,
)

__all__ = [
    "answer_paused_questions",
    "cleanup_completed",
    "emit_event",
    "get_execution_status",
    "get_paused_questions",
    "handle_layer_failure",
    "list_checkpoints",
    "load_checkpoint",
    "record_learning",
    "recover_execution",
    "recover_incomplete_executions",
    "save_checkpoint",
    "save_paused_questions",
]
