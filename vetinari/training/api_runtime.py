"""Training runtime control helpers formerly exposed through Python web routes.

The Rust kernel owns HTTP routing. This module retains reusable training
status/control logic for CLI, scheduler, and runtime tests without registering
Python route handlers.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from vetinari.security.redaction import redact_value

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from vetinari.learning.prompt_evolver import PromptEvolver
    from vetinari.training.idle_scheduler import TrainingScheduler


def _run_promotion_check_after_shadow_complete(evolver: PromptEvolver) -> None:
    """Wire the shadow-test promotion gate into the training API.

    Called by the training API after a shadow test reports a completed pass
    so the prompt evolver's promotion gate fires from the same code path
    instead of being silently skipped. Resolved through ``sys.modules`` at
    call time to avoid the direct-import patch-seam drift anti-pattern.

    Args:
        evolver: The active :class:`PromptEvolver` instance whose shadow test
            just completed. ``_check_shadow_test_results`` is invoked
            through the live ``vetinari.learning.prompt_evolver_promotion``
            module so test patches at the module level take effect.
    """
    from vetinari.learning import prompt_evolver_promotion

    try:
        prompt_evolver_promotion._check_shadow_test_results(evolver)
    except Exception:
        logger.exception("Shadow-test promotion check raised - promotion gate not advanced")
        return
    logger.info("Shadow-test promotion check completed for evolver=%r", evolver)


@dataclass(frozen=True, slots=True)
class RuntimeResponse:
    """HTTP-like response payload for non-web runtime callers."""

    content: dict[str, Any] | None
    status_code: int
    media_type: str = "application/json"


TrainingAPIResponse = dict[str, Any] | RuntimeResponse


_scheduler_singleton: TrainingScheduler | None = None
_scheduler_lock = threading.Lock()


def _get_scheduler() -> TrainingScheduler | None:
    """Return the shared TrainingScheduler singleton."""
    global _scheduler_singleton
    try:
        from vetinari.training.idle_scheduler import get_training_scheduler

        scheduler = get_training_scheduler()
    except ImportError:
        logger.debug("Training scheduler modules not available")
        return None
    except Exception:
        logger.exception("Failed to obtain training scheduler singleton")
        return None
    with _scheduler_lock:
        _scheduler_singleton = scheduler
    return scheduler


def _is_scheduler_training() -> bool:
    """Return True when the shared TrainingScheduler has an active job."""
    scheduler = _get_scheduler()
    return scheduler is not None and scheduler.current_job is not None


def get_training_status() -> dict[str, Any]:
    """Aggregate current training pipeline state from available subsystems.

    Returns:
        Value produced for the caller.
    """
    records_collected = 0
    status = "idle"
    curriculum_phase = "unknown"
    next_activity = None
    last_run = None
    try:
        from vetinari.learning.training_data import get_training_collector

        records_collected = get_training_collector().get_stats().get("total", 0)
    except Exception as exc:
        logger.warning("get_training_status: training collector unavailable, defaulting records_collected=0: %s", exc)
    try:
        from vetinari.training.idle_scheduler import get_idle_detector

        detector = get_idle_detector()
        if detector is not None and not detector.idle:
            status = "running"
    except Exception as exc:
        logger.warning("get_training_status: idle detector unavailable, defaulting status='idle': %s", exc)
    try:
        from vetinari.training.curriculum import TrainingCurriculum

        curriculum = TrainingCurriculum()
        curriculum_phase = curriculum.get_status().get("phase", "unknown")
        activity = curriculum.next_activity()
        if activity is not None:
            next_activity = {
                "type": activity.type.value,
                "description": activity.description,
                "priority": activity.priority,
            }
    except Exception as exc:
        logger.warning("get_training_status: curriculum unavailable, defaulting phase='unknown': %s", exc)
    try:
        from vetinari.training.pipeline import BenchmarkTracker

        last_run = BenchmarkTracker().last_run()
    except Exception as exc:
        logger.warning("get_training_status: benchmark tracker unavailable, defaulting last_run=None: %s", exc)
    return {
        "status": status,
        "current_job": None,
        "last_run": last_run,
        "records_collected": records_collected,
        "curriculum_phase": curriculum_phase,
        "next_activity": next_activity,
    }


def get_training_history(*, limit: int = 50) -> list[dict[str, Any]]:
    """Return a merged, time-sorted list of training history entries.

    Returns:
        Value produced for the caller.
    """
    entries: list[dict[str, Any]] = []
    for source, getter_path in (
        ("quality_gate", "vetinari.training.quality_gate"),
        ("auto_tune", "vetinari.learning.auto_tuner"),
    ):
        try:
            if source == "quality_gate":
                from vetinari.training.quality_gate import get_training_quality_gate

                history = get_training_quality_gate().get_history()
            else:
                from vetinari.learning.auto_tuner import get_auto_tuner

                history = get_auto_tuner().get_history()
            entries.extend(_public_training_history_entry(entry, source=source) for entry in history)
        except Exception as exc:
            logger.warning("get_training_history: %s unavailable, omitting entries: %s", getter_path, exc)
    entries.sort(key=lambda e: e.get("timestamp", ""), reverse=True)
    return entries[:limit]


def _public_training_history_entry(entry: dict[str, Any], *, source: str) -> dict[str, Any]:
    """Return a redacted, API-safe training history entry."""
    if not isinstance(entry, dict):
        return {"type": source, "timestamp": "", "redaction_applied": True}
    redacted = redact_value(entry)
    if not isinstance(redacted, dict):
        return {"type": source, "timestamp": "", "redaction_applied": True}
    redacted["type"] = source
    redacted["redaction_applied"] = redacted != entry
    return redacted


def get_quality_comparison() -> dict[str, Any]:
    """Return the most recent quality gate comparison result.

    Returns:
        Value produced for the caller.
    """
    sentinel = {
        "baseline_quality": 0.0,
        "candidate_quality": 0.0,
        "quality_delta": 0.0,
        "decision": "no_data",
        "latency_ratio": 0.0,
    }
    try:
        from vetinari.training.quality_gate import get_training_quality_gate

        history = get_training_quality_gate().get_history()
        if not history:
            return sentinel
        latest = history[-1]
        return {
            "baseline_quality": latest.get("baseline_quality", 0.0),
            "candidate_quality": latest.get("candidate_quality", 0.0),
            "quality_delta": latest.get("quality_delta", 0.0),
            "decision": latest.get("decision", "no_data"),
            "latency_ratio": latest.get("latency_ratio", 0.0),
        }
    except Exception as exc:
        logger.warning("get_quality_comparison: quality gate unavailable, returning no_data sentinel: %s", exc)
        return sentinel


def _training_requirements_state() -> tuple[bool, list[str], bool]:
    try:
        from vetinari.training.pipeline import TrainingPipeline

        reqs = TrainingPipeline().check_requirements()
        libraries = reqs.get("libraries", {})
        return reqs.get("ready_for_training", False), [lib for lib, avail in libraries.items() if not avail], True
    except Exception as exc:
        logger.warning("training_status: pipeline requirements unavailable: %s", exc)
        return False, [], False


def _training_curriculum_state() -> tuple[str, Any, bool]:
    try:
        from vetinari.training.curriculum import TrainingCurriculum

        status_data = TrainingCurriculum().get_status()
        return status_data.get("phase", "unknown"), status_data.get("next_activity_description"), True
    except Exception as exc:
        logger.warning("training_status: curriculum unavailable: %s", exc)
        return "unknown", None, False


def _training_scheduler_state() -> tuple[bool, float, bool, Any, bool]:
    scheduler = _get_scheduler()
    if scheduler is None:
        return True, 0.0, False, None, False
    try:
        detector = scheduler._idle_detector
        job = scheduler.current_job
        activity = job.activity_description if job is not None else None
        return detector.idle, detector.idle_duration_minutes, scheduler.is_training, activity, True
    except Exception as exc:
        logger.warning("training_status: scheduler query failed: %s", exc)
        return True, 0.0, False, None, True


def _error_response(message: str, code: int, details: Any = None) -> RuntimeResponse:
    content: dict[str, Any] = {"status": "error", "message": message, "code": code}
    if details is not None:
        content["details"] = details
    return RuntimeResponse(content=content, status_code=code)


def training_status() -> TrainingAPIResponse:
    """Return current training scheduler state.

    Returns:
        Value produced for the caller.
    """
    ready, missing, pipeline_available = _training_requirements_state()
    phase, next_activity, curriculum_available = _training_curriculum_state()
    is_idle, idle_minutes, is_training, current_activity, scheduler_available = _training_scheduler_state()
    if not any((pipeline_available, curriculum_available, scheduler_available)):
        return _error_response("Training subsystem unavailable - no training modules could be loaded", 503)
    return {
        "status": "ok",
        "phase": phase,
        "is_idle": is_idle,
        "idle_minutes": idle_minutes,
        "is_training": is_training,
        "current_activity": current_activity,
        "next_activity": next_activity,
        "ready_for_training": ready,
        "missing_libraries": missing,
    }


def _validate_training_start_data(data: dict[str, Any]) -> RuntimeResponse | None:
    if not data:
        return RuntimeResponse(
            content={"status": "error", "message": "Request body must not be empty - provide at least one field"},
            status_code=422,
        )
    if "skill" not in data:
        return RuntimeResponse(
            content={
                "status": "error",
                "message": "Request body contains no recognised fields - provide a 'skill' field",
            },
            status_code=422,
        )
    skill = data.get("skill")
    if skill is None or not isinstance(skill, str) or not skill.strip():
        return RuntimeResponse(
            content={"status": "error", "message": "'skill' must be a non-empty string"},
            status_code=422,
        )
    return None


def _training_start_requirements_error() -> RuntimeResponse | None:
    try:
        from vetinari.training.pipeline import TrainingPipeline

        reqs = TrainingPipeline().check_requirements()
        if reqs.get("ready_for_training", False):
            return None
        missing = [lib for lib, avail in reqs.get("libraries", {}).items() if not avail]
        message = f"Training libraries not installed: {', '.join(missing)}"
        return RuntimeResponse(content={"status": "error", "message": message}, status_code=503)
    except Exception as exc:
        logger.warning(
            "training_start: requirements check failed - blocking start to prevent running without "
            "verified prerequisites; caller should surface this to the user: %s",
            exc,
        )
        return RuntimeResponse(
            content={"status": "error", "message": "Training prerequisites check failed - cannot start training"},
            status_code=503,
        )


def training_start(data: dict[str, Any]) -> TrainingAPIResponse:
    """Manually trigger a training cycle.

    Returns:
        Value produced for the caller.
    """
    if response := _validate_training_start_data(data):
        return response
    if response := _training_start_requirements_error():
        return response
    from vetinari.training.control import TrainingControlError, get_training_control_service

    try:
        receipt = get_training_control_service().start(skill=data.get("skill"))
    except TrainingControlError as exc:
        logger.warning("training_start: control service unavailable: %s", exc)
        return RuntimeResponse(
            content={"status": "error", "message": "Training scheduler not available"},
            status_code=503,
        )
    status_code = 200 if receipt.passed else 409
    if receipt.passed:
        return receipt.to_dict()
    return RuntimeResponse(content=receipt.to_dict(), status_code=status_code)


def training_pause(data: dict[str, Any]) -> TrainingAPIResponse:
    """Pause the active training job through the shared control service.

    Returns:
        JSON response or error response containing the control receipt.
    """
    from vetinari.training.control import get_training_control_service

    receipt = get_training_control_service().pause(job_id=data.get("job_id") if data else None)
    status_code = 200 if receipt.passed else 409
    if receipt.passed:
        return receipt.to_dict()
    return RuntimeResponse(content=receipt.to_dict(), status_code=status_code)


def training_resume(data: dict[str, Any]) -> TrainingAPIResponse:
    """Resume a paused training job through the shared control service.

    Returns:
        JSON response or error response containing the control receipt.
    """
    from vetinari.training.control import get_training_control_service

    receipt = get_training_control_service().resume(job_id=data.get("job_id") if data else None)
    status_code = 200 if receipt.passed else 409
    if receipt.passed:
        return receipt.to_dict()
    return RuntimeResponse(content=receipt.to_dict(), status_code=status_code)


def training_stop(data: dict[str, Any]) -> TrainingAPIResponse:
    """Stop the active training job through the shared control service.

    Returns:
        JSON response or error response containing the control receipt.
    """
    from vetinari.training.control import get_training_control_service

    receipt = get_training_control_service().stop(job_id=data.get("job_id") if data else None)
    status_code = 200 if receipt.passed else 409
    if receipt.passed:
        return receipt.to_dict()
    return RuntimeResponse(content=receipt.to_dict(), status_code=status_code)


def training_cancel(data: dict[str, Any] | None = None) -> TrainingAPIResponse:
    """Cancel a training job through the shared control service.

    Returns:
        JSON response or error response containing the control receipt.
    """
    from vetinari.training.control import TrainingControlError, get_training_control_service

    try:
        receipt = get_training_control_service().cancel(job_id=_control_job_id(data))
    except TrainingControlError:
        logger.warning("Training cancel rejected because control service is unavailable", exc_info=True)
        return RuntimeResponse(
            content={"status": "error", "message": "Training scheduler not available"},
            status_code=503,
        )
    status_code = 200 if receipt.passed else 409
    if receipt.passed:
        return receipt.to_dict()
    return RuntimeResponse(content=receipt.to_dict(), status_code=status_code)


def training_checkpoint(data: dict[str, Any] | None = None) -> TrainingAPIResponse:
    """Write a durable checkpoint through the shared training control service.

    Returns:
        JSON response or error response containing the control receipt.
    """
    from vetinari.training.control import TrainingControlError, get_training_control_service

    try:
        receipt = get_training_control_service().checkpoint(job_id=_control_job_id(data))
    except TrainingControlError:
        logger.warning("Training checkpoint rejected because control service is unavailable", exc_info=True)
        return RuntimeResponse(
            content={"status": "error", "message": "Training scheduler not available"},
            status_code=503,
        )
    status_code = 200 if receipt.passed else 409
    if receipt.passed:
        return receipt.to_dict()
    return RuntimeResponse(content=receipt.to_dict(), status_code=status_code)


def training_jobs() -> dict[str, Any]:
    """Return server-side training job state.

    Returns:
        JSON-serializable current and historical training jobs.
    """
    from dataclasses import asdict

    from vetinari.training.control import get_training_control_service

    return {"jobs": [asdict(job) for job in get_training_control_service().jobs()]}


def _control_job_id(data: dict[str, Any] | None) -> str | None:
    if not isinstance(data, dict):
        return None
    value = data.get("job_id")
    return value if isinstance(value, str) and value.strip() else None


def _next_training_activity_description() -> str | None:
    try:
        from vetinari.training.curriculum import TrainingCurriculum

        activity = TrainingCurriculum().next_activity()
        return activity.description if activity else None
    except Exception as exc:
        logger.warning("training/start: could not determine activity from curriculum: %s", exc)
        return None
