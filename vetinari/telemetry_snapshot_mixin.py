"""Snapshot and export helpers for :mod:`vetinari.telemetry`."""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

logger = logging.getLogger(__name__)


def _escape_label_value(v: str) -> str:
    """Escape a Prometheus label value per the text exposition format.

    Applies the three required escapes from the Prometheus text format spec:
    backslash → double-backslash, double-quote → backslash-quote,
    newline → backslash-n.

    Args:
        v: Raw label value string that may contain special characters.

    Returns:
        Escaped string safe for use inside Prometheus label value quotes.
    """
    return v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


class TelemetrySnapshotMixin:
    """Export and restore behavior shared by the telemetry collector facade."""

    if TYPE_CHECKING:
        from vetinari.telemetry import AdapterMetrics, MemoryMetrics, PlanMetrics

        adapter_metrics: dict[str, AdapterMetrics]
        memory_metrics: dict[str, MemoryMetrics]
        plan_metrics: PlanMetrics
        _lock: threading.RLock
        _start_time: datetime

    def export_json(self, path: str) -> bool:
        """Export all metrics to JSON file.

        Returns:
            True if successful, False otherwise.
        """
        try:
            with self._lock:
                uptime_ms = (datetime.now(timezone.utc) - self._start_time).total_seconds() * 1000

                export_data = {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "uptime_ms": uptime_ms,
                    "adapters": {
                        k: {
                            "provider": v.provider,
                            "model": v.model,
                            "total_requests": v.total_requests,
                            "successful_requests": v.successful_requests,
                            "failed_requests": v.failed_requests,
                            "success_rate": v.success_rate,
                            "avg_latency_ms": v.avg_latency_ms,
                            "min_latency_ms": v.min_latency_ms,
                            "max_latency_ms": v.max_latency_ms,
                            "total_tokens_used": v.total_tokens_used,
                            "last_request_time": v.last_request_time,
                        }
                        for k, v in self.adapter_metrics.items()
                    },
                    "memory": {
                        k: {
                            "backend": v.backend,
                            "total_writes": v.total_writes,
                            "total_reads": v.total_reads,
                            "total_searches": v.total_searches,
                            "avg_write_latency_ms": v.avg_write_latency(),
                            "avg_read_latency_ms": v.avg_read_latency(),
                            "avg_search_latency_ms": v.avg_search_latency(),
                            "dedup_hit_rate": v.dedup_hit_rate,
                            "sync_failures": v.sync_failures,
                        }
                        for k, v in self.memory_metrics.items()
                    },
                    "plan_mode": {
                        "total_decisions": self.plan_metrics.total_decisions,
                        "approved_decisions": self.plan_metrics.approved_decisions,
                        "rejected_decisions": self.plan_metrics.rejected_decisions,
                        "auto_approved_decisions": self.plan_metrics.auto_approved_decisions,
                        "approval_rate": self.plan_metrics.approval_rate,
                        "average_risk_score": self.plan_metrics.average_risk_score,
                        "average_approval_time_ms": self.plan_metrics.average_approval_time_ms,
                    },
                }

                Path(path).parent.mkdir(parents=True, exist_ok=True)
                with Path(path).open("w", encoding="utf-8") as f:
                    json.dump(export_data, f, indent=2)

                logger.info("Telemetry exported to %s", path)
                return True
        except Exception as e:
            logger.error("Failed to export telemetry: %s", e)
            return False

    def persist_snapshot(self, path: str | Path) -> bool:
        """Persist a telemetry snapshot that a later process can read back."""
        return self.export_json(str(path))

    @staticmethod
    def read_snapshot(path: str | Path) -> dict[str, Any] | None:
        """Read a previously persisted telemetry snapshot from disk."""
        snapshot_path = Path(path)
        try:
            with snapshot_path.open(encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError as exc:
            logger.debug("telemetry snapshot not found: %s (%s)", snapshot_path, exc)
            return None
        except json.JSONDecodeError as exc:
            logger.warning("telemetry snapshot is not valid JSON: %s (%s)", snapshot_path, exc)
            return None
        if not isinstance(data, dict):
            logger.warning("telemetry snapshot must be a JSON object: %s", snapshot_path)
            return None
        return data

    def export_prometheus(self, path: str) -> bool:
        """Export metrics in Prometheus text format.

        Returns:
            True if successful, False otherwise.
        """
        try:
            with self._lock:
                lines = []
                lines.extend((
                    "# HELP vetinari_adapter_requests_total Total adapter requests",
                    "# TYPE vetinari_adapter_requests_total counter",
                ))

                for _key, metrics in self.adapter_metrics.items():
                    labels = f'provider="{_escape_label_value(metrics.provider)}",model="{_escape_label_value(metrics.model)}"'
                    lines.append(f"vetinari_adapter_requests_total{{{labels}}} {metrics.total_requests}")

                lines.extend((
                    "# HELP vetinari_adapter_latency_ms Adapter latency in milliseconds",
                    "# TYPE vetinari_adapter_latency_ms gauge",
                ))

                for _key, metrics in self.adapter_metrics.items():
                    labels = f'provider="{_escape_label_value(metrics.provider)}",model="{_escape_label_value(metrics.model)}"'
                    lines.append(f"vetinari_adapter_latency_ms{{{labels}}} {metrics.avg_latency_ms}")

                lines.extend((
                    "# HELP vetinari_memory_operations_total Total memory operations",
                    "# TYPE vetinari_memory_operations_total counter",
                ))

                for _key, metrics in self.memory_metrics.items():
                    total_ops = metrics.total_writes + metrics.total_reads + metrics.total_searches
                    lines.append(
                        f'vetinari_memory_operations_total{{backend="{_escape_label_value(metrics.backend)}"}} {total_ops}'
                    )

                lines.extend((
                    "# HELP vetinari_plan_decisions_total Total plan decisions",
                    "# TYPE vetinari_plan_decisions_total counter",
                    f'vetinari_plan_decisions_total{{decision="approve"}} {self.plan_metrics.approved_decisions}',
                    f'vetinari_plan_decisions_total{{decision="reject"}} {self.plan_metrics.rejected_decisions}',
                ))

                Path(path).parent.mkdir(parents=True, exist_ok=True)
                Path(path).write_text("\n".join(lines), encoding="utf-8")

                logger.info("Prometheus metrics exported to %s", path)
                return True
        except Exception as e:
            logger.error("Failed to export Prometheus metrics: %s", e)
            return False

    def restore_from_snapshot(self) -> None:
        """Seed in-memory counters from the most recent SQLite telemetry snapshot.

        Reads the newest row from the ``telemetry_snapshots`` table, parses
        the JSON ``data`` column, and adds the stored adapter request/latency
        counts as a baseline so that ``get_summary()`` continues from the last
        known totals after a process restart.

        Must be called after ``__init__``, not inside it, to keep construction
        lightweight and side-effect-free.

        Gracefully degrades on any error (missing table, empty DB, malformed
        JSON) by logging at INFO level and leaving counters at zero.
        """
        # Read the N most-recent snapshots so we can fall back to an older one
        # when the newest row is malformed or contains no adapter data.
        snapshot_fallback_limit = 5
        try:
            from vetinari.database import get_connection

            conn = get_connection()
            rows = conn.execute(
                "SELECT data FROM telemetry_snapshots ORDER BY timestamp DESC LIMIT ?",
                (snapshot_fallback_limit,),
            ).fetchall()
        except Exception as exc:
            logger.info("TelemetryCollector: no snapshot available for restore (DB not ready): %s", exc)
            return

        if not rows:
            logger.info("TelemetryCollector: no prior snapshot found - starting from zero")
            return

        for i, row in enumerate(rows):
            try:
                snapshot: dict[str, Any] = json.loads(row[0])
            except Exception as exc:
                logger.warning(
                    "TelemetryCollector: snapshot row %d JSON parse failed - trying older row: %s",
                    i,
                    exc,
                )
                continue

            # Skip snapshots with no useful adapter data; try the next older one.
            if not snapshot.get("adapter_details"):
                logger.info(
                    "TelemetryCollector: snapshot row %d has no adapter data - trying older row",
                    i,
                )
                continue

            try:
                self._apply_snapshot(snapshot)
                return
            except Exception as exc:
                logger.warning(
                    "TelemetryCollector: snapshot row %d apply failed - trying older row: %s",
                    i,
                    exc,
                )

        logger.info(
            "TelemetryCollector: no usable snapshot found in last %d rows - starting from zero",
            snapshot_fallback_limit,
        )

    def _apply_snapshot(self, snapshot: dict[str, Any]) -> None:
        """Apply parsed snapshot data to in-memory counters."""
        from vetinari.telemetry import AdapterMetrics

        adapter_details: dict[str, Any] = snapshot.get("adapter_details", {})

        with self._lock:
            for key, detail in adapter_details.items():
                if not isinstance(detail, dict):
                    continue
                parts = key.split(":", 1)
                if len(parts) != 2:
                    continue
                provider, model = parts
                total_req = int(detail.get("total_requests", 0))
                failed_req = int(detail.get("failed_requests", 0))
                successful_req = max(0, total_req - failed_req)
                avg_latency = float(detail.get("avg_latency_ms", 0.0))
                total_latency = avg_latency * successful_req
                min_lat = float(detail.get("min_latency_ms", float("inf")))
                max_lat = float(detail.get("max_latency_ms", 0.0))
                tokens = int(detail.get("total_tokens_used", 0))

                m = AdapterMetrics(provider=provider, model=model)
                m.total_requests = total_req
                m.successful_requests = successful_req
                m.failed_requests = failed_req
                m.total_latency_ms = total_latency
                m.min_latency_ms = min_lat if min_lat != 0.0 else float("inf")
                m.max_latency_ms = max_lat
                m.total_tokens_used = tokens
                self.adapter_metrics[key] = m

            restored = len(adapter_details)
            if restored:
                logger.info(
                    "TelemetryCollector: restored baseline from snapshot (%d adapter(s))",
                    restored,
                )
            else:
                logger.info("TelemetryCollector: snapshot contained no adapter data - starting from zero")
