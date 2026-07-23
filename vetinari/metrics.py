"""Metrics collection module for Vetinari.

Provides a thread-safe MetricsCollector for counters and histograms,
a module-level singleton, and convenience helper functions for
common Vetinari metrics (task duration, task count, model latency,
API request counts).

Usage:
    from vetinari.metrics import get_metrics, record_task_duration

    record_task_duration("task-123", 42.0)
    get_metrics().get_counter("vetinari.task.count", status=StatusEnum.COMPLETED.value)
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
from collections import deque
from pathlib import Path
from typing import Any

from vetinari.security.fail_closed import sanitize_untrusted_text

logger = logging.getLogger(__name__)

_TAG_SAFE_RE = re.compile(r"[^a-zA-Z0-9_.:-]+")
_SNAPSHOT_KEYS = frozenset({"schema_version", "counters", "histograms"})


def _stable_metric_tag(value: object, *, high_cardinality: bool = False) -> str:
    """Return a bounded metric tag that avoids raw high-cardinality values."""
    raw_value = str(value or "unknown")
    raw = sanitize_untrusted_text(raw_value, max_length=512)
    if high_cardinality:
        return hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()[:16]
    normalized = _TAG_SAFE_RE.sub("_", raw.strip().lower())[:64].strip("_")
    return normalized or "unknown"


class MetricsCollector:
    """Thread-safe metrics collector for observability.

    Collects counters and histograms for monitoring. All operations
    acquire an internal lock, making the collector safe to use from
    multiple threads simultaneously.
    """

    def __init__(self, persistence_path: str | Path | None = None) -> None:
        """Initialise empty counter and histogram stores."""
        self._counters: dict[str, int] = {}
        self._histograms: dict[str, deque[float]] = {}
        self._lock = threading.Lock()
        self._snapshot_version = 0
        configured_path = persistence_path or os.environ.get("VETINARI_METRICS_SNAPSHOT_PATH")
        self._persistence_path = Path(configured_path) if configured_path else None
        self._restore()

    def increment(self, metric_name: str, value: int = 1, **tags: object) -> None:
        """Increment a counter metric.

        Args:
            metric_name: The metric name.
            value: Amount to add to the counter (default 1).
            **tags: Dimension tags used to qualify the metric key.
        """
        key = self._make_key(metric_name, tags)
        payload: dict[str, Any] | None
        version: int
        with self._lock:
            self._counters[key] = self._counters.get(key, 0) + value
            payload, version = self._snapshot_payload_locked()
        self._persist_snapshot_payload(payload, version)

    def record(self, metric_name: str, value: float, **tags: object) -> None:
        """Record a histogram value.

        Args:
            metric_name: The metric name.
            value: The numeric observation to record.
            **tags: Dimension tags used to qualify the metric key.
        """
        key = self._make_key(metric_name, tags)
        payload: dict[str, Any] | None
        version: int
        with self._lock:
            if key not in self._histograms:
                self._histograms[key] = deque(maxlen=10000)
            self._histograms[key].append(value)
            payload, version = self._snapshot_payload_locked()
        self._persist_snapshot_payload(payload, version)

    def get_counter(self, metric_name: str, **tags: object) -> int:
        """Return the current counter value for a metric key.

        Args:
            metric_name: The metric name.
            **tags: Dimension tags used to qualify the metric key.

        Returns:
            Current counter value, or 0 if never incremented.
        """
        key = self._make_key(metric_name, tags)
        with self._lock:
            return self._counters.get(key, 0)

    def get_histogram_stats(
        self,
        metric_name: str,
        **tags: object,
    ) -> dict[str, float] | None:
        """Return summary statistics for a histogram metric.

        Args:
            metric_name: The metric name.
            **tags: Dimension tags used to qualify the metric key.

        Returns:
            Dictionary with keys ``count``, ``sum``, ``min``, ``max``,
            ``avg``, ``p50``, ``p95``, ``p99``, or ``None`` if no
            observations have been recorded.
        """
        key = self._make_key(metric_name, tags)
        with self._lock:
            values = self._histograms.get(key, [])
            if not values:
                return None

            sorted_values = sorted(values)
            return {
                "count": len(values),
                "sum": sum(values),
                "min": min(values),
                "max": max(values),
                "avg": sum(values) / len(values),
                "p50": sorted_values[len(sorted_values) // 2],
                "p95": sorted_values[int(len(sorted_values) * 0.95)],
                "p99": sorted_values[int(len(sorted_values) * 0.99)],
            }

    def _make_key(self, metric_name: str, tags: dict[str, object]) -> str:
        """Build a unique metric key from name and tags.

        Args:
            metric_name: Base metric name.
            tags: Tag dictionary to encode into the key.

        Returns:
            A deterministic string key such as
            ``"vetinari.task.count{status=completed}"``.
        """
        safe_metric = sanitize_untrusted_text(metric_name, max_length=200)
        if not tags:
            return safe_metric
        safe_tags = {sanitize_untrusted_text(str(k), max_length=80): _stable_metric_tag(v) for k, v in tags.items()}
        tag_str = ",".join(f"{k}={v}" for k, v in sorted(safe_tags.items()))
        return f"{safe_metric}{{{tag_str}}}"

    def _restore(self) -> None:
        """Restore counters and histograms from the configured snapshot."""
        if self._persistence_path is None or not self._persistence_path.exists():
            return
        try:
            data = json.loads(self._persistence_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"metrics snapshot unreadable: {self._persistence_path}") from exc
        if data.get("schema_version") != 1:
            raise ValueError(f"metrics snapshot schema unsupported: {self._persistence_path}")
        unknown_keys = set(data) - _SNAPSHOT_KEYS
        if unknown_keys:
            keys = ", ".join(sorted(str(key) for key in unknown_keys))
            raise ValueError(f"metrics snapshot has unknown fields: {keys}")
        counters = data.get("counters", {})
        histograms = data.get("histograms", {})
        if not isinstance(counters, dict) or not isinstance(histograms, dict):
            raise ValueError(f"metrics snapshot malformed: {self._persistence_path}")
        self._counters = {str(key): int(value) for key, value in counters.items()}
        self._histograms = {
            str(key): deque((float(item) for item in values), maxlen=10000)
            for key, values in histograms.items()
            if isinstance(values, list)
        }

    def _snapshot_payload_locked(self) -> tuple[dict[str, Any] | None, int]:
        """Return a persistence payload while the caller holds ``_lock``."""
        self._snapshot_version += 1
        version = self._snapshot_version
        if self._persistence_path is None:
            return None, version
        return {
            "schema_version": 1,
            "counters": dict(self._counters),
            "histograms": {key: list(values) for key, values in self._histograms.items()},
        }, version

    def _persist_snapshot_payload(self, payload: dict[str, Any] | None, version: int) -> None:
        """Persist a metrics snapshot without holding the hot-path mutation lock."""
        if self._persistence_path is None or payload is None:
            return
        self._persistence_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self._persistence_path.with_suffix(
            f"{self._persistence_path.suffix}.{version}.{threading.get_ident()}.tmp",
        )
        tmp_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        with self._lock:
            if version == self._snapshot_version:
                tmp_path.replace(self._persistence_path)
                return
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            logger.warning("Could not remove stale metrics snapshot temp file: %s", tmp_path, exc_info=True)


# Global metrics collector singleton
_metrics = MetricsCollector()


def get_metrics() -> MetricsCollector:
    """Return the process-wide metrics collector singleton.

    Returns:
        The shared ``MetricsCollector`` instance.
    """
    return _metrics


def record_task_duration(task_id: str, duration_ms: float) -> None:
    """Record task execution duration in the global metrics collector.

    The task identifier is logged for debugging but is intentionally not used
    as a metric tag; task IDs are high-cardinality values that would fragment
    the histogram and make aggregate duration stats unavailable.

    Args:
        task_id: Identifier of the task whose duration is being recorded.
        duration_ms: Elapsed time in milliseconds.
    """
    logger.debug("Recording task duration for %s: %.2f ms", task_id, duration_ms)
    _metrics.record("vetinari.task.duration", duration_ms, task_type="generic")


def increment_task_count(status: str) -> None:
    """Increment the global task counter for a given status.

    Args:
        status: Completion status label (e.g. ``"completed"``, ``"failed"``).
    """
    _metrics.increment("vetinari.task.count", status=status)


def record_model_latency(duration_ms: float) -> None:
    """Record model inference latency in the global metrics collector.

    Args:
        duration_ms: Elapsed inference time in milliseconds.
    """
    _metrics.record("vetinari.model.latency", duration_ms)


def record_model_call_failure(
    *,
    project_id: str,
    agent_type: str,
    model_id: str,
    failure_class: str,
    task_id: str = "",
) -> None:
    """Record a failed model call with route-identifying dimensions.

    Args:
        project_id: Workbench or project identifier associated with the call.
        task_id: Task identifier associated with the failed call.
        agent_type: Agent type that attempted the call.
        model_id: Model or provider route that failed.
        failure_class: Stable failure class such as ``timeout`` or ``rate_limit``.
    """
    tags = {
        "project_id": f"sha256:{_stable_metric_tag(project_id, high_cardinality=True)}",
        "agent_type": _stable_metric_tag(agent_type),
        "model_id": _stable_metric_tag(model_id),
        "failure_class": _stable_metric_tag(failure_class),
    }
    if task_id:
        tags["task_id"] = f"sha256:{_stable_metric_tag(task_id, high_cardinality=True)}"
    _metrics.increment("vetinari.model.call.failure", **tags)


def increment_training_records_skipped(*, reason: str, model: str) -> None:
    """Record a rejected training record with route-stable labels."""
    _metrics.increment(
        "vetinari.training.records.skipped",
        reason=_stable_metric_tag(reason),
        model=_stable_metric_tag(model),
    )


def increment_api_request(status_code: int) -> None:
    """Increment the API request counter for a given HTTP status code.

    Args:
        status_code: HTTP response status code (e.g. 200, 404, 500).
    """
    _metrics.increment("vetinari.api.request", status=status_code)
