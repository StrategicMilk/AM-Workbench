"""Daily Digest — automated summary of Vetinari's activity.

Generates a card-based daily briefing covering tasks completed/failed,
learning improvements, cost summary, pending approvals, and system health.
Delivered to all configured channels via NotificationManager.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from vetinari.types import NotificationPriority

logger = logging.getLogger(__name__)


@dataclass
class DigestSection:
    """A single section of the daily digest.

    Args:
        title: Section heading.
        items: List of human-readable summary strings.
        metrics: Optional key-value metrics for this section.
    """

    title: str
    items: list[str] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)


@dataclass
class DailyDigest:
    """Complete daily digest with structured sections.

    Args:
        generated_at: Timestamp when the digest was generated.
        sections: Ordered list of digest sections.
        overall_health: System health status (``"healthy"``, ``"warning"``, ``"degraded"``).
    """

    generated_at: str
    sections: list[DigestSection] = field(default_factory=list)
    overall_health: str = "healthy"

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dictionary for notification payloads."""
        return {
            "generated_at": self.generated_at,
            "overall_health": self.overall_health,
            "sections": [
                {
                    "title": s.title,
                    "items": s.items,
                    "metrics": s.metrics,
                }
                for s in self.sections
            ],
        }

    def to_text(self) -> str:
        """Render as human-readable plain text for notification body.

        Returns:
            Multi-line string with a header, health status, and one section per digest entry.
        """
        lines = [
            f"Vetinari Daily Briefing — {self.generated_at[:10]}",
            f"System Health: {self.overall_health.upper()}",
            "",
        ]
        for section in self.sections:
            lines.append(f"## {section.title}")
            lines.extend(f"  - {item}" for item in section.items)
            lines.extend(f"  {key}: {value}" for key, value in section.metrics.items())
            lines.append("")
        return "\n".join(lines)


class DigestGenerator:
    """Generates daily digest from various Vetinari subsystems.

    Each data source is queried via a try/except to ensure partial failures
    don't prevent the digest from being generated. Missing data sources
    produce an "(unavailable)" item instead of crashing.
    """

    def generate_digest(self) -> DailyDigest:
        """Generate a complete daily digest.

        Queries tasks, learning, cost, approvals, and health subsystems
        to build a structured summary.

        Returns:
            A DailyDigest with all available sections populated.
        """
        now = datetime.now(timezone.utc).isoformat()
        sections: list[DigestSection] = [
            self._collect_tasks_section(),
            self._collect_learning_section(),
            self._collect_cost_section(),
            self._collect_approvals_section(),
            self._collect_health_section(),
        ]

        health = self._assess_overall_health(sections)

        return DailyDigest(
            generated_at=now,
            sections=sections,
            overall_health=health,
        )

    @staticmethod
    def _collect_tasks_section() -> DigestSection:
        """Collect task completion/failure summary."""
        section = DigestSection(title="Tasks")
        try:
            from vetinari.analytics.cost import get_cost_tracker

            tracker = get_cost_tracker()
            stats = tracker.get_period_summary() if hasattr(tracker, "get_period_summary") else {}
            section.metrics = {
                "completed": stats.get("tasks_completed", 0),
                "failed": stats.get("tasks_failed", 0),
            }
            section.items.append(
                f"{stats.get('tasks_completed', 0)} tasks completed, {stats.get('tasks_failed', 0)} failed"
            )
        except Exception:
            section.items.append("Task data unavailable")
        return section

    @staticmethod
    def _collect_learning_section() -> DigestSection:
        """Collect learning improvement summary."""
        section = DigestSection(title="Learning & Improvement")
        try:
            from vetinari.learning.training_collector import get_training_collector

            collector = get_training_collector()
            count = collector.count_records()
            section.items.append(f"{count} training records collected")
        except Exception:
            section.items.append("Learning data unavailable")
        return section

    @staticmethod
    def _collect_cost_section() -> DigestSection:
        """Collect cost and token usage summary."""
        section = DigestSection(title="Cost & Usage")
        try:
            from vetinari.analytics.cost import get_cost_tracker

            tracker = get_cost_tracker()
            total = tracker.get_total_cost() if hasattr(tracker, "get_total_cost") else 0.0
            section.metrics = {"total_cost_usd": round(total, 4)}
            section.items.append(f"Total cost: ${total:.4f}")
        except Exception:
            section.items.append("Cost data unavailable")
        return section

    @staticmethod
    def _collect_approvals_section() -> DigestSection:
        """Collect pending approval count."""
        section = DigestSection(title="Pending Approvals")
        try:
            from vetinari.autonomy.approval_queue import get_approval_queue

            pending = get_approval_queue().get_pending()
            count = len(pending)
            section.metrics = {"pending_count": count}
            if count > 0:
                section.items.append(f"{count} actions awaiting human approval")
            else:
                section.items.append("No pending approvals")
        except Exception:
            section.items.append("Approval data unavailable")
        return section

    @staticmethod
    def _collect_health_section() -> DigestSection:
        """Collect system health assessment from live subsystems.

        Queries the metrics collector and any registered health sources.
        Falls back to "unknown" when no health data is available rather than
        hardcoding "healthy", which would mask real degradation.
        """
        section = DigestSection(title="System Health")
        status = "unknown"
        details: list[str] = []

        try:
            from vetinari.metrics import get_metrics

            metrics = get_metrics()
            # Check for recent inference errors as a health proxy
            error_counter = metrics.get_counter("vetinari.api.request", status=500)
            total_counter = sum(metrics.get_counter("vetinari.api.request", status=code) for code in (200, 400, 500))
            if total_counter > 0:
                error_rate = error_counter / total_counter
                if error_rate > 0.1:
                    status = "degraded"
                    details.append(f"API error rate elevated: {error_rate:.1%}")
                else:
                    status = "healthy"
                    details.append("API error rate within normal range")
            else:
                status = "healthy"
                details.append("No API traffic recorded in this period")
        except Exception as exc:
            logger.warning("Could not collect health metrics for digest — proceeding without metrics: %s", exc)
            details.append("Health metrics unavailable")

        section.items.extend(details or ["No health data available"])
        section.metrics = {"status": status}
        return section

    @staticmethod
    def _assess_overall_health(sections: list[DigestSection]) -> str:
        """Determine overall health from section data.

        Returns ``"healthy"``, ``"warning"``, or ``"degraded"`` based on
        heuristics across all digest sections.  Uses the ``"failed"`` key
        from the Tasks section metrics (set by ``_collect_tasks_section()``).
        """
        for section in sections:
            if "unavailable" in " ".join(section.items).lower():
                return "warning"
            # Tasks section stores failures under "failed" (not "tasks_failed")
            failed = section.metrics.get("failed", 0)
            if isinstance(failed, int) and failed > 5:
                return "warning"
            # Health section may explicitly report degraded status
            if section.metrics.get("status") == "degraded":
                return "degraded"
        return "healthy"

    def send_digest(self) -> None:
        """Generate and dispatch the daily digest via NotificationManager.

        Sends the digest as a MEDIUM priority notification to all configured
        channels.  Logs confirmation only after ``notify()`` returns without
        raising, which means the notification was accepted for dispatch (though
        individual channel delivery may still fail asynchronously).
        """
        try:
            digest = self.generate_digest()
            from vetinari.notifications.manager import get_notification_manager

            notification_id = get_notification_manager().notify(
                title="Vetinari Daily Briefing",
                body=digest.to_text(),
                priority=NotificationPriority.MEDIUM,
                action_type="daily_digest",
                metadata=digest.to_dict(),
            )
            logger.info(
                "Daily digest dispatched (id=%s, health=%s)",
                notification_id,
                digest.overall_health,
            )
        except Exception:
            logger.warning("Failed to generate or send daily digest — will retry next schedule")
