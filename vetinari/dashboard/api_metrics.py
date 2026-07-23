"""Extracted implementation helpers for api.py."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from vetinari.dashboard.api import MetricsSnapshot

logger = logging.getLogger(__name__)


class DashboardMetricsMixin:
    """Shared method implementations for the compatibility wrapper."""

    if TYPE_CHECKING:
        _calc_avg_latency: Any
        _lock: Any
        _start_time: Any
        telemetry: Any

    def get_latest_metrics(self) -> MetricsSnapshot:
        """Get current snapshot of all metrics.

        Returns:
            MetricsSnapshot with latest values
        """
        with self._lock:
            uptime = (datetime.now(timezone.utc) - self._start_time).total_seconds() * 1000

            # Collect adapter metrics
            adapter_metrics = self.telemetry.get_adapter_metrics()
            adapter_summary = {
                "total_providers": len({m.provider for m in adapter_metrics.values()}),
                "total_requests": sum(m.total_requests for m in adapter_metrics.values()),
                "total_successful": sum(m.successful_requests for m in adapter_metrics.values()),
                "total_failed": sum(m.failed_requests for m in adapter_metrics.values()),
                "average_latency_ms": self._calc_avg_latency(adapter_metrics),
                "timeout_rate_percent": 0.0,
                "total_tokens_used": sum(m.total_tokens_used for m in adapter_metrics.values()),
                "providers": {
                    k: {
                        "provider": v.provider,
                        "model": v.model,
                        "requests": v.total_requests,
                        "success_rate": v.success_rate,
                        "avg_latency_ms": v.avg_latency_ms,
                        "min_latency_ms": v.min_latency_ms if v.min_latency_ms != float("inf") else 0,
                        "max_latency_ms": v.max_latency_ms,
                        "last_request": v.last_request_time,
                    }
                    for k, v in adapter_metrics.items()
                },
            }

            # Collect memory metrics
            memory_metrics = self.telemetry.get_memory_metrics()
            memory_summary = {
                "backends": {
                    k: {
                        "backend": v.backend,
                        "writes": v.total_writes,
                        "reads": v.total_reads,
                        "searches": v.total_searches,
                        "avg_write_latency_ms": v.avg_write_latency(),
                        "avg_read_latency_ms": v.avg_read_latency(),
                        "avg_search_latency_ms": v.avg_search_latency(),
                        "dedup_hit_rate": v.dedup_hit_rate,
                        "sync_failures": v.sync_failures,
                    }
                    for k, v in memory_metrics.items()
                },
            }

            # Collect plan metrics
            plan_metrics = self.telemetry.get_plan_metrics()
            plan_summary = {
                "total_decisions": plan_metrics.total_decisions,
                "approved": plan_metrics.approved_decisions,
                "rejected": plan_metrics.rejected_decisions,
                "auto_approved": plan_metrics.auto_approved_decisions,
                "approval_rate": plan_metrics.approval_rate,
                "average_risk_score": plan_metrics.average_risk_score,
                "average_approval_time_ms": plan_metrics.average_approval_time_ms,
            }
            rag_summary = self._get_rag_summary()

            from vetinari.dashboard.api import MetricsSnapshot

            return MetricsSnapshot(
                timestamp=datetime.now(timezone.utc).isoformat(),
                uptime_ms=uptime,
                adapter_summary=adapter_summary,
                memory_summary=memory_summary,
                plan_summary=plan_summary,
                rag_summary=rag_summary,
            )

    @staticmethod
    def _get_rag_summary() -> dict[str, object]:
        try:
            from vetinari.rag.knowledge_base import get_knowledge_base

            stats = get_knowledge_base().get_stats()
            rate = float(stats.get("embedding_fallback_rate", 0.0))
            percent = round(rate * 100.0, 3)
            return {
                "status": "ok",
                "embedding_fallback_rate": rate,
                "embedding_fallback_rate_percent": percent,
                "rag_embedding_fallback_rate": rate,
                "rag_embedding_fallback_rate_percent": percent,
                "embedding_attempts": int(stats.get("embedding_attempts", 0)),
                "embedding_fallbacks": int(stats.get("embedding_fallbacks", 0)),
            }
        except Exception as exc:
            logger.warning("RAG embedding fallback metrics unavailable", exc_info=True)
            return {"status": "unavailable", "error": str(exc)}
