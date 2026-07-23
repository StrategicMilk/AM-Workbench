"""Extracted WorkbenchScheduler scheduling policy responsibilities."""

from __future__ import annotations

import logging
import sys
import threading
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from datetime import time as dt_time
from typing import TYPE_CHECKING, Any

from vetinari.runtime.workbench_scheduler_bridge import RustSchedulerBridgeSnapshot
from vetinari.runtime.workbench_scheduler_config import load_compute_routing_config, parse_hhmm
from vetinari.runtime.workbench_scheduler_types import (
    Lane,
    Lease,
    VRAMOverCommit,
    WorkbenchSchedulerConfigError,
)

if TYPE_CHECKING:
    from vetinari.inference import ComputeTarget, RoutedInferenceRequest
    from vetinari.runtime.workbench_scheduler_bridge import RustSchedulerBridge

logger = logging.getLogger(__name__)
NoCapacityError: type[Exception] | None = None
select_target: Callable[..., Any] | None = None


def _load_inference_runtime() -> tuple[type[Exception], Callable[..., Any]]:
    global NoCapacityError, select_target
    scheduler_module = sys.modules.get("vetinari.runtime.workbench_scheduler")
    if scheduler_module is not None:
        patched_error = getattr(scheduler_module, "NoCapacityError", None)
        patched_selector = getattr(scheduler_module, "select_target", None)
        if patched_error is not None and patched_selector is not None:
            return patched_error, patched_selector
    if NoCapacityError is None or select_target is None:
        from vetinari.inference import NoCapacityError as loaded_no_capacity_error
        from vetinari.inference import select_target as loaded_select_target

        NoCapacityError = loaded_no_capacity_error
        if select_target is None:
            select_target = loaded_select_target
    return NoCapacityError, select_target


def _parse_hhmm(value: str) -> Any:
    """Resolve the patchable scheduler parse seam, then parse a HH:MM value."""
    scheduler_module = sys.modules.get("vetinari.runtime.workbench_scheduler")
    if scheduler_module is not None:
        patched_parse = getattr(scheduler_module, "parse_hhmm", None)
        if patched_parse is not None:
            return patched_parse(value)
    return parse_hhmm(value)


class WorkbenchSchedulerPolicy:
    """Named collaborator marker for WorkbenchScheduler scheduling policy responsibilities."""


SchedulerPolicy = WorkbenchSchedulerPolicy


def validate_scheduler_policy_runtime_contract(
    config: Mapping[str, Any],
    *,
    active_count: Mapping[Lane, int] | None = None,
) -> list[str]:
    """Return fail-closed scheduler policy contract errors.

    This is intentionally side-effect free so operator checks can validate the
    same lane, VRAM, and training-window invariants before the scheduler starts
    accepting work. Missing or malformed policy state is reported as an error
    instead of being treated as "no restriction configured."

    Returns:
        A list of contract errors; an empty list means the policy is usable.
    """
    errors: list[str] = []
    if not isinstance(config, Mapping):
        return ["scheduler policy config must be a mapping"]

    lanes = config.get("lanes")
    if not isinstance(lanes, Mapping):
        errors.append("scheduler policy config missing lanes mapping")
        lanes = {}
    shares = config.get("vram_shares")
    if not isinstance(shares, Mapping):
        errors.append("scheduler policy config missing vram_shares mapping")
        shares = {}

    declared_total = 0.0
    for lane in Lane:
        lane_config = lanes.get(lane.value)
        if not isinstance(lane_config, Mapping):
            errors.append(f"lane {lane.value} missing configuration")
        else:
            try:
                capacity = int(lane_config["max_concurrent_inferences"])
                if capacity <= 0:
                    errors.append(f"lane {lane.value} capacity must be positive")
            except (KeyError, TypeError, ValueError):
                errors.append(f"lane {lane.value} has invalid max_concurrent_inferences")

        try:
            share = float(shares[lane.value])
            if share <= 0:
                errors.append(f"lane {lane.value} VRAM share must be positive")
            declared_total += share
        except (KeyError, TypeError, ValueError):
            errors.append(f"lane {lane.value} has invalid VRAM share")

    if declared_total > 1.0 + 1e-9:
        errors.append(f"declared VRAM shares exceed 1.0: {declared_total:.3f}")

    windows = config.get("training_allowed_windows") or []
    if not isinstance(windows, list):
        errors.append("training_allowed_windows must be a list")
    else:
        for index, window in enumerate(windows):
            if not isinstance(window, Mapping):
                errors.append(f"training window {index} must be a mapping")
                continue
            if str(window.get("timezone", "UTC")).upper() != "UTC":
                errors.append(f"training window {index} uses unsupported timezone")
            for key in ("start", "end"):
                if key not in window:
                    errors.append(f"training window {index} missing {key}")
                    continue
                try:
                    _parse_hhmm(str(window[key]))
                except Exception:
                    errors.append(f"training window {index} has invalid {key}")

    if active_count is not None:
        for lane, count in active_count.items():
            try:
                coerced_lane = lane if isinstance(lane, Lane) else Lane(str(lane))
            except ValueError:
                errors.append(f"unknown active lane {lane!r}")
                coerced_lane = None
            lane_config = lanes.get(coerced_lane.value) if coerced_lane is not None else None
            if coerced_lane is not None and isinstance(lane_config, Mapping):
                try:
                    capacity = int(lane_config["max_concurrent_inferences"])
                    if int(count) > capacity:
                        errors.append(
                            f"lane {coerced_lane.value} active count {int(count)} exceeds capacity {capacity}"
                        )
                except (KeyError, TypeError, ValueError):
                    errors.append(f"lane {coerced_lane.value} has invalid active capacity state")

    return errors


class WorkbenchSchedulerPolicyMixin:
    """Mixin containing WorkbenchScheduler scheduling policy behavior."""

    if TYPE_CHECKING:
        _active_count: dict[Lane, int]
        _config: dict[str, Any]
        _lane_capacity: dict[Lane, int]
        _rust_bridge: RustSchedulerBridge
        _state_lock: threading.Lock
        _training_windows_parsed: list[tuple[dt_time, dt_time]]

    def rust_authority_snapshot(self) -> RustSchedulerBridgeSnapshot:
        """Return the Rust scheduler authority bridge state.

        Returns:
            RustSchedulerBridgeSnapshot value produced by rust_authority_snapshot().
        """
        return self._rust_bridge.snapshot()

    def _pick_target(self, request: RoutedInferenceRequest) -> ComputeTarget:
        """Forward target selection to the inference router."""
        no_capacity_error, target_selector = _load_inference_runtime()
        try:
            return target_selector(request, load_compute_routing_config())
        except no_capacity_error as exc:
            raise VRAMOverCommit(f"no compute target available for {request.capability!r}") from exc

    def _vram_preflight(self, lane: Lane) -> None:
        """Validate declared-share headroom for a requested lane."""
        with self._state_lock:
            self._vram_preflight_locked(self._coerce_lane(lane))

    def _check_training_window(self, now: datetime) -> bool:
        """Return whether training is allowed at the supplied instant.

        Args:
            now: Time to test. Naive values are interpreted as UTC.

        Returns:
            True when training is allowed.
        """
        windows = self._training_windows_parsed
        if not windows:
            return True
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
        current = now.astimezone(UTC).time()
        for start, end in windows:
            if start <= end:
                if start <= current <= end:
                    return True
            elif current >= start or current <= end:
                return True
        return False

    def _parse_training_windows(self) -> list[tuple[Any, Any]]:
        windows = self._config.get("training_allowed_windows") or []
        if not isinstance(windows, list):
            raise WorkbenchSchedulerConfigError("training_allowed_windows must be a list")
        parsed = []
        for index, window in enumerate(windows):
            if not isinstance(window, Mapping):
                raise WorkbenchSchedulerConfigError(f"training window {index} must be a mapping")
            if str(window.get("timezone", "UTC")).upper() != "UTC":
                raise WorkbenchSchedulerConfigError("only UTC training windows are supported")
            try:
                parsed.append((_parse_hhmm(str(window["start"])), _parse_hhmm(str(window["end"]))))
            except (KeyError, TypeError, ValueError) as exc:
                raise WorkbenchSchedulerConfigError(f"invalid training window {index}") from exc
        return parsed

    def scheduler_policy_contract_errors(self) -> list[str]:
        """Return current policy contract errors for this scheduler host.

        Returns:
            A list of fail-closed policy contract errors.
        """
        with self._state_lock:
            return validate_scheduler_policy_runtime_contract(self._config, active_count=self._active_count)

    def _coerce_lane(self, lane: Lane) -> Lane:
        if isinstance(lane, Lane):
            return lane
        return Lane(str(lane))

    def _slot_available_locked(self, lane: Lane, *, preempt_lease: Lease | None) -> bool:
        if lane is Lane.HUB_AGENT and self._active_count[Lane.TRAINING] > 0:
            return False
        if lane is Lane.INTERACTIVE and self._active_count[Lane.TRAINING] > 0 and preempt_lease is None:
            return False
        if self._active_count[lane] < self._lane_capacity[lane]:
            return True
        return lane is Lane.INTERACTIVE and preempt_lease is not None

    def _vram_preflight_locked(self, lane: Lane) -> None:
        try:
            shares = self._config["vram_shares"]
            lane_share = float(shares[lane.value])
            if lane_share <= 0:
                raise VRAMOverCommit(f"lane {lane.value} has non-positive declared VRAM share {lane_share}")
            active_total = lane_share
            for active_lane, count in self._active_count.items():
                if count > 0:
                    active_total += float(shares[active_lane.value])
        except VRAMOverCommit:
            raise
        except Exception as exc:
            raise VRAMOverCommit(f"VRAM preflight failed closed for lane {lane.value}") from exc
        if active_total > 1.0 + 1e-9:
            raise VRAMOverCommit(f"lane {lane.value} declared VRAM share would exceed 1.0: {active_total:.3f}")

    def _vram_headroom_available_locked(self) -> bool:
        try:
            shares = self._config["vram_shares"]
            total = sum(float(shares[lane.value]) for lane, count in self._active_count.items() if count > 0)
        except Exception as exc:
            logger.warning("VRAM headroom check failed closed: %s", exc)
            return False
        return total <= 1.0 + 1e-9


__all__ = [
    "SchedulerPolicy",
    "WorkbenchSchedulerPolicyMixin",
    "validate_scheduler_policy_runtime_contract",
]
