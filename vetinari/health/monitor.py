"""Health monitor compatibility helpers."""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)


class HealthMonitor:
    """Minimal health monitor facade."""

    def __init__(self) -> None:
        self._alive = True

    def is_alive(self) -> bool:
        """Return whether the monitor is active."""
        return self._alive

    def get_snapshot(self) -> dict[str, Any]:
        """Return the current health snapshot."""
        return {"stale": not self._alive, "healthy": self._alive}


def start_health_monitor() -> threading.Thread:
    """Start the health monitor.

    Returns:
        Started daemon monitor thread.
    """
    thread = threading.Thread(target=_monitor_loop, name="vetinari-health-monitor", daemon=True)
    thread.start()
    return thread


def _monitor_loop(interval_seconds: float = 1.0) -> None:
    while True:
        try:
            logger.debug("health monitor tick")
            time.sleep(interval_seconds)
        except Exception:
            logger.exception("health monitor tick failed")


__all__ = ["HealthMonitor", "start_health_monitor"]
