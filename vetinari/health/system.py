"""System health helpers."""

from __future__ import annotations

import logging
from typing import Any

import psutil

logger = logging.getLogger(__name__)


def _probe_memory() -> dict[str, float | bool | str]:
    try:
        memory = psutil.virtual_memory()
    except Exception as exc:
        logger.warning("Exception handled by  probe memory fallback", exc_info=True)
        return {"available": False, "error": str(exc), "total_gb": 0.0, "available_gb": 0.0}
    gib = 1024**3
    return {
        "available": True,
        "total_gb": round(memory.total / gib, 3),
        "available_gb": round(memory.available / gib, 3),
        "percent": float(memory.percent),
    }


def check_system_health() -> dict[str, object]:
    """Check host system health.

    Returns:
        System health mapping.
    """
    memory = _probe_memory()
    degraded = not bool(memory.get("available")) or float(memory.get("available_gb") or 0.0) <= 0.0
    checks: dict[str, Any] = {"memory": memory}
    return {"memory": memory, "checks": checks, "degraded": degraded}


__all__ = ["check_system_health"]
