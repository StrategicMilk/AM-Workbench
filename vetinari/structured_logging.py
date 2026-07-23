"""Structured JSON Logging Module for Vetinari — Public Facade.

Provides structured logging via structlog with:
- JSON or console rendering (configurable)
- Distributed tracing via CorrelationContext (contextvars-based)
- Context fields (execution_id, task_id, wave_id, plan_id)
- Processor chain: contextvars → correlation → service context → timestamper → renderer

Decision: structlog is a required core dependency (ADR-0064)

Usage::

    from vetinari.structured_logging import get_logger, log_event

    logger = get_logger("orchestrator")
    logger.info("Task started", task_id="task_1")

    log_event("info", "orchestrator", "Task completed", task_id="task_1")

This module re-exports the public API from:
- :mod:`vetinari.logging_context` — CorrelationContext and ID getters/setters
- :mod:`vetinari.logging_events` — event-logging functions
- :mod:`vetinari.logging_decorators` — timing and trace decorators
"""

from __future__ import annotations

import logging
import os
import sys
import threading
from enum import Enum
from typing import Any

import structlog

from vetinari.logging_context import (
    CorrelationContext,
    _request_id_var,
    _span_id_var,
    _trace_id_var,
    clear_request_id,
    get_plan_id,
    get_request_id,
    get_span_id,
    get_trace_id,
    set_request_id,
)
from vetinari.logging_decorators import (
    async_timed_operation,
    async_traced_operation,
    timed_operation,
    traced_operation,
)
from vetinari.logging_events import (
    log_api_request,
    log_event,
    log_model_discovery,
    log_sandbox_execution,
    log_task_complete,
    log_task_error,
    log_task_start,
    log_wave_complete,
    log_wave_start,
)

logger = logging.getLogger(__name__)


class LogLevel(Enum):
    """Log level."""

    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


def _add_correlation_context(
    logger_obj: Any,
    method_name: str,
    event_dict: dict[str, Any],
) -> dict[str, Any]:
    """Inject trace/span/request/plan IDs from ContextVars.

    Args:
        logger_obj: The logger instance (unused).
        method_name: The log method name (unused).
        event_dict: The current event dict to enrich.

    Returns:
        Enriched event dict with correlation context.
    """
    _ = (logger_obj, method_name)
    trace_id = _trace_id_var.get()
    span_id = _span_id_var.get()
    request_id = _request_id_var.get()
    plan_id = get_plan_id()

    if trace_id:
        event_dict.setdefault("trace_id", trace_id)
    if span_id:
        event_dict.setdefault("span_id", span_id)
    if request_id:
        event_dict.setdefault("request_id", request_id)
    if plan_id:
        event_dict.setdefault("plan_id", plan_id)

    return event_dict


def _add_service_context(
    logger_obj: Any,
    method_name: str,
    event_dict: dict[str, Any],
) -> dict[str, Any]:
    """Add service-level context fields.

    Args:
        logger_obj: The logger instance (unused).
        method_name: The log method name (unused).
        event_dict: The current event dict to enrich.

    Returns:
        Enriched event dict with service context.
    """
    _ = (logger_obj, method_name)
    event_dict.setdefault("service", "vetinari")
    event_dict.setdefault("version", os.environ.get("VETINARI_VERSION", "unknown"))
    return event_dict


class StructuredLogger:
    """Structured logger wrapping structlog for rich context binding.

    Provides %-style message formatting for backward compatibility while
    delegating all output through structlog's processor chain.
    """

    def __init__(self, name: str):
        self.name = name
        self._logger = structlog.get_logger(name)

    def set_context(self, **kwargs: Any) -> StructuredLogger:
        """Return a new StructuredLogger view with extra context fields bound.

        Does NOT mutate this instance — the shared cached logger returned by
        :func:`get_logger` is unaffected, so other callers that retrieved the
        same logger do not inherit the caller-specific context.

        Args:
            **kwargs: Additional structured context fields to bind.

        Returns:
            A new :class:`StructuredLogger` instance with the extra fields
            attached to every log line it emits.
        """
        child = StructuredLogger.__new__(StructuredLogger)
        child.name = self.name
        child._logger = self._logger.bind(**kwargs)
        return child

    def _log(self, level: str, message: str, *args: Any, **kwargs: Any) -> None:
        """Format and dispatch a log event.

        Args:
            level: Log level name (debug, info, warning, error, critical).
            message: Log message (supports %-style formatting).
            *args: Positional arguments for %-style formatting.
            **kwargs: Additional structured context fields.
        """
        if args:
            message = message % args
        log_method = getattr(self._logger, level, self._logger.info)
        log_method(message, **kwargs)

    def debug(self, message: str, *args: Any, **kwargs: Any) -> None:
        """Log a debug message with context.

        Args:
            message: The log message (supports %-style formatting).
            *args: Positional arguments for %-style string formatting.
            **kwargs: Additional structured context fields.
        """
        self._log("debug", message, *args, **kwargs)

    def info(self, message: str, *args: Any, **kwargs: Any) -> None:
        """Log an info message with context.

        Args:
            message: The log message (supports %-style formatting).
            *args: Positional arguments for %-style string formatting.
            **kwargs: Additional structured context fields.
        """
        self._log("info", message, *args, **kwargs)

    def warning(self, message: str, *args: Any, **kwargs: Any) -> None:
        """Log a warning message with context.

        Args:
            message: The log message (supports %-style formatting).
            *args: Positional arguments for %-style string formatting.
            **kwargs: Additional structured context fields.
        """
        self._log("warning", message, *args, **kwargs)

    def error(self, message: str, *args: Any, **kwargs: Any) -> None:
        """Log an error message with context.

        Args:
            message: The log message (supports %-style formatting).
            *args: Positional arguments for %-style string formatting.
            **kwargs: Additional structured context fields.
        """
        self._log("error", message, *args, **kwargs)

    def critical(self, message: str, *args: Any, **kwargs: Any) -> None:
        """Log a critical message with context.

        Args:
            message: The log message (supports %-style formatting).
            *args: Positional arguments for %-style string formatting.
            **kwargs: Additional structured context fields.
        """
        self._log("critical", message, *args, **kwargs)

    def exception(self, message: str, *args: Any, **kwargs: Any) -> None:
        """Log an exception with full traceback.

        Args:
            message: The log message (supports %-style formatting).
            *args: Positional arguments for %-style string formatting.
            **kwargs: Additional structured context fields.
        """
        self._log("exception", message, *args, **kwargs)


_loggers: dict[str, StructuredLogger] = {}
_loggers_lock = threading.Lock()
_configured = False
_log_level = logging.INFO
_use_json = False


def _get_log_level() -> int:
    """Get log level from environment variable."""
    level_str = os.environ.get("VETINARI_LOG_LEVEL", "INFO").upper()
    return getattr(logging, level_str, logging.INFO)


def _use_structured_logging() -> bool:
    """Check if JSON output is enabled.

    Returns True when VETINARI_LOG_FORMAT is "json" or when
    VETINARI_STRUCTURED_LOGGING is truthy.
    """
    log_format = os.environ.get("VETINARI_LOG_FORMAT", "").lower()
    if log_format == "json":
        return True
    if log_format == "text":
        return False
    return os.environ.get("VETINARI_STRUCTURED_LOGGING", "false").lower() in ("1", "true", "yes")


def configure_logging() -> None:
    """Configure structlog processor chain for the Vetinari application.

    Environment variables:
    - VETINARI_LOG_FORMAT — "json" for JSON, "text" for console.
    - VETINARI_STRUCTURED_LOGGING — alternative flag
    - VETINARI_LOG_LEVEL — log level string (default "INFO")

    Safe to call multiple times; subsequent calls are no-ops.
    """
    global _configured, _log_level, _use_json

    if _configured:
        return

    _log_level = _get_log_level()
    _use_json = _use_structured_logging()

    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        _add_correlation_context,
        _add_service_context,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    if _use_json:
        renderer = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer()

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    root_logger = logging.getLogger()
    root_logger.setLevel(_log_level)
    root_logger.handlers.clear()

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                renderer,
            ],
        ),
    )
    root_logger.addHandler(handler)

    _configured = True


def get_logger(name: str) -> StructuredLogger:
    """Get a structured logger instance.

    Args:
        name: Logger name (typically module name).

    Returns:
        StructuredLogger instance backed by structlog.
    """
    global _loggers

    if not _configured:
        configure_logging()

    if name not in _loggers:
        with _loggers_lock:
            if name not in _loggers:
                _loggers[name] = StructuredLogger(name)

    return _loggers[name]


__all__ = [
    "CorrelationContext",
    "LogLevel",
    "StructuredLogger",
    "async_timed_operation",
    "async_traced_operation",
    "clear_request_id",
    "configure_logging",
    "get_logger",
    "get_plan_id",
    "get_request_id",
    "get_span_id",
    "get_trace_id",
    "log_api_request",
    "log_event",
    "log_model_discovery",
    "log_sandbox_execution",
    "log_task_complete",
    "log_task_error",
    "log_task_start",
    "log_wave_complete",
    "log_wave_start",
    "set_request_id",
    "timed_operation",
    "traced_operation",
]
