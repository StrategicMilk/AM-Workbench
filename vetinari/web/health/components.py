"""Component health payloads for system-status handlers."""

from __future__ import annotations

import logging
from typing import Any

from vetinari.system.health_monitor import get_health_snapshot
from vetinari.workbench.health_checks import get_workbench_health_snapshot
from vetinari.workbench.status.contracts import WorkbenchHealthState
from vetinari.workbench.status.probes import build_metadata_spine_probe_snapshots

logger = logging.getLogger(__name__)


def collect_system_status_components() -> dict[str, Any]:
    """Return fail-closed component health for the reachable status API.

    Returns:
        A status payload with overall state, component rows, and a degraded
        flag for API consumers.
    """
    components: list[dict[str, Any]] = []
    components.extend(_system_components())
    components.append(_workbench_component())
    overall = _overall_status(components)
    return {
        "overall": overall,
        "components": components,
        "degraded": overall != "healthy",
    }


def _system_components() -> list[dict[str, Any]]:
    try:
        snapshot = get_health_snapshot()
    except Exception:
        logger.warning(
            "System health snapshot unavailable during status request",
            exc_info=True,
            extra={
                "action": "collect_system_health_components",
                "impact": "system status component health failed closed",
            },
        )
        return [
            {
                "name": "system_health",
                "healthy": False,
                "status": "unavailable",
                "detail": "system health snapshot unavailable; status failed closed",
            }
        ]

    payload = snapshot.to_dict()
    component_rows = payload.get("components")
    if not isinstance(component_rows, list):
        return [
            {
                "name": "system_health",
                "healthy": False,
                "status": "unreadable",
                "detail": "system health components missing or malformed; status failed closed",
            }
        ]

    results: list[dict[str, Any]] = []
    for item in component_rows:
        if not isinstance(item, dict):
            results.append({
                "name": "system_health",
                "healthy": False,
                "status": "unreadable",
                "detail": "system health component row unreadable; status failed closed",
            })
            continue
        healthy = bool(item.get("healthy"))
        results.append({
            "name": str(item.get("name") or "system_health"),
            "healthy": healthy,
            "status": "healthy" if healthy else "unhealthy",
            "detail": str(item.get("detail") or ""),
        })
    return results


def _workbench_component() -> dict[str, Any]:
    try:
        dependency_snapshots = build_metadata_spine_probe_snapshots()
        snapshot = get_workbench_health_snapshot(dependency_snapshots=dependency_snapshots)
    except Exception:
        logger.warning(
            "Workbench component health unavailable during status request",
            exc_info=True,
            extra={
                "action": "collect_workbench_health_components",
                "impact": "workbench status component health failed closed",
            },
        )
        return {
            "name": "workbench",
            "healthy": False,
            "status": WorkbenchHealthState.BROKEN.value,
            "detail": "workbench component health unavailable; status failed closed",
        }

    unhealthy = {
        WorkbenchHealthState.BROKEN,
        WorkbenchHealthState.DEGRADED,
        WorkbenchHealthState.STALE,
    }
    state = snapshot.overall_state
    return {
        "name": "workbench",
        "healthy": state not in unhealthy,
        "status": state.value,
        "detail": f"{len(snapshot.results)} workbench status domain(s) checked from metadata spine probes",
        "state_counts": {key.value: value for key, value in snapshot.state_counts.items()},
    }


def _overall_status(components: list[dict[str, Any]]) -> str:
    if not components:
        return "unhealthy"
    unhealthy_count = sum(1 for component in components if not component.get("healthy"))
    if unhealthy_count == 0:
        return "healthy"
    if unhealthy_count == 1:
        return "degraded"
    return "unhealthy"


__all__ = ["collect_system_status_components"]
