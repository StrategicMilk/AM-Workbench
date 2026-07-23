"""Dashboard Backend API for Vetinari.

Provides REST endpoints for:
- Real-time metrics visualization
- Historical data and time-series
- Alert configuration and status
- Trace searching and exploration

The dashboard integrates telemetry data from Phase 3 and exposes it through
a RESTful API for frontend consumption.

Usage:
    from vetinari.dashboard.api import DashboardAPI

    api = DashboardAPI()
    # Returns latest metrics snapshot
    latest = api.get_latest_metrics()

    # Returns time-series data for charting
    timeseries = api.get_timeseries_data('latency', timerange='24h')
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from vetinari.dashboard.api_metrics import DashboardMetricsMixin
from vetinari.telemetry import get_telemetry_collector
from vetinari.utils.serialization import dataclass_to_dict

logger = logging.getLogger(__name__)


@dataclass
class MetricsSnapshot:
    """Current snapshot of all system metrics."""

    timestamp: str
    uptime_ms: float

    # Adapter metrics summary
    adapter_summary: dict[str, Any]

    # Memory metrics summary
    memory_summary: dict[str, Any]

    # Plan mode metrics summary
    plan_summary: dict[str, Any]
    rag_summary: dict[str, Any] | None = None

    def __repr__(self) -> str:
        return f"MetricsSnapshot(timestamp={self.timestamp!r}, uptime_ms={self.uptime_ms!r})"

    def to_dict(self) -> dict[str, Any]:
        """Produce a JSON-ready snapshot using short API keys for the frontend contract.

        Returns:
            Dictionary with keys ``timestamp``, ``uptime_ms``, ``adapters``,
            ``memory``, and ``plan`` — note that ``adapters``, ``memory``, and
            ``plan`` are intentionally shorter than the dataclass field names to
            match the established REST API contract.
        """
        return {
            "timestamp": self.timestamp,
            "uptime_ms": self.uptime_ms,
            "adapters": self.adapter_summary,
            "memory": self.memory_summary,
            "plan": self.plan_summary,
            "operations": {"queue_depth": 0},
            "workbench": {"spine_corruption_count": 0},
            "rag": self.rag_summary
            or {
                "status": "unavailable",
                "rag_embedding_fallback_rate": 0.0,
                "embedding_fallback_rate_percent": 0.0,
            },
            "security": {"authz_rejection_rate": 0.0, "authz_rejection_rate_percent": 0.0},
        }


@dataclass
class TimeSeriesPoint:
    """A single point in a time-series."""

    timestamp: str
    value: float
    metadata: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize this point, normalizing a missing metadata value to an empty dict.

        Returns:
            Dictionary with ``timestamp``, ``value``, and ``metadata`` keys.
            ``metadata`` is always a dict (never ``None``) for safe frontend consumption.
        """
        return {"timestamp": self.timestamp, "value": self.value, "metadata": self.metadata or {}}


@dataclass
class TimeSeriesData:
    """Time-series data for a metric."""

    metric: str
    unit: str
    points: list[TimeSeriesPoint]
    min_value: float | None = 0.0
    max_value: float | None = 0.0
    avg_value: float | None = 0.0

    def __repr__(self) -> str:
        return f"TimeSeriesData(metric={self.metric!r}, unit={self.unit!r}, points={len(self.points)})"

    def to_dict(self) -> dict[str, Any]:
        """Serialize this dataset using short API keys and recursively serialize each point.

        Returns:
            Dictionary with keys ``metric``, ``unit``, ``min``, ``max``, ``avg``,
            and ``points`` — note that ``min``, ``max``, and ``avg`` are shorter
            than the field names to match the established REST API contract.
        """
        return {
            "metric": self.metric,
            "unit": self.unit,
            "min": self.min_value,
            "max": self.max_value,
            "avg": self.avg_value,
            "points": [p.to_dict() for p in self.points],
        }


@dataclass
class TraceInfo:
    """Information about a distributed trace."""

    trace_id: str
    start_time: str
    duration_ms: float
    span_count: int
    status: str  # success, error, or StatusEnum.IN_PROGRESS.value
    root_operation: str

    def __repr__(self) -> str:
        return f"TraceInfo(trace_id={self.trace_id!r}, status={self.status!r}, root_operation={self.root_operation!r})"

    def to_dict(self) -> dict[str, Any]:
        """Serialize trace summary fields into a dictionary for JSON delivery."""
        return dataclass_to_dict(self)


@dataclass
class TraceDetail:
    """Detailed trace information with spans."""

    trace_id: str
    start_time: str
    end_time: str
    duration_ms: float
    status: str
    spans: list[dict[str, Any]]

    def __repr__(self) -> str:
        return f"TraceDetail(trace_id={self.trace_id!r}, status={self.status!r}, spans={len(self.spans)})"

    def to_dict(self) -> dict[str, Any]:
        """Serialize detailed trace fields and spans into a dictionary for JSON delivery."""
        return dataclass_to_dict(self)


class DashboardAPI(DashboardMetricsMixin):
    """Backend API for dashboard metrics and monitoring.

    Provides REST-style methods for accessing telemetry data,
    alert configuration, and trace information.
    """

    def __init__(self):
        self.telemetry = get_telemetry_collector()
        self._lock = threading.RLock()
        self._start_time = datetime.now(timezone.utc)

        # In-memory trace registry — compat-only.
        # _traces: trace_id -> TraceDetail, used by get_trace_detail() for O(1)
        #   lookup by ID.  Written by add_trace(); read by get_trace_detail() and
        #   evicted by add_trace() when _trace_list reaches capacity.
        # _trace_list: bounded deque of TraceInfo summaries (maxlen=1000).
        #   Written by add_trace() for new traces only; read by search_traces()
        #   for listing and ordering.  Both structures are protected by _lock.
        # NOTE: These two attributes are an in-process store only.  They are NOT
        # persisted to disk and are NOT shared across processes.  They exist
        # solely for the dashboard to display recent traces without an external
        # database dependency.
        self._traces: dict[str, TraceDetail] = {}
        self._trace_list: deque[TraceInfo] = deque(maxlen=1000)

        logger.info("DashboardAPI initialized")

    # === Metrics Endpoints ===

    def get_timeseries_data(
        self,
        metric: str,
        timerange: str = "24h",
        provider: str | None = None,
    ) -> TimeSeriesData | None:
        """Get time-series data for a metric.

        Args:
            metric: Metric name ('latency', 'success_rate', 'token_usage', 'memory_latency')
            timerange: Time range ('1h', '24h', '7d')
            provider: Optional provider filter for adapter metrics

        Returns:
            TimeSeriesData with historical points, or None if not available
        """
        with self._lock:
            if metric == "latency":
                return self._get_adapter_latency_timeseries(timerange, provider)
            if metric == "success_rate":
                return self._get_success_rate_timeseries(timerange, provider)
            if metric == "token_usage":
                return self._get_token_usage_timeseries(timerange, provider)
            if metric == "memory_latency":
                return self._get_memory_latency_timeseries(timerange)
            logger.warning("Unknown metric requested: %s", metric)
            return None

    def _get_adapter_latency_timeseries(self, timerange: str, provider: str | None = None) -> TimeSeriesData:
        """Get adapter latency time-series."""
        metrics = self.telemetry.get_adapter_metrics(provider)

        points = []
        latencies = []

        for _key, metric in metrics.items():
            if metric.total_requests > 0:
                points.append(
                    TimeSeriesPoint(
                        timestamp=metric.last_request_time or datetime.now(timezone.utc).isoformat(),
                        value=metric.avg_latency_ms,
                        metadata={"provider": metric.provider, "model": metric.model},
                    ),
                )
                latencies.append(metric.avg_latency_ms)

        # Sort by timestamp
        points.sort(key=lambda p: p.timestamp)

        return TimeSeriesData(
            metric="latency",
            unit="ms",
            points=points,
            min_value=min(latencies) if latencies else 0.0,
            max_value=max(latencies) if latencies else 0.0,
            avg_value=sum(latencies) / len(latencies) if latencies else 0.0,
        )

    def _get_success_rate_timeseries(self, timerange: str, provider: str | None = None) -> TimeSeriesData:
        """Get success rate time-series."""
        metrics = self.telemetry.get_adapter_metrics(provider)

        points = []
        rates = []

        for _key, metric in metrics.items():
            if metric.total_requests > 0:
                points.append(
                    TimeSeriesPoint(
                        timestamp=metric.last_request_time or datetime.now(timezone.utc).isoformat(),
                        value=metric.success_rate,
                        metadata={"provider": metric.provider, "model": metric.model},
                    ),
                )
                rates.append(metric.success_rate)

        points.sort(key=lambda p: p.timestamp)

        return TimeSeriesData(
            metric="success_rate",
            unit="%",
            points=points,
            min_value=min(rates) if rates else None,
            max_value=max(rates) if rates else None,
            avg_value=sum(rates) / len(rates) if rates else None,
        )

    def _get_token_usage_timeseries(self, timerange: str, provider: str | None = None) -> TimeSeriesData:
        """Get token usage time-series."""
        metrics = self.telemetry.get_adapter_metrics(provider)

        points = []
        usages = []

        for _key, metric in metrics.items():
            if metric.total_requests > 0:
                points.append(
                    TimeSeriesPoint(
                        timestamp=metric.last_request_time or datetime.now(timezone.utc).isoformat(),
                        value=metric.total_tokens_used,
                        metadata={"provider": metric.provider, "model": metric.model},
                    ),
                )
                usages.append(metric.total_tokens_used)

        points.sort(key=lambda p: p.timestamp)

        return TimeSeriesData(
            metric="token_usage",
            unit="tokens",
            points=points,
            min_value=min(usages) if usages else 0.0,
            max_value=max(usages) if usages else 0.0,
            avg_value=sum(usages) / len(usages) if usages else 0.0,
        )

    def _get_memory_latency_timeseries(self, timerange: str) -> TimeSeriesData:
        """Return current-average memory latency as a single-point time series.

        MemoryMetrics stores running aggregates, not per-operation timestamps,
        so true historical time-series reconstruction is not possible here.
        Each point is stamped with the current UTC time and annotated with
        ``"snapshot": true`` in its metadata so callers know these are
        instantaneous averages rather than historical data points.
        """
        metrics = self.telemetry.get_memory_metrics()
        # Capture a single timestamp for all points in this snapshot so they
        # are comparable and the caller can distinguish a snapshot from genuine
        # historical points (which would have distinct, earlier timestamps).
        snapshot_ts = datetime.now(timezone.utc).isoformat()

        points = []
        all_latencies = []

        for _key, metric in metrics.items():
            read_latency = metric.avg_read_latency()
            write_latency = metric.avg_write_latency()
            search_latency = metric.avg_search_latency()

            if read_latency > 0:
                points.append(
                    TimeSeriesPoint(
                        timestamp=snapshot_ts,
                        value=read_latency,
                        metadata={"backend": metric.backend, "operation": "read", "snapshot": True},
                    ),
                )
                all_latencies.append(read_latency)

            if write_latency > 0:
                points.append(
                    TimeSeriesPoint(
                        timestamp=snapshot_ts,
                        value=write_latency,
                        metadata={"backend": metric.backend, "operation": "write", "snapshot": True},
                    ),
                )
                all_latencies.append(write_latency)

            if search_latency > 0:
                points.append(
                    TimeSeriesPoint(
                        timestamp=snapshot_ts,
                        value=search_latency,
                        metadata={"backend": metric.backend, "operation": "search", "snapshot": True},
                    ),
                )
                all_latencies.append(search_latency)

        return TimeSeriesData(
            metric="memory_latency",
            unit="ms",
            points=points,
            min_value=min(all_latencies) if all_latencies else 0.0,
            max_value=max(all_latencies) if all_latencies else 0.0,
            avg_value=sum(all_latencies) / len(all_latencies) if all_latencies else 0.0,
        )

    # === Trace Endpoints ===

    def search_traces(self, trace_id: str | None = None, limit: int = 100) -> list[TraceInfo]:
        """Search for traces by ID or list recent traces.

        Args:
            trace_id: Optional trace ID to search for
            limit: Maximum number of traces to return

        Returns:
            List of TraceInfo objects
        """
        with self._lock:
            if trace_id:
                # Search for specific trace
                results = [t for t in self._trace_list if t.trace_id == trace_id]
            else:
                # Return recent traces
                results = sorted(self._trace_list, key=lambda t: t.start_time, reverse=True)[:limit]

            return results

    def get_trace_detail(self, trace_id: str) -> TraceDetail | None:
        """Get detailed information about a trace.

        Args:
            trace_id: Trace ID to retrieve

        Returns:
            TraceDetail object, or None if not found
        """
        with self._lock:
            return self._traces.get(trace_id)

    def add_trace(self, trace_detail: TraceDetail) -> bool:
        """Add a trace to the dashboard (called by logging integration).

        Args:
            trace_detail: Trace information to store

        Returns:
            True if successful
        """
        with self._lock:
            try:
                trace_id = trace_detail.trace_id
                is_new = trace_id not in self._traces
                self._traces[trace_id] = trace_detail

                # Only append to _trace_list when this is a genuinely new trace.
                # Re-adding an existing trace_id updates the detail dict but must
                # not grow the deque — otherwise the list overflows and evicts
                # unrelated traces while the duplicate accumulates without bound.
                if is_new:
                    trace_info = TraceInfo(
                        trace_id=trace_id,
                        start_time=trace_detail.start_time,
                        duration_ms=trace_detail.duration_ms,
                        span_count=len(trace_detail.spans),
                        status=trace_detail.status,
                        root_operation=trace_detail.spans[0].get("operation", "unknown")
                        if trace_detail.spans
                        else "unknown",
                    )
                    # Evict the oldest trace detail when the deque is at capacity
                    if len(self._trace_list) == self._trace_list.maxlen:
                        oldest = self._trace_list[0]
                        self._traces.pop(oldest.trace_id, None)
                    self._trace_list.append(trace_info)

                logger.debug("Trace %s: %s", "added" if is_new else "updated", trace_id)
                return True
            except Exception as e:
                logger.error("Failed to add trace %s: %s", trace_detail.trace_id, e)
                return False

    # === Helper Methods ===

    @staticmethod
    def _calc_avg_latency(metrics: dict[str, Any]) -> float:
        """Calculate request-weighted average latency across all providers.

        A simple mean of per-provider averages would give equal weight to a
        provider with 1 request and one with 10 000 requests, skewing the
        reported average.  Instead we weight each provider's latency by its
        successful request count so the global average reflects actual load.

        Returns:
            Weighted mean latency in milliseconds, or 0.0 if no successful
            requests have been recorded.
        """
        if not metrics:
            return 0.0

        total_latency = sum(m.total_latency_ms for m in metrics.values())
        total_successful = sum(m.successful_requests for m in metrics.values())
        if total_successful == 0:
            return 0.0
        return total_latency / total_successful

    def clear_traces(self) -> None:
        """Clear all stored traces (useful for testing or memory management)."""
        with self._lock:
            self._traces.clear()
            self._trace_list.clear()
            logger.info("Traces cleared")

    def get_stats(self) -> dict[str, Any]:
        """Get dashboard statistics.

        Returns:
            Dictionary with ``total_traces_stored`` (full-detail traces kept in
            memory), ``trace_list_size`` (lightweight TraceInfo index entries),
            and ``timestamp`` (ISO-8601 UTC time of this snapshot).
        """
        with self._lock:
            return {
                "total_traces_stored": len(self._traces),
                "trace_list_size": len(self._trace_list),
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }


# Global singleton instance
_dashboard_api: DashboardAPI | None = None
_dashboard_lock = threading.Lock()


def get_dashboard_api() -> DashboardAPI:
    """Get or create the global dashboard API instance.

    Returns:
        The DashboardAPI result.
    """
    global _dashboard_api
    if _dashboard_api is None:
        with _dashboard_lock:
            if _dashboard_api is None:
                _dashboard_api = DashboardAPI()
    return _dashboard_api


def reset_dashboard() -> None:
    """Reset dashboard API (mainly for testing)."""
    global _dashboard_api
    _dashboard_api = None
