"""Configuration loading helpers for the workbench scheduler."""

from __future__ import annotations

import copy
import functools
from datetime import time as dt_time
from pathlib import Path
from typing import Any

import yaml

from vetinari.runtime.workbench_scheduler_types import Lane, VRAMOverCommit, WorkbenchSchedulerConfigError


@functools.lru_cache(maxsize=4)
def _load_compute_routing_config_cached(path_str: str) -> dict[str, Any]:
    """Read and parse a compute routing YAML once, then memoize.

    Memoization key is the resolved POSIX path string so equivalent
    ``Path`` instances hit the same cache slot.  Caller MUST treat the
    returned dict as immutable; ``load_compute_routing_config`` returns
    a deep copy so caller mutations cannot bleed back into the cache.
    """
    with Path(path_str).open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    if not isinstance(data, dict):
        raise WorkbenchSchedulerConfigError("config/compute_routing.yaml must be a mapping")
    return data


def load_lane_capacity(config: dict[str, Any]) -> dict[Lane, int]:
    """Load per-lane capacity limits from scheduler config.

    Args:
    config: Parsed scheduler YAML mapping.

    Returns:
    Mapping of scheduler lane to max concurrent inference count.

    Raises:
        WorkbenchSchedulerConfigError: Propagated when validation, persistence, or execution fails.
    """
    capacities: dict[Lane, int] = {}
    for lane in Lane:
        raw = config["lanes"][lane.value]
        capacity = int(raw["max_concurrent_inferences"])
        if capacity < 1:
            raise WorkbenchSchedulerConfigError(f"lanes.{lane.value}.max_concurrent_inferences must be >= 1")
        capacities[lane] = capacity
    return capacities


def load_config(path: Path) -> dict[str, Any]:
    """Read and validate the scheduler YAML config.

    Args:
    path: YAML file path.

    Returns:
    Parsed scheduler config mapping.

    Raises:
        WorkbenchSchedulerConfigError: Propagated when validation, persistence, or execution fails.
        VRAMOverCommit: Propagated when validation, persistence, or execution fails.
    """
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle)
    except yaml.YAMLError as exc:
        raise WorkbenchSchedulerConfigError(f"invalid YAML in {path}") from exc
    except OSError as exc:
        raise WorkbenchSchedulerConfigError(f"unable to read scheduler config {path}") from exc
    if not isinstance(data, dict):
        raise WorkbenchSchedulerConfigError(f"scheduler config {path} must be a mapping")
    for key in ("lanes", "preemption", "vram_shares"):
        if key not in data:
            raise WorkbenchSchedulerConfigError(f"scheduler config missing required key: {key}")
    for lane in Lane:
        if lane.value not in data["lanes"]:
            raise WorkbenchSchedulerConfigError(f"scheduler config missing lane: {lane.value}")
        if lane.value not in data["vram_shares"]:
            raise WorkbenchSchedulerConfigError(f"scheduler config missing vram share: {lane.value}")
    timeout = float(data.get("preemption", {}).get("checkpoint_timeout_s", 30.0))
    if timeout < 0:
        raise WorkbenchSchedulerConfigError("preemption.checkpoint_timeout_s must be non-negative")
    share_sum = sum(float(value) for value in data["vram_shares"].values())
    if share_sum > 1.0 + 1e-9:
        raise VRAMOverCommit(f"declared VRAM shares exceed 1.0: {share_sum:.3f}")
    return data


def load_compute_routing_config() -> dict[str, Any]:
    """Read the compute routing config (cached + deep-copied).

    Disk reads are memoized by
    :func:`_load_compute_routing_config_cached`; every call returns a
    fresh ``copy.deepcopy`` of the cached payload so caller mutations
    cannot bleed back into the cache.  Tests can clear the cache via
    ``_load_compute_routing_config_cached.cache_clear()``.

    Returns:
    Parsed compute routing config mapping.

    Raises:
        WorkbenchSchedulerConfigError: Propagated when validation, persistence, or execution fails.
    """
    config_path = Path(__file__).resolve().parent.parent.parent / "config" / "compute_routing.yaml"
    data = _load_compute_routing_config_cached(config_path.as_posix())
    return copy.deepcopy(data)


def parse_hhmm(value: str) -> dt_time:
    """Parse a ``HH:MM`` wall-clock time value.

    Args:
        value: Time string.

    Returns:
        Parsed ``datetime.time`` value.
    """
    hour, minute = value.split(":", 1)
    return dt_time(hour=int(hour), minute=int(minute))


__all__ = ["load_compute_routing_config", "load_config", "load_lane_capacity", "parse_hhmm"]
