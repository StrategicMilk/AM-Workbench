"""Checkpoint persistence helpers for pipeline engine stages.

Pipeline checkpoints let replay and cost-analysis tools inspect completed
stage boundaries. Persistence failures are accounted and propagated so replay
evidence does not disappear silently.
"""

from __future__ import annotations

import logging
from typing import Any

from vetinari.boundary_guards import account_evidence_drop

logger = logging.getLogger(__name__)


def _checkpoint_item(
    *,
    trace_id: str,
    execution_id: str,
    step_name: str,
    step_index: int,
    status: str,
    output_snapshot: dict[str, Any],
) -> dict[str, Any]:
    return {
        "trace_id": trace_id,
        "execution_id": execution_id,
        "step_name": step_name,
        "step_index": step_index,
        "status": status,
        "output_snapshot": output_snapshot,
    }


def _persist_checkpoint(checkpoint: Any, item: dict[str, Any]) -> None:
    try:
        from vetinari.observability.checkpoints import get_checkpoint_store

        get_checkpoint_store().save_checkpoint(checkpoint)
    except Exception:
        account_evidence_drop(item, "checkpoint_persist", logger=logger)
        raise


def _save_pipeline_checkpoint(
    *,
    trace_id: str,
    execution_id: str,
    step_name: str,
    step_index: int,
    status: str,
    output_snapshot: dict[str, Any],
) -> None:
    """Persist a stage checkpoint and fail closed when evidence cannot be written.

    Args:
        trace_id: Trace identifier for the pipeline run.
        execution_id: Queue execution identifier for the run.
        step_name: Stable pipeline step name.
        step_index: Ordered step index used by replay tooling.
        status: Step completion status to record.
        output_snapshot: Small serializable snapshot of stage output.
    """
    item = _checkpoint_item(
        trace_id=trace_id,
        execution_id=execution_id,
        step_name=step_name,
        step_index=step_index,
        status=status,
        output_snapshot=output_snapshot,
    )
    try:
        from vetinari.observability.checkpoints import PipelineCheckpoint

        checkpoint = PipelineCheckpoint(
            trace_id=trace_id,
            execution_id=execution_id,
            step_name=step_name,
            step_index=step_index,
            status=status,
            output_snapshot=output_snapshot,
        )
    except Exception:
        account_evidence_drop(item, "replay_evidence", logger=logger)
        raise

    _persist_checkpoint(checkpoint, item)
