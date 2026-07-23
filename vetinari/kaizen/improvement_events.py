"""Kaizen improvement event emitters — publish lifecycle events to the EventBus.

Each function fires a typed event when an improvement transitions state
(proposed → active → confirmed/reverted). Consumers that subscribe to these
events (aggregator, notification system) receive real-time updates without
polling the database.

These are standalone functions so they can be unit-tested without
instantiating ImprovementLog.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)
_SUBSCRIBED = False
_SUBSCRIBED_BUS_ID: int | None = None
_SUBSCRIBE_LOCK = threading.Lock()


def _log_kaizen_event(event: Any) -> None:
    logger.info("kaizen event observed: %r", event)


def _ensure_kaizen_event_subscribers() -> None:
    global _SUBSCRIBED, _SUBSCRIBED_BUS_ID
    from vetinari.events import (
        KaizenImprovementActive,
        KaizenImprovementConfirmed,
        KaizenImprovementProposed,
        KaizenImprovementReverted,
        KaizenLintFinding,
        get_event_bus,
    )

    with _SUBSCRIBE_LOCK:
        bus = get_event_bus()
        bus_id = id(bus)
        if _SUBSCRIBED and bus_id == _SUBSCRIBED_BUS_ID:
            return
        bus.subscribe(KaizenImprovementProposed, _log_kaizen_event)
        bus.subscribe(KaizenImprovementConfirmed, _log_kaizen_event)
        bus.subscribe(KaizenImprovementActive, _log_kaizen_event)
        bus.subscribe(KaizenImprovementReverted, _log_kaizen_event)
        bus.subscribe(KaizenLintFinding, _log_kaizen_event)
        _SUBSCRIBED = True
        _SUBSCRIBED_BUS_ID = bus_id


def emit_proposed(
    improvement_id: str,
    hypothesis: str,
    metric: str,
    applied_by: str,
) -> None:
    """Emit a KaizenImprovementProposed event on the EventBus.

    Logs at WARNING with full traceback on failure so publish errors are
    visible in logs rather than silently dropped.

    Args:
        improvement_id: The improvement that was proposed.
        hypothesis: The improvement hypothesis.
        metric: Which metric is targeted.
        applied_by: Which subsystem proposed it.

    Raises:
        Exception: If EventBus publishing fails.
    """
    try:
        from vetinari.events import KaizenImprovementProposed, get_event_bus

        _ensure_kaizen_event_subscribers()
        bus = get_event_bus()
        bus.publish(
            KaizenImprovementProposed(
                event_type="KaizenImprovementProposed",
                timestamp=time.time(),
                improvement_id=improvement_id,
                hypothesis=hypothesis,
                metric=metric,
                applied_by=applied_by,
            )
        )
    except Exception:
        logger.error(
            "Failed to emit KaizenImprovementProposed for improvement %s — "
            "consumers will not receive the proposed event; check EventBus health",
            improvement_id,
            exc_info=True,
        )
        raise


def emit_confirmed(
    improvement_id: str,
    metric: str,
    baseline_value: float,
    actual_value: float,
    applied_by: str,
) -> None:
    """Emit a KaizenImprovementConfirmed event on the EventBus.

    Args:
        improvement_id: The improvement that was confirmed.
        metric: Which metric improved.
        baseline_value: Pre-improvement baseline.
        actual_value: Post-improvement actual.
        applied_by: Which subsystem applied it.

    Raises:
        Exception: If EventBus publishing fails.
    """
    try:
        from vetinari.events import KaizenImprovementConfirmed, get_event_bus

        _ensure_kaizen_event_subscribers()
        bus = get_event_bus()
        bus.publish(
            KaizenImprovementConfirmed(
                event_type="KaizenImprovementConfirmed",
                timestamp=time.time(),
                improvement_id=improvement_id,
                metric=metric,
                baseline_value=baseline_value,
                actual_value=actual_value,
                applied_by=applied_by,
            )
        )
    except Exception:
        logger.error(
            "Failed to emit KaizenImprovementConfirmed for improvement %s — "
            "consumers will not receive the confirmed event; check EventBus health",
            improvement_id,
            exc_info=True,
        )
        raise


def emit_active(
    improvement_id: str,
    metric: str,
    applied_by: str,
) -> None:
    """Emit a KaizenImprovementActive event when an improvement starts its trial.

    Called by PDCA when an improvement moves from PROPOSED to ACTIVE state.
    Consumers (aggregator, notification system) use this event to start
    collecting observations for the improvement's observation window.

    Args:
        improvement_id: The improvement that is now being trialled.
        metric: Which metric the improvement targets.
        applied_by: Which subsystem activated the improvement.

    Raises:
        Exception: If EventBus publishing fails.
    """
    try:
        from vetinari.events import KaizenImprovementActive, get_event_bus

        _ensure_kaizen_event_subscribers()
        bus = get_event_bus()
        bus.publish(
            KaizenImprovementActive(
                event_type="KaizenImprovementActive",
                timestamp=time.time(),
                improvement_id=improvement_id,
                metric=metric,
                applied_by=applied_by,
            )
        )
    except Exception:
        logger.error(
            "Failed to emit KaizenImprovementActive for improvement %s — "
            "observation window may not start correctly; check EventBus health",
            improvement_id,
            exc_info=True,
        )
        raise


def emit_reverted(improvement_id: str, metric: str, reason: str) -> None:
    """Emit a KaizenImprovementReverted event on the EventBus.

    Args:
        improvement_id: The improvement that was reverted.
        metric: The metric this improvement was tracking.
        reason: Why it was reverted.

    Raises:
        Exception: If EventBus publishing fails.
    """
    try:
        from vetinari.events import KaizenImprovementReverted, get_event_bus

        _ensure_kaizen_event_subscribers()
        bus = get_event_bus()
        bus.publish(
            KaizenImprovementReverted(
                event_type="KaizenImprovementReverted",
                timestamp=time.time(),
                improvement_id=improvement_id,
                metric=metric,
                reason=reason,
            )
        )
    except Exception:
        logger.error(
            "Failed to emit KaizenImprovementReverted for improvement %s — "
            "consumers will not receive the reverted event; check EventBus health",
            improvement_id,
            exc_info=True,
        )
        raise


def emit_lint_finding(
    finding_id: str,
    category: str,
    description: str,
    severity: str = "warning",
) -> None:
    """Emit a KaizenLintFinding event when knowledge lint detects an issue.

    Args:
        finding_id: Unique identifier for the lint finding.
        category: Lint category (contradiction, stale, orphaned, vocabulary_drift).
        description: Human-readable description of the finding.
        severity: Finding severity (info, warning, error).

    Raises:
        Exception: If EventBus publishing fails.
    """
    try:
        from vetinari.events import KaizenLintFinding, get_event_bus

        _ensure_kaizen_event_subscribers()
        bus = get_event_bus()
        bus.publish(
            KaizenLintFinding(
                event_type="KaizenLintFinding",
                timestamp=time.time(),
                finding_id=finding_id,
                category=category,
                description=description,
                severity=severity,
            )
        )
    except Exception:
        logger.error(
            "Failed to emit KaizenLintFinding for finding %s — consumers will not "
            "receive the lint finding event; check EventBus health",
            finding_id,
            exc_info=True,
        )
        raise
