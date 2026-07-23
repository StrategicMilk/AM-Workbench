"""Remediation action implementations used by the tiered engine.

Advisory-Only Contract:
All action functions in this module record ``RemediationIntent`` entries via
``_record_action_state()`` and return ``True``. They do not call live
subsystems directly; downstream callers consume the queued intents through
``get_remediation_action_intents()`` in ``RemediationEngine.execute_remediation``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

_ACTION_STATE: dict[str, int] = {}
_ACTION_INTENTS: list[RemediationIntent] = []
_ADVISORY_ONLY: bool = True


@dataclass(frozen=True, slots=True)
class RemediationIntent:
    """Executable remediation request produced by an automatic action."""

    action_name: str
    target_subsystem: str
    requested_effect: str
    applies_to_next: str

    def __repr__(self) -> str:
        return (
            "RemediationIntent("
            f"action_name={self.action_name!r}, target_subsystem={self.target_subsystem!r}, "
            f"requested_effect={self.requested_effect!r}, applies_to_next={self.applies_to_next!r})"
        )


def _record_action_state(
    action_name: str,
    *,
    target_subsystem: str,
    requested_effect: str,
    applies_to_next: str,
) -> bool:
    _ACTION_STATE[action_name] = _ACTION_STATE.get(action_name, 0) + 1
    _ACTION_INTENTS.append(
        RemediationIntent(
            action_name=action_name,
            target_subsystem=target_subsystem,
            requested_effect=requested_effect,
            applies_to_next=applies_to_next,
        )
    )
    return True


def get_remediation_action_state_snapshot() -> dict[str, int]:
    """Return observed built-in remediation side effects for verification."""
    return dict(_ACTION_STATE)


def get_remediation_action_intents() -> tuple[RemediationIntent, ...]:
    """Return remediation intents that downstream runtime components can consume."""
    return tuple(_ACTION_INTENTS)


def reset_remediation_action_state() -> None:
    """Clear observed remediation side effects for tests."""
    _ACTION_STATE.clear()
    _ACTION_INTENTS.clear()


def _reduce_context_size() -> bool:
    """Attempt to recover from OOM by signalling a context size reduction.

    Returns:
        True — the signal is best-effort; execution continues after logging.
    """
    # Advisory signal only: no live subsystem call; intent queued to _ACTION_INTENTS for RemediationEngine consumption.
    logger.info("OOM remediation: requesting context size reduction for next inference")
    return _record_action_state(
        "context_size_reduction_requested",
        target_subsystem="inference_context",
        requested_effect="reduce_context_window",
        applies_to_next="inference_request",
    )


def _switch_to_smaller_model() -> bool:
    """Request a fallback to a smaller model to address memory pressure.

    Returns:
        True — the model router will honor this on the next request.
    """
    # Advisory signal only: no live subsystem call; intent queued to _ACTION_INTENTS for RemediationEngine consumption.
    logger.info("OOM remediation: requesting switch to smaller model via model router")
    return _record_action_state(
        "smaller_model_switch_requested",
        target_subsystem="model_router",
        requested_effect="prefer_smaller_memory_footprint",
        applies_to_next="model_selection",
    )


def _cancel_and_retry() -> bool:
    """Cancel the stalled agent task and queue a fresh retry.

    Returns:
        True — cancellation is advisory; the scheduler acts on it.
    """
    # Advisory signal only: no live subsystem call; intent queued to _ACTION_INTENTS for RemediationEngine consumption.
    logger.info("Hang remediation: cancelling stalled task and scheduling retry")
    return _record_action_state(
        "stalled_task_retry_requested",
        target_subsystem="task_scheduler",
        requested_effect="cancel_and_retry",
        applies_to_next="stalled_task",
    )


def _retry_with_refinement() -> bool:
    """Re-run the last task with a refined prompt to improve quality.

    Returns:
        True — prompt refinement is queued for the next execution slot.
    """
    # Advisory signal only: no live subsystem call; intent queued to _ACTION_INTENTS for RemediationEngine consumption.
    logger.info("Quality-drop remediation: queuing retry with prompt refinement")
    return _record_action_state(
        "prompt_refinement_retry_requested",
        target_subsystem="prompt_pipeline",
        requested_effect="retry_with_refinement",
        applies_to_next="failed_task_retry",
    )


def _switch_model() -> bool:
    """Switch to an alternative model with better quality characteristics.

    Returns:
        True — model selection override is recorded for next inference.
    """
    # Advisory signal only: no live subsystem call; intent queued to _ACTION_INTENTS for RemediationEngine consumption.
    logger.info("Quality-drop remediation: requesting model switch for quality improvement")
    return _record_action_state(
        "quality_model_switch_requested",
        target_subsystem="model_router",
        requested_effect="prefer_higher_quality_model",
        applies_to_next="model_selection",
    )


def _clear_caches() -> bool:
    """Clear non-essential caches to free disk space.

    Returns:
        True — cache directories are cleared; training artifacts are preserved.
    """
    # Advisory signal only: no live subsystem call; intent queued to _ACTION_INTENTS for RemediationEngine consumption.
    logger.info("Disk-full remediation: clearing non-essential caches to free space")
    return _record_action_state(
        "cache_clear_requested",
        target_subsystem="cache_manager",
        requested_effect="clear_nonessential_caches",
        applies_to_next="storage_recovery",
    )


def _pause_training() -> bool:
    """Pause the training pipeline to stop writing new artifacts to disk.

    Returns:
        True — pause signal is set; idle scheduler will not start new runs.
    """
    # Advisory signal only: no live subsystem call; intent queued to _ACTION_INTENTS for RemediationEngine consumption.
    logger.info("Disk-full remediation: pausing training pipeline to halt artifact writes")
    return _record_action_state(
        "training_pause_requested",
        target_subsystem="training_pipeline",
        requested_effect="pause_new_runs",
        applies_to_next="training_scheduler",
    )


def _reduce_batch_size() -> bool:
    """Reduce inference batch size to lower GPU load and heat output.

    Returns:
        True — batch size reduction is applied to the next inference call.
    """
    # Advisory signal only: no live subsystem call; intent queued to _ACTION_INTENTS for RemediationEngine consumption.
    logger.info("Thermal remediation: reducing inference batch size to lower GPU load")
    return _record_action_state(
        "batch_size_reduction_requested",
        target_subsystem="inference_scheduler",
        requested_effect="reduce_batch_size",
        applies_to_next="inference_request",
    )


def _pause_and_cooldown() -> bool:
    """Pause inference briefly to allow GPU temperature to drop.

    Returns:
        True — cooldown pause is registered; scheduler will honour it.
    """
    # Advisory signal only: no live subsystem call; intent queued to _ACTION_INTENTS for RemediationEngine consumption.
    logger.info("Thermal remediation: pausing inference to allow GPU cooldown")
    return _record_action_state(
        "inference_cooldown_requested",
        target_subsystem="inference_scheduler",
        requested_effect="pause_for_cooldown",
        applies_to_next="inference_queue",
    )


def _alert_operator(failure_mode: Any) -> Callable[[], bool]:
    """Build an alert action closure for the given failure mode.

    Args:
        failure_mode: The failure mode to include in the alert message.

    Returns:
        A zero-argument callable that logs the alert and returns True.
    """

    def _alert() -> bool:
        # Advisory signal only: no live subsystem call; intent queued to _ACTION_INTENTS for RemediationEngine consumption.
        logger.warning(
            "OPERATOR ALERT: failure mode '%s' could not be auto-resolved — manual intervention may be required",
            failure_mode.value,
        )
        return _record_action_state(
            f"operator_alert:{failure_mode.value}",
            target_subsystem="operator_alerts",
            requested_effect="notify_operator",
            applies_to_next=failure_mode.value,
        )

    return _alert


def _pause_pipeline(failure_mode: Any) -> Callable[[], bool]:
    """Build a pipeline-pause action closure for the given failure mode.

    Args:
        failure_mode: The failure mode that triggered the pause.

    Returns:
        A zero-argument callable that signals a pipeline pause and returns True.
    """

    def _pause() -> bool:
        # Advisory signal only: no live subsystem call; intent queued to _ACTION_INTENTS for RemediationEngine consumption.
        logger.warning(
            "PIPELINE PAUSE: halting pipeline due to unresolved '%s' failure — awaiting operator intervention",
            failure_mode.value,
        )
        return _record_action_state(
            f"pipeline_pause:{failure_mode.value}",
            target_subsystem="pipeline_controller",
            requested_effect="pause_pipeline",
            applies_to_next=failure_mode.value,
        )

    return _pause


# ── Engine ────────────────────────────────────────────────────────────
