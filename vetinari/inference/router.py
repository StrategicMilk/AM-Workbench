"""Two-axis compute router for inference requests."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any

from vetinari.events import CpuTierRouteStatusChanged, Event, get_event_bus
from vetinari.inference.request import RoutedInferenceRequest
from vetinari.inference.result import NoCapacityError
from vetinari.workbench.effective_config import capture_model_selection_config_snapshot

_QUALITY_RANK = {"draft": 0, "standard": 1, "premium": 2}
DEVELOPER_WORKFLOW_CONTRACT_ID = "wave3-rcg0014p06-scope-followup-P09"
INFERENCE_ROUTER_WORKFLOW_GUARDS: tuple[str, ...] = (
    "unknown capabilities raise NoCapacityError",
    "unhealthy compute tiers are skipped before target selection",
    "quality floors are enforced before latency comparison",
    "selection records an effective config snapshot id",
)


def developer_workflow_contract() -> dict[str, object]:
    """Return inference-router workflow guarantees verified by this follow-up pack."""
    return {
        "pack": DEVELOPER_WORKFLOW_CONTRACT_ID,
        "surface": "vetinari/inference/router.py",
        "guards": INFERENCE_ROUTER_WORKFLOW_GUARDS,
    }


@dataclass(frozen=True, slots=True)
class ComputeTarget:
    """Selected compute target for one inference request."""

    compute: str
    model: str
    estimated_latency_s: float
    quality: str = "standard"
    escalation_margin: float | None = None
    effective_config_snapshot_id: str | None = None

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"ComputeTarget(compute={self.compute!r}, model={self.model!r}, estimated_latency_s={self.estimated_latency_s!r})"


# Side effects:
#   - _tier_status_cache stores last-known tier health from CpuTierRouteStatusChanged.
#   - _tier_status_lock guards writer callbacks and select_target snapshots.
_tier_status_cache: dict[str, CpuTierRouteStatusChanged] = {}
_tier_status_lock = threading.Lock()
_SUBSCRIBED = False


def _handle_tier_status(event: Event) -> None:
    if not isinstance(event, CpuTierRouteStatusChanged):
        return
    with _tier_status_lock:
        _tier_status_cache[event.compute_id] = event


def _ensure_status_subscription() -> None:
    global _SUBSCRIBED
    if _SUBSCRIBED:
        return
    get_event_bus().subscribe(CpuTierRouteStatusChanged, _handle_tier_status)
    _SUBSCRIBED = True


def update_tier_status(compute_id: str, state: str, *, queue_depth: int = 0) -> None:
    """Update the router status cache directly for tests and local probes.

    Args:
        compute_id: Compute id value consumed by update_tier_status().
        state: State value consumed by update_tier_status().
        queue_depth: Queue depth value consumed by update_tier_status().
    """
    timestamp = time.time()
    event = CpuTierRouteStatusChanged(
        event_type="CpuTierRouteStatusChanged",
        timestamp=timestamp,
        compute_id=compute_id,
        state=state,
        queue_depth=queue_depth,
    )
    with _tier_status_lock:
        _tier_status_cache[compute_id] = event
    get_event_bus().publish(
        CpuTierRouteStatusChanged(
            event_type="CpuTierRouteStatusChanged",
            timestamp=timestamp,
            compute_id=compute_id,
            state=state,
            queue_depth=queue_depth,
        )
    )


def reset_tier_status() -> None:
    """Clear cached compute-tier health state under the production lock."""
    with _tier_status_lock:
        _tier_status_cache.clear()


def select_target(request: RoutedInferenceRequest, config: dict[str, Any]) -> ComputeTarget:
    """Select the cheapest healthy target that satisfies the request budget.

    Args:
        request: Request object sent through the operation.
        config: Config value consumed by select_target().

    Returns:
        Resolved target value.

    Raises:
        Exception: Propagates validation or runtime failures from the underlying operation.
    """
    _ensure_status_subscription()
    capabilities = config.get("capabilities", {})
    capability = capabilities.get(request.capability)
    if not capability:
        raise NoCapacityError(
            f"unknown capability: {request.capability}",
            capability=request.capability,
            budget_s=request.latency_budget_s,
        )

    with _tier_status_lock:
        status_snapshot = dict(_tier_status_cache)

    candidates: list[ComputeTarget] = []
    for raw in capability.get("targets", []):
        compute = str(raw["compute"])
        status = status_snapshot.get(compute)
        if status is not None and status.state != "ready":
            continue
        if _QUALITY_RANK.get(str(raw.get("quality", "standard")), 1) < _QUALITY_RANK.get(request.quality_floor, 1):
            continue
        queue_depth = status.queue_depth if status is not None else int(raw.get("queue_depth", 0))
        estimated_ms = (
            queue_depth * float(raw.get("p95_per_request_ms", raw.get("p95_per_request", 0)))
            + float(raw["p95_ms_per_tok"]) * request.max_tokens
        )
        estimated_s = estimated_ms / 1000.0
        if estimated_s <= request.latency_budget_s:
            candidates.append(
                ComputeTarget(
                    compute=compute,
                    model=str(raw["model"]),
                    estimated_latency_s=estimated_s,
                    quality=str(raw.get("quality", "standard")),
                    escalation_margin=raw.get("escalation_margin"),
                )
            )

    if not candidates:
        raise NoCapacityError(
            f"no target for capability {request.capability!r} within {request.latency_budget_s}s",
            capability=request.capability,
            budget_s=request.latency_budget_s,
        )
    selected = min(candidates, key=lambda target: target.estimated_latency_s)
    snapshot = capture_model_selection_config_snapshot(request, config, selected)
    return ComputeTarget(
        compute=selected.compute,
        model=selected.model,
        estimated_latency_s=selected.estimated_latency_s,
        quality=selected.quality,
        escalation_margin=selected.escalation_margin,
        effective_config_snapshot_id=snapshot.snapshot_id,
    )


__all__ = ["ComputeTarget", "developer_workflow_contract", "reset_tier_status", "select_target", "update_tier_status"]
