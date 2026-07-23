"""Run-kernel handle and timestamp conversion helpers."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from vetinari.workbench.session_kernel import (
    RecoveryAction,
    RunHandle,
    RunKernelStatus,
    RunSessionSnapshot,
    RunStepState,
)

logger = logging.getLogger(__name__)


_TERMINAL_STATUSES = {
    RunKernelStatus.SUCCEEDED,
    RunKernelStatus.FAILED,
    RunKernelStatus.BLOCKED,
}


def _stream_id(project_id: str, run_id: str) -> str:
    return f"stream.{project_id}.{run_id}"


def _handle_from_snapshot(snapshot: RunSessionSnapshot) -> RunHandle:
    step_state = _step_state_from_snapshot(snapshot)
    terminal = snapshot.status in _TERMINAL_STATUSES
    return RunHandle(
        project_id=snapshot.project_id,
        run_id=snapshot.run_id,
        stream_id=_stream_id(snapshot.project_id, snapshot.run_id),
        status=snapshot.status,
        step_state=step_state,
        can_stop=snapshot.status is RunKernelStatus.RUNNING,
        can_checkpoint=snapshot.status in {RunKernelStatus.RUNNING, RunKernelStatus.INTERRUPTED},
        can_resume=snapshot.checkpoint.sealed and snapshot.status is not RunKernelStatus.SUCCEEDED,
        can_wait=not terminal,
        can_read_result=snapshot.status is RunKernelStatus.SUCCEEDED and bool(snapshot.final_verdict_ref),
    )


def _step_state_from_snapshot(snapshot: RunSessionSnapshot) -> RunStepState:
    if snapshot.status is RunKernelStatus.SUCCEEDED:
        return RunStepState.COMPLETED
    if snapshot.status is RunKernelStatus.FAILED:
        return RunStepState.FAILED
    if snapshot.status is RunKernelStatus.BLOCKED:
        return RunStepState.BLOCKED
    if snapshot.status is RunKernelStatus.INTERRUPTED:
        return RunStepState.INTERRUPTED
    if snapshot.recovery_action is RecoveryAction.RESUME:
        return RunStepState.RESUMING
    for event in reversed(snapshot.events):
        if event.startswith("step-receipt:"):
            parts = event.split(":")
            if len(parts) >= 5:
                try:
                    return RunStepState(parts[4])
                except ValueError:
                    logger.warning("Handled recoverable failure before fallback.", exc_info=True)
                    return RunStepState.RUNNING_MODEL
    return RunStepState.PLANNING


def _parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware")
    return parsed.astimezone(UTC)
