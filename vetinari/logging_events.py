"""Event-logging helpers for structured logging.

Provides high-level functions for logging domain-specific events (tasks,
waves, API requests, sandbox execution) with standard field names and
levels. Uses :func:`get_logger` to emit events to the appropriate logger.

Usage::

    from vetinari.logging_events import log_task_start, log_task_complete

    log_task_start("task_123", task_type="inference")
    # ... do work ...
    log_task_complete("task_123", execution_time_ms=250.5)
"""

from __future__ import annotations

from typing import Any

from vetinari.privacy import PRIVACY_ENVELOPE_KEY, privacy_receipt, require_privacy_envelope
from vetinari.types import StatusEnum

_SENSITIVE_CONTEXT_KEY_MARKERS = ("secret", "token", "api_key", "apikey", "password", "credential")
_VALID_LOG_LEVELS = frozenset({"debug", "info", "warning", "error", "critical"})


def get_logger(name: str) -> Any:
    """Lazy import to avoid circular imports.

    Returns:
        StructuredLogger instance for the given name.
    """
    from vetinari.structured_logging import get_logger as _get_logger

    return _get_logger(name)


def _logging_context(
    logger_name: str,
    context: dict[str, Any],
    *,
    privacy_class: str = "operational",
    subject_id: str | None = None,
) -> dict[str, Any]:
    sanitized: dict[str, Any] = {}
    for key, value in context.items():
        lowered = key.lower()
        sanitized[key] = "[redacted]" if any(marker in lowered for marker in _SENSITIVE_CONTEXT_KEY_MARKERS) else value
    if PRIVACY_ENVELOPE_KEY in sanitized:
        require_privacy_envelope({PRIVACY_ENVELOPE_KEY: sanitized[PRIVACY_ENVELOPE_KEY]})
        return sanitized
    sanitized[PRIVACY_ENVELOPE_KEY] = privacy_receipt(
        privacy_class=privacy_class,
        subject_id=subject_id,
        source=f"log:{logger_name}",
        retention_days=30,
        redaction_applied=True,
    )
    return sanitized


def log_event(level: str, logger_name: str, message: str, **context: Any) -> None:
    """Log a structured event.

    Args:
        level: Log level (debug, info, warning, error, critical).
        logger_name: Name of the logger.
        message: Log message.
        **context: Additional context fields.

    Raises:
        ValueError: If ``level`` is not a supported log level name.
    """
    normalized_level = level.lower()
    if normalized_level not in _VALID_LOG_LEVELS:
        raise ValueError(f"Unknown log level: {level!r}. Valid levels: {sorted(_VALID_LOG_LEVELS)}")
    log = get_logger(logger_name)
    level_func = getattr(log, normalized_level)
    level_func(message, **_logging_context(logger_name, context))


def log_task_start(task_id: str, task_type: str = "generic", **kwargs: Any) -> None:
    """Log task start event.

    Args:
        task_id: The task id.
        task_type: The task type.
        **kwargs: Additional structured fields.
    """
    log = get_logger("executor")
    log.info(
        "Task started: %s",
        task_id,
        **_logging_context(
            "executor",
            {"event_type": "task_started", "task_id": task_id, "task_type": task_type, **kwargs},
        ),
    )


def log_task_complete(task_id: str, execution_time_ms: float, **kwargs: Any) -> None:
    """Log task completion event.

    Args:
        task_id: The task id.
        execution_time_ms: The execution time in milliseconds.
        **kwargs: Additional structured fields.
    """
    log = get_logger("executor")
    log.info(
        "Task completed: %s",
        task_id,
        **_logging_context(
            "executor",
            {
                "event_type": "task_completed",
                "task_id": task_id,
                "execution_time_ms": execution_time_ms,
                **kwargs,
            },
        ),
    )


def log_task_error(task_id: str, error: str, **kwargs: Any) -> None:
    """Log task error event.

    Args:
        task_id: The task id.
        error: The error description.
        **kwargs: Additional structured fields.
    """
    log = get_logger("executor")
    log.error(
        "Task failed: %s",
        task_id,
        **_logging_context("executor", {"event_type": "task_error", "task_id": task_id, "error": error, **kwargs}),
    )


def log_model_discovery(models_found: int, duration_ms: float, **kwargs: Any) -> None:
    """Log model discovery event.

    Args:
        models_found: Number of models discovered.
        duration_ms: Discovery duration in milliseconds.
        **kwargs: Additional structured fields.
    """
    log = get_logger("model_pool")
    log.info(
        "Model discovery completed: %d models",
        models_found,
        **_logging_context(
            "model_pool",
            {"event_type": "model_discovery", "models_found": models_found, "duration_ms": duration_ms, **kwargs},
        ),
    )


def log_wave_start(wave_id: str, task_count: int, **kwargs: Any) -> None:
    """Log wave start event.

    Args:
        wave_id: The wave identifier.
        task_count: Number of tasks in the wave.
        **kwargs: Additional structured fields.
    """
    log = get_logger("scheduler")
    log.info(
        "Wave started: %s",
        wave_id,
        **_logging_context(
            "scheduler",
            {"event_type": "wave_start", "wave_id": wave_id, "task_count": task_count, **kwargs},
        ),
    )


def log_wave_complete(wave_id: str, duration_ms: float, **kwargs: Any) -> None:
    """Log wave completion event.

    Args:
        wave_id: The wave identifier.
        duration_ms: Wave duration in milliseconds.
        **kwargs: Additional structured fields.
    """
    log = get_logger("scheduler")
    log.info(
        "Wave completed: %s",
        wave_id,
        **_logging_context(
            "scheduler",
            {"event_type": "wave_complete", "wave_id": wave_id, "duration_ms": duration_ms, **kwargs},
        ),
    )


def log_api_request(endpoint: str, method: str, status_code: int, duration_ms: float, **kwargs: Any) -> None:
    """Log API request event.

    Args:
        endpoint: The API endpoint path.
        method: HTTP method (GET, POST, etc.).
        status_code: Response status code.
        duration_ms: Request duration in milliseconds.
        **kwargs: Additional structured fields.
    """
    log = get_logger("web_ui")
    log.info(
        "API request: %s %s",
        method,
        endpoint,
        **_logging_context(
            "web_ui",
            {
                "event_type": "api_request",
                "endpoint": endpoint,
                "method": method,
                "status_code": status_code,
                "duration_ms": duration_ms,
                **kwargs,
            },
        ),
    )


def log_sandbox_execution(
    execution_id: str,
    success: bool,
    duration_ms: float,
    memory_mb: float,
    **kwargs: Any,
) -> None:
    """Log sandbox execution event.

    Args:
        execution_id: The execution identifier.
        success: Whether execution succeeded.
        duration_ms: Execution duration in milliseconds.
        memory_mb: Memory usage in megabytes.
        **kwargs: Additional structured fields.
    """
    log = get_logger("sandbox")
    level = log.info if success else log.error
    level(
        "Sandbox execution %s: %s",
        "success" if success else StatusEnum.FAILED.value,
        execution_id,
        **_logging_context(
            "sandbox",
            {
                "event_type": "sandbox_execution",
                "execution_id": execution_id,
                "success": success,
                "duration_ms": duration_ms,
                "memory_mb": memory_mb,
                **kwargs,
            },
        ),
    )
