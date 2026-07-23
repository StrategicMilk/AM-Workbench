"""Backend state and configuration for GenAI tracing."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from urllib.parse import urlsplit

from vetinari.clock import Clock, SystemClock

logger = logging.getLogger(__name__)

_VALID_BACKENDS = frozenset({"noop", "jaeger", "file"})
_backend: str = "noop"


def _endpoint_log_label(endpoint: str) -> str:
    """Return a non-secret endpoint label for logs."""
    if not endpoint:
        return ""
    parsed = urlsplit(endpoint)
    if parsed.scheme and parsed.netloc:
        host = parsed.hostname or ""
        port = f":{parsed.port}" if parsed.port else ""
        return f"{parsed.scheme}://{host}{port}"
    return "[endpoint-configured]"


def _get_backend() -> str:
    """Return the raw active backend name for internal tracer checks."""
    return _backend


def configure_backend(backend_type: str, endpoint: str = "") -> None:
    """Configure the tracing backend for this process.

    Args:
        backend_type: Backend identifier, one of ``noop``, ``jaeger``, or ``file``.
        endpoint: Optional OTLP endpoint used by the jaeger backend.

    Raises:
        ValueError: If ``backend_type`` is not supported.
    """
    global _backend
    from vetinari.observability import otel_genai as owner

    if backend_type not in _VALID_BACKENDS:
        raise ValueError(f"Invalid OTel backend {backend_type!r} -- must be one of {sorted(_VALID_BACKENDS)}")

    owner._backend_initialized = True
    if backend_type == "jaeger":
        if not owner._OTEL_AVAILABLE:
            logger.warning(
                "configure_backend('jaeger') requested but opentelemetry SDK is not "
                "installed -- falling back to noop export (spans recorded in-process only)"
            )
            _backend = "noop"
            logger.debug("OTel backend configured: noop (jaeger requested but SDK unavailable)")
            owner.reset_genai_tracer()
            return
        endpoint = endpoint or os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")
        logger.info("OTel backend set to jaeger -- endpoint=%s", _endpoint_log_label(endpoint))

    _backend = backend_type
    logger.debug("OTel backend configured: %s", backend_type)
    owner.reset_genai_tracer()


def get_active_backend() -> str:
    """Return the name of the currently active tracing backend."""
    return _backend


def _trace_file_path(clock: Clock | None = None) -> Path:
    active_clock = clock or SystemClock()
    timestamp = active_clock.utc_now().strftime("%Y%m%dT%H%M%S%fZ")
    return Path("outputs") / "traces" / f"traces_{timestamp}.json"


def flush_file_backend() -> int:
    """Write completed spans to disk when the file backend is active.

    Returns:
        Number of exported spans, or 0 when the active backend is not ``file``.
    """
    if _backend != "file":
        return 0

    from vetinari.observability.otel_genai import get_genai_tracer

    filepath = _trace_file_path()
    filepath.parent.mkdir(parents=True, exist_ok=True)
    return get_genai_tracer().export_traces(str(filepath))


def _init_backend_from_env() -> None:
    """Read environment variables and configure the tracing backend."""
    raw = os.environ.get("VETINARI_OTEL_BACKEND", "noop").strip().lower()
    endpoint = os.environ.get("VETINARI_OTEL_ENDPOINT", "")
    if raw not in _VALID_BACKENDS:
        logger.warning(
            "Unrecognised VETINARI_OTEL_BACKEND value %r -- defaulting to 'noop'. Valid values are: %s",
            raw,
            sorted(_VALID_BACKENDS),
        )
        configure_backend("noop")
        return
    configure_backend(raw, endpoint)
