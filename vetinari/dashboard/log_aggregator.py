"""Log Aggregation Integration for Vetinari Dashboard  (Phase 4 Step 4).

Provides a unified interface for exporting structured log records to multiple
centralised logging backends, plus a lightweight in-process log store that the
dashboard can query without any external dependency.

Core components defined here
-----------------------------
  LogRecord       Structured log record dataclass.
  BackendBase     Abstract base class for all backends.
  LogAggregator   Central fan-out hub with in-process search buffer.
  AggregatorHandler  stdlib ``logging.Handler`` bridge.

Backend implementations live in ``vetinari.dashboard.log_backends``.
Import those classes directly from ``vetinari.dashboard.log_backends``.

Usage
-----
    from vetinari.constants import LOGS_DIR
    from vetinari.dashboard.log_aggregator import get_log_aggregator, LogRecord

    agg = get_log_aggregator()
    agg.configure_backend("file", path=str(LOGS_DIR / "audit.jsonl"))
    agg.ingest(LogRecord(
        message="Plan approved",
        level="INFO",
        trace_id="abc-123",
        extra={"plan_id": "plan_001", "risk_score": 0.12},
    ))

    # Search in-process store
    results = agg.search(trace_id="abc-123", limit=10)
"""

from __future__ import annotations

import json
import logging
import threading
import time
from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass, field
from typing import Any, cast

from vetinari.boundary_guards import account_evidence_drop
from vetinari.exceptions import ConfigurationError
from vetinari.safety.guardrails import redact_pii_payload
from vetinari.types import StatusEnum
from vetinari.utils.serialization import dataclass_to_dict

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Log record
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LogRecord:
    """A structured log record ready for aggregation."""

    message: str
    level: str = "INFO"  # DEBUG / INFO / WARNING / ERROR / CRITICAL
    timestamp: float = field(default_factory=time.time)
    trace_id: str | None = None
    span_id: str | None = None
    request_id: str | None = None
    logger_name: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def __repr__(self) -> str:
        return f"LogRecord(level={self.level!r}, trace_id={self.trace_id!r}, message={self.message[:60]!r})"

    def to_dict(self) -> dict[str, Any]:
        """Serialize structured fields plus any extra context into a flat dictionary.

        Extra fields are merged at the top level so consumers can access them
        directly (e.g. ``d["plan_id"]``) without nesting.

        Returns:
            Flat dictionary with all structured fields and extras merged in.
        """
        result = dataclass_to_dict(self)
        extras = result.pop("extra", {})
        result.update(extras)
        return cast(dict[str, Any], redact_pii_payload(result))

    def to_json(self) -> str:
        """Serialize this LogRecord to a JSON string.

        Returns:
            JSON-encoded string of the dictionary produced by ``to_dict()``.
        """
        return json.dumps(self.to_dict())


# ---------------------------------------------------------------------------
# Backend protocol
# ---------------------------------------------------------------------------


class BackendBase(ABC):
    """Abstract base for log aggregation backends."""

    name: str = "base"

    def configure(self, **kwargs: Any) -> None:
        """Apply backend-specific configuration.

        Default is a no-op. Subclasses override to handle config params.
        """
        return

    @abstractmethod
    def send(self, records: list[LogRecord]) -> bool:
        """Send a batch of records.

        Args:
            records: Structured log records to flush to the backend.

        Returns:
            True when the backend accepts the batch.

        Raises:
            NotImplementedError: Subclasses must implement batch delivery.
        """
        raise NotImplementedError

    def close(self) -> None:
        """Release any resources held by this backend.

        Default is a no-op. Subclasses override to clean up connections.
        """
        return


# ---------------------------------------------------------------------------
# Backend registry — populated after log_backends is imported below
# ---------------------------------------------------------------------------

# Populated lazily to avoid a circular import at module load time.
# LogAggregator.configure_backend() accesses this dict at call time,
# by which point log_backends is fully initialised.
_BACKEND_CLASSES: dict[str, type] = {}


# ---------------------------------------------------------------------------
# LogAggregator — core class
# ---------------------------------------------------------------------------


class LogAggregator:
    """Central log aggregation hub.

    * Maintains an in-process circular buffer of recent records for dashboard
      search / correlation.
    * Fans out each ingested batch to all configured backends.
    * Thread-safe singleton — obtain via ``get_log_aggregator()``.
    """

    _instance: LogAggregator | None = None
    _class_lock = threading.Lock()
    MAX_BUFFER = 5_000  # maximum records kept in memory

    def __new__(cls) -> LogAggregator:
        if cls._instance is None:
            with cls._class_lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._setup()
        return cls._instance

    # ------------------------------------------------------------------
    # Initialisation
    # ------------------------------------------------------------------

    def _setup(self) -> None:
        self._lock = threading.RLock()
        self._buffer: deque = deque(maxlen=self.MAX_BUFFER)
        self._backends: dict[str, BackendBase] = {}
        self._batch_size: int = 100
        self._pending: deque[LogRecord] = deque(maxlen=1000)

    # ------------------------------------------------------------------
    # Backend management
    # ------------------------------------------------------------------

    def configure_backend(self, name: str, **kwargs: Any) -> None:
        """Add and configure a named backend.

        Args:
            name: One of ``file``, ``datadog``, ``webhook``, ``sse``.
            **kwargs: Backend-specific configuration (see each class's
                      ``configure()`` docstring).

        Raises:
            ValueError: If ``name`` is not a recognised backend type.
        """
        if not _BACKEND_CLASSES:
            from vetinari.dashboard.log_backends import (
                DatadogBackend,
                FileBackend,
                SSEBackend,
                WebhookBackend,
            )

            _BACKEND_CLASSES.update({
                "file": FileBackend,
                "datadog": DatadogBackend,
                "webhook": WebhookBackend,
                "sse": SSEBackend,
            })
        cls = _BACKEND_CLASSES.get(name)
        if cls is None:
            raise ConfigurationError(f"Unknown backend '{name}'. Valid options: {sorted(_BACKEND_CLASSES)}")
        with self._lock:
            backend = cls()
            backend.configure(**kwargs)
            self._backends[name] = backend
            logger.info("Configured log aggregation backend: %s", name)

    def remove_backend(self, name: str) -> bool:
        """Remove a backend by name. Returns True if it existed.

        Returns:
            True if successful, False otherwise.
        """
        with self._lock:
            b = self._backends.pop(name, None)
            if b:
                b.close()
                logger.info("Removed log aggregation backend: %s", name)
            return b is not None

    def list_backends(self) -> list[str]:
        """List backends.

        Returns:
            Names of all currently configured backends (e.g. ``["file", "sse"]``).
        """
        with self._lock:
            return list(self._backends.keys())

    # ------------------------------------------------------------------
    # Ingestion
    # ------------------------------------------------------------------

    def ingest(self, record: LogRecord) -> None:
        """Ingest a single log record.

        The record is appended to the in-process buffer immediately and
        queued for backend dispatch. When the pending queue reaches
        ``_batch_size`` it is flushed automatically.
        """
        with self._lock:
            self._buffer.append(record)
            self._pending.append(record)
            if len(self._pending) >= self._batch_size:
                self._flush_locked()

    def ingest_many(self, records: list[LogRecord]) -> None:
        """Ingest multiple records at once."""
        for rec in records:
            self.ingest(rec)

    def flush(self) -> None:
        """Force-send all pending records to configured backends."""
        with self._lock:
            self._flush_locked()

    def _flush_locked(self) -> None:
        """Must be called while holding self._lock."""
        if not self._pending:
            return
        batch = list(self._pending)
        if not self._backends:
            self._pending.clear()
            return
        delivered = True
        for name, backend in self._backends.items():
            try:
                ok = backend.send(batch)
                if not ok:
                    delivered = False
                    account_evidence_drop(
                        logger=logger,
                        evidence_ref=f"log_batch:{name}",
                        reason="log_backend_delivery_failed",
                    )
                    logger.warning("Backend '%s' returned False for batch of %d", name, len(batch))
            except Exception as exc:
                delivered = False
                account_evidence_drop(
                    logger=logger,
                    evidence_ref=f"log_batch:{name}",
                    reason="log_backend_delivery_failed",
                )
                logger.error("Backend '%s' raised during send: %s", name, exc)
        if delivered:
            for _ in batch:
                self._pending.popleft()

    # ------------------------------------------------------------------
    # Search / correlation
    # ------------------------------------------------------------------

    def search(
        self,
        trace_id: str | None = None,
        level: str | None = None,
        logger_name: str | None = None,
        message_contains: str | None = None,
        since: float | None = None,  # unix timestamp
        limit: int = 100,
    ) -> list[LogRecord]:
        """Search the in-process buffer.

        All supplied filters are ANDed together.

        Args:
            trace_id:         Match records with this trace_id.
            level:            Match records with this log level (case-insensitive).
            logger_name:      Match records emitted by this logger.
            message_contains: Match records whose message contains this string.
            since:            Only return records with timestamp >= since.
            limit:            Maximum number of records to return (most recent first).

        Returns:
            List of matching LogRecord objects (newest first).
        """
        with self._lock:
            results: list[LogRecord] = []
            for rec in reversed(list(self._buffer)):
                if trace_id and rec.trace_id != trace_id:
                    continue
                if level and rec.level.upper() != level.upper():
                    continue
                if logger_name and rec.logger_name != logger_name:
                    continue
                if message_contains and message_contains not in rec.message:
                    continue
                if since is not None and rec.timestamp < since:
                    continue
                results.append(rec)
                if len(results) >= limit:
                    break
        return results

    def get_trace_records(self, trace_id: str) -> list[LogRecord]:
        """Return all records belonging to a trace, ordered oldest-first.

        Returns:
            List of results.
        """
        with self._lock:
            records = [r for r in self._buffer if r.trace_id == trace_id]
        return sorted(records, key=lambda r: r.timestamp)

    def correlate_span(self, trace_id: str, span_id: str) -> list[LogRecord]:
        """Return all records for a specific span within a trace.

        Args:
            trace_id: The trace id.
            span_id: The span id.

        Returns:
            List of results.
        """
        with self._lock:
            return [r for r in self._buffer if r.trace_id == trace_id and r.span_id == span_id]

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    def get_stats(self) -> dict[str, Any]:
        """Summarise current aggregator state.

        Returns:
            Dictionary with ``buffer_size`` (records in the circular in-process
            buffer), ``pending`` (records queued but not yet flushed to backends),
            ``backends`` (names of active backends), ``max_buffer`` (circular
            buffer capacity), and ``batch_size`` (auto-flush threshold).
        """
        with self._lock:
            return {
                "buffer_size": len(self._buffer),
                StatusEnum.PENDING.value: len(self._pending),
                "backends": list(self._backends.keys()),
                "max_buffer": self.MAX_BUFFER,
                "batch_size": self._batch_size,
            }

    def clear_buffer(self) -> None:
        """Discard all buffered records (for tests / memory management)."""
        with self._lock:
            self._buffer.clear()
            self._pending.clear()


# ---------------------------------------------------------------------------
# Python logging handler — bridge from stdlib logging to LogAggregator
# ---------------------------------------------------------------------------


class AggregatorHandler(logging.Handler):
    """A stdlib ``logging.Handler`` that feeds records into the ``LogAggregator``.

    Attach it to any logger to automatically capture structured log output:

        import logging
        from vetinari.dashboard.log_aggregator import AggregatorHandler

        root_logger = logging.getLogger()
        root_logger.addHandler(AggregatorHandler())
    """

    def emit(self, record: logging.LogRecord) -> None:
        """Emit for the current context."""
        try:
            agg = get_log_aggregator()
            lr = LogRecord(
                message=self.format(record),
                level=record.levelname,
                timestamp=record.created,
                trace_id=getattr(record, "trace_id", None),
                span_id=getattr(record, "span_id", None),
                request_id=getattr(record, "request_id", None),
                logger_name=record.name,
                extra={
                    k: v
                    for k, v in record.__dict__.items()
                    if k not in logging.LogRecord.__dict__
                    and not k.startswith("_")
                    and k
                    not in (
                        "message",
                        "asctime",
                        "args",
                        "msg",
                        "levelname",
                        "levelno",
                        "name",
                        "pathname",
                        "filename",
                        "module",
                        "exc_info",
                        "exc_text",
                        "stack_info",
                        "lineno",
                        "funcName",
                        "created",
                        "msecs",
                        "relativeCreated",
                        "thread",
                        "threadName",
                        "processName",
                        "process",
                    )
                },
            )
            agg.ingest(lr)
        except Exception:
            self.handleError(record)


# ---------------------------------------------------------------------------
# Singleton helpers
# ---------------------------------------------------------------------------


def get_log_aggregator() -> LogAggregator:
    """Return the global LogAggregator singleton."""
    return LogAggregator()


def reset_log_aggregator() -> None:
    """Destroy the singleton (for tests / clean shutdown)."""
    with LogAggregator._class_lock:
        if LogAggregator._instance is not None:
            LogAggregator._instance.flush()
        LogAggregator._instance = None
    logger.debug("LogAggregator singleton reset")


# ---------------------------------------------------------------------------
# Backward-compatible re-exports from log_backends
# ---------------------------------------------------------------------------
# Backend implementations live in log_backends.py to keep this module focused
# on the aggregator core. These re-exports preserve the public API so callers
# can write ``from vetinari.dashboard.log_aggregator import FileBackend``.
# These re-exports stay here instead of at module top.
# The imports MUST be here (not at the top) because log_backends itself imports
# BackendBase and LogRecord from this module — placing these at the top would
# create a true circular import failure.
from vetinari.dashboard.log_backends import (
    DatadogBackend,
    FileBackend,
    SSEBackend,
    WebhookBackend,
    get_sse_backend,
    reset_sse_backend,
)

__all__ = [
    "AggregatorHandler",
    "BackendBase",
    "DatadogBackend",
    "FileBackend",
    "LogAggregator",
    "LogRecord",
    "SSEBackend",
    "WebhookBackend",
    "get_log_aggregator",
    "get_sse_backend",
    "reset_log_aggregator",
    "reset_sse_backend",
]
