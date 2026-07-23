"""Vetinari Dashboard Package.

Provides real-time metrics visualization, alert management, and distributed
trace exploration via the Litestar web server.

Key Modules:
    - api: Main dashboard API for metrics, traces, and time-series data
    - alerts: Alert configuration and evaluation engine
    - log_aggregator: Integration with centralized logging platforms

Quick Start (Backend API):
    from vetinari.dashboard import get_dashboard_api

    api = get_dashboard_api()

    # Get latest metrics
    metrics = api.get_latest_metrics()
    logger.debug(metrics.to_dict())

    # Get time-series data
    latency_data = api.get_timeseries_data('latency', timerange='24h')
    logger.debug(latency_data.to_dict())

    # Search traces
    traces = api.search_traces(limit=50)
    for trace in traces:
        logger.debug(trace.to_dict())
"""

from __future__ import annotations

import logging

from .agent_dashboard import AgentDashboard, AgentMetrics, SystemHealth
from .alerts import (
    AlertCondition,
    AlertEngine,
    AlertRecord,
    AlertSeverity,
    AlertThreshold,
    get_alert_engine,
    reset_alert_engine,
)
from .api import (
    DashboardAPI,
    MetricsSnapshot,
    TimeSeriesData,
    TraceDetail,
    get_dashboard_api,
    reset_dashboard,
)
from .log_aggregator import (
    AggregatorHandler,
    LogAggregator,
    LogRecord,
    get_log_aggregator,
    reset_log_aggregator,
)
from .log_backends import (
    DatadogBackend,
    FileBackend,
    SSEBackend,
    WebhookBackend,
    get_sse_backend,
    reset_sse_backend,
)

logger = logging.getLogger(__name__)


__all__ = [
    "AgentDashboard",
    "AgentMetrics",
    "AggregatorHandler",
    "AlertCondition",
    "AlertEngine",
    "AlertRecord",
    "AlertSeverity",
    "AlertThreshold",
    "DashboardAPI",
    "DatadogBackend",
    "FileBackend",
    "LogAggregator",
    "LogRecord",
    "MetricsSnapshot",
    "SSEBackend",
    "SystemHealth",
    "TimeSeriesData",
    "TraceDetail",
    "WebhookBackend",
    "get_alert_engine",
    "get_dashboard_api",
    "get_log_aggregator",
    "get_sse_backend",
    "reset_alert_engine",
    "reset_dashboard",
    "reset_log_aggregator",
    "reset_sse_backend",
]

__version__ = "1.0.0"
