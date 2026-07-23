"""Pure backend tuning overlay helpers for benchmark candidates."""

from __future__ import annotations

import logging
from copy import deepcopy
from typing import Any

from vetinari.tuning.backend_tuning import (
    BackendTuningConfig,
    TuningApplicationResult,
    TuningProposal,
    validate_candidate,
)

logger = logging.getLogger(__name__)


_ALLOWED_BACKEND_KNOBS: dict[str, frozenset[str]] = {
    "am_engine": frozenset({
        "slots",
        "kv_cache_type_k",
        "kv_cache_type_v",
        "keep_alive",
        "preemption",
        "draft_pairing",
        "prefix_pins",
        "batch_token_budget",
    }),
    "vllm": frozenset({
        "gpu_memory_utilization",
        "max_model_len",
        "max_num_batched_tokens",
        "enable_prefix_caching",
        "prefix_cache_salt",
    }),
    "llama_cpp": frozenset({"n_gpu_layers", "n_batch", "n_ctx", "prompt_cache"}),
    "sglang": frozenset({"max_running_requests", "chunked_prefill_size", "schedule_policy"}),
    "retry_policy": frozenset({"max_attempts", "base_delay_ms", "max_delay_ms"}),
    "http": frozenset({"pool_max_connections", "timeout_s"}),
    "scheduler": frozenset({"lane_budgets"}),
    "search": frozenset({"fallback_order"}),
}


def apply_tuning_candidate(
    backend: str,
    current_settings: dict[str, Any],
    candidate: TuningProposal | dict[str, Any],
    *,
    capabilities: dict[str, Any] | None = None,
    config: BackendTuningConfig | None = None,
) -> TuningApplicationResult:
    """Return a candidate settings overlay without mutating live config.

    Args:
        backend: Backend value consumed by apply_tuning_candidate().
        current_settings: Current settings value consumed by apply_tuning_candidate().
        candidate: Candidate value consumed by apply_tuning_candidate().
        capabilities: Capabilities value consumed by apply_tuning_candidate().
        config: Config value consumed by apply_tuning_candidate().

    Returns:
        TuningApplicationResult value produced by apply_tuning_candidate().
    """
    if isinstance(candidate, TuningProposal) and config is not None:
        validate_candidate(config, candidate)
    knobs = candidate.knobs if isinstance(candidate, TuningProposal) else dict(candidate)
    facts = capabilities or {}
    settings = deepcopy(current_settings)
    blockers = _blocked_reasons(backend, settings, knobs, facts)
    if blockers:
        return TuningApplicationResult(
            status="blocked",
            backend=backend,
            settings=settings,
            blocked_reasons=tuple(blockers),
        )

    for knob, value in knobs.items():
        if backend == "scheduler" and knob == "lane_budgets":
            lane_settings = deepcopy(settings.get("lane_budgets", {}))
            for lane, budget in value.items():
                lane_settings[lane] = budget
            settings["lane_budgets"] = lane_settings
        else:
            settings[knob] = deepcopy(value)
    return TuningApplicationResult(
        status="applied",
        backend=backend,
        settings=settings,
        applied_knobs=tuple(sorted(knobs)),
    )


def _blocked_reasons(
    backend: str,
    current_settings: dict[str, Any],
    knobs: dict[str, Any],
    capabilities: dict[str, Any],
) -> list[str]:
    blockers: list[str] = []
    if backend == "am_engine":
        _require_present(current_settings, knobs, blockers)
        _reject_unknown_knobs(backend, knobs, blockers)
        for int_knob in ("slots", "keep_alive", "batch_token_budget"):
            if int_knob not in knobs:
                continue
            value = _coerce_int(knobs[int_knob], int_knob, blockers)
            if value is not None and value <= 0:
                blockers.append(f"{int_knob} must be positive")
    elif backend == "vllm":
        _require_present(current_settings, knobs, blockers)
        _reject_unknown_knobs(backend, knobs, blockers)
        if knobs.get("enable_prefix_caching") and not capabilities.get("prefix_cache"):
            blockers.append("vllm prefix cache unsupported by capability profile")
        if (
            knobs.get("enable_prefix_caching")
            and capabilities.get("shared_server")
            and not knobs.get("prefix_cache_salt")
        ):
            blockers.append("shared vLLM prefix cache requires prefix_cache_salt")
    elif backend == "llama_cpp":
        _require_present(current_settings, knobs, blockers)
        _reject_unknown_knobs(backend, knobs, blockers)
        for int_knob in ("n_gpu_layers", "n_batch", "n_ctx"):
            if int_knob not in knobs:
                continue
            value = _coerce_int(knobs[int_knob], int_knob, blockers)
            if value is not None and value < 0:
                blockers.append(f"{int_knob} cannot be negative")
    elif backend == "sglang":
        _require_present(current_settings, knobs, blockers)
        _reject_unknown_knobs(backend, knobs, blockers)
        if not capabilities.get("enabled"):
            blockers.append("SGLang tuning disabled")
    elif backend == "retry_policy":
        _reject_unknown_knobs(backend, knobs, blockers)
        for key in ("max_attempts", "base_delay_ms", "max_delay_ms"):
            if key not in knobs:
                continue
            value = _coerce_int(knobs[key], key, blockers)
            if value is not None and value < 0:
                blockers.append(f"{key} cannot be negative")
    elif backend == "http":
        _reject_unknown_knobs(backend, knobs, blockers)
        if "pool_max_connections" in knobs:
            pool = _coerce_int(knobs["pool_max_connections"], "pool_max_connections", blockers)
            if pool is not None and pool <= 0:
                blockers.append("pool_max_connections must be positive")
        if "timeout_s" in knobs:
            timeout = _coerce_float(knobs["timeout_s"], "timeout_s", blockers)
            if timeout is not None and timeout <= 0:
                blockers.append("timeout_s must be positive")
    elif backend == "scheduler":
        _reject_unknown_knobs(backend, knobs, blockers)
        lanes = set(current_settings.get("lane_budgets", {}))
        lane_budgets = knobs.get("lane_budgets", {})
        if "lane_budgets" in knobs and not isinstance(lane_budgets, dict):
            blockers.append("lane_budgets must be a mapping")
            lane_budgets = {}
        for lane, budget in lane_budgets.items():
            if lane not in lanes:
                blockers.append(f"unknown scheduler lane: {lane}")
            value = _coerce_int(budget, f"lane_budgets.{lane}", blockers)
            if value is not None and value <= 0:
                blockers.append(f"scheduler lane budget must be positive: {lane}")
    elif backend == "search":
        _reject_unknown_knobs(backend, knobs, blockers)
        order = knobs.get("fallback_order")
        if order is not None and (not isinstance(order, list) or not order):
            blockers.append("search fallback_order must be a non-empty list")
    else:
        blockers.append(f"unsupported backend: {backend}")
    return blockers


def _require_present(current_settings: dict[str, Any], knobs: dict[str, Any], blockers: list[str]) -> None:
    blockers.extend(
        f"missing baseline setting for knob: {knob}"
        for knob in knobs
        if knob not in current_settings and knob != "prefix_cache_salt"
    )


def _reject_unknown_knobs(backend: str, knobs: dict[str, Any], blockers: list[str]) -> None:
    allowed = _ALLOWED_BACKEND_KNOBS.get(backend)
    if allowed is None:
        return
    blockers.extend(f"unsupported knob for {backend}: {knob}" for knob in sorted(knobs) if knob not in allowed)


def _coerce_int(value: Any, field_name: str, blockers: list[str]) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        logger.warning("field=%r raw_value=%r expected=int coerce failed", field_name, value, exc_info=True)
        blockers.append(f"{field_name} must be an integer")
        return None


def _coerce_float(value: Any, field_name: str, blockers: list[str]) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        logger.warning("field=%r raw_value=%r expected=float coerce failed", field_name, value, exc_info=True)
        blockers.append(f"{field_name} must be numeric")
        return None
