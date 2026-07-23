"""Health snapshot helpers."""

from __future__ import annotations

import logging
from typing import Any

from vetinari.health.system import check_system_health

logger = logging.getLogger(__name__)


def take_health_snapshot() -> dict[str, Any]:
    """Return a health snapshot.

    Returns:
        Health snapshot mapping.
    """
    try:
        system = check_system_health()
    except Exception as exc:
        logger.warning("Exception handled by take health snapshot fallback", exc_info=True)
        return {
            "healthy": False,
            "checks": {"system": {"available": False, "error": str(exc)}},
            "degraded": True,
        }
    degraded = bool(system.get("degraded"))
    return {"healthy": not degraded, "checks": {"system": system}, "degraded": degraded}


__all__ = ["take_health_snapshot"]
