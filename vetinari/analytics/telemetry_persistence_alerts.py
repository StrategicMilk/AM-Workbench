"""Extracted implementation helpers for telemetry_persistence.py."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from vetinari.guards import GateError

logger = logging.getLogger(__name__)


class TelemetryAlertMixin:
    """Shared method implementations for the compatibility wrapper."""

    _emit_alert_event: Callable[[str, str, dict[str, Any]], None]
    _error_rate_threshold: float
    _p95_latency_threshold_ms: float

    def _check_alert_thresholds(self, summary: dict[str, Any]) -> None:
        """Emit WARNING logs and events when error-rate or p95 latency breach thresholds.

        Args:
            summary: The enriched telemetry summary dict from the current cycle.
        """
        if not isinstance(summary, dict):
            raise GateError("telemetry_alert_summary", "summary must be a dictionary")
        # -- Error rate check across all adapters --
        adapter_details: dict[str, Any] = summary.get("adapter_details", {})
        if not isinstance(adapter_details, dict):
            raise GateError("telemetry_alert_summary", "adapter_details must be a dictionary")
        total_requests = 0
        total_failed = 0
        for _key, detail in adapter_details.items():
            if isinstance(detail, dict):
                total_requests += int(detail.get("total_requests", 0))
                total_failed += int(detail.get("failed_requests", 0))

        if total_requests == 0 and adapter_details:
            logger.warning(
                "Telemetry alert: zero requests observed across %d adapters - telemetry or inference may be stalled",
                len(adapter_details),
            )
            self._emit_alert_event(
                "zero_traffic_blackout",
                "No requests observed across telemetry adapters",
                {"adapter_count": len(adapter_details)},
            )
        elif total_requests > 0:
            error_rate = (total_failed / total_requests) * 100.0
            if error_rate > self._error_rate_threshold:
                logger.warning(
                    "Telemetry alert: error rate %.1f%% exceeds threshold %.1f%% "
                    "(%d failed / %d total requests) - investigate adapter failures",
                    error_rate,
                    self._error_rate_threshold,
                    total_failed,
                    total_requests,
                )
                self._emit_alert_event(
                    "high_error_rate",
                    f"Error rate {error_rate:.1f}% exceeds threshold {self._error_rate_threshold:.1f}%",
                    {"error_rate": error_rate, "threshold": self._error_rate_threshold},
                )

        # -- p95 latency check using MetricsCollector histogram --
        try:
            from vetinari.metrics import get_metrics

            stats = get_metrics().get_histogram_stats("vetinari.model.latency")
            if stats is not None:
                p95 = stats.get("p95", 0.0)
                if p95 > self._p95_latency_threshold_ms:
                    logger.warning(
                        "Telemetry alert: p95 model latency %.1fms exceeds threshold %.1fms "
                        "- model may be overloaded or undersized",
                        p95,
                        self._p95_latency_threshold_ms,
                    )
                    self._emit_alert_event(
                        "high_p95_latency",
                        f"p95 latency {p95:.1f}ms exceeds threshold {self._p95_latency_threshold_ms:.1f}ms",
                        {"p95_latency_ms": p95, "threshold_ms": self._p95_latency_threshold_ms},
                    )
        except Exception as exc:
            raise GateError("telemetry_p95_alert", "p95 latency check failed", exc) from exc
