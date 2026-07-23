"""Probe and readiness helpers for local runtime onboarding."""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from typing import Any

import httpx

from vetinari.inference import ComputeTarget, RoutedInferenceRequest, select_target
from vetinari.models.model_registry import ModelRegistryEntry
from vetinari.runtime.workbench_scheduler import Lane, WorkbenchScheduler
from vetinari.workbench.local_runtime_contracts import (
    BlockerKind,
    HardwareFit,
    LocalRuntimeBlocker,
    LocalRuntimeKind,
    LocalRuntimeProbeError,
    LocalRuntimeProbeResult,
)

logger = logging.getLogger(__name__)

_DEFAULT_LMSTUDIO_BASE = os.environ.get("VETINARI_LMSTUDIO_BASE_URL", "http://127.0.0.1:1234")
_DEFAULT_JAN_BASE = os.environ.get("VETINARI_JAN_BASE_URL", "http://127.0.0.1:1337")
_DEFAULT_OPENWEBUI_BASE = os.environ.get("VETINARI_OPENWEBUI_BASE_URL", "http://127.0.0.1:3000")
_PROBE_TIMEOUT_S = 5.0
_HARDWARE_FIT_PROBE_MAX_TOKENS = 16


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalise_models(payload: Any, *, keys: tuple[str, ...]) -> tuple[dict[str, Any], ...]:
    raw_models: Any = payload
    for key in keys:
        if isinstance(payload, Mapping) and key in payload:
            raw_models = payload[key]
            break
    if isinstance(raw_models, Mapping):
        raw_models = [raw_models]
    if not isinstance(raw_models, list):
        raise LocalRuntimeProbeError("runtime response did not contain a model list")
    models: list[dict[str, Any]] = []
    for raw in raw_models:
        if not isinstance(raw, Mapping):
            continue
        model_id = str(raw.get("id") or raw.get("model") or raw.get("name") or "").strip()
        if model_id:
            model = dict(raw)
            model["id"] = model_id
            models.append(model)
    return tuple(models)


def _probe_json_endpoint(
    client: httpx.Client,
    *,
    runtime_kind: LocalRuntimeKind,
    base_url: str,
    path: str,
    model_keys: tuple[str, ...],
) -> LocalRuntimeProbeResult:
    checked_at_utc = _utc_now_iso()
    started = time.perf_counter()
    url = f"{base_url.rstrip('/')}{path}"
    try:
        response = client.get(url, timeout=_PROBE_TIMEOUT_S)
        latency_ms = int((time.perf_counter() - started) * 1000)
        if response.status_code >= 400:
            return LocalRuntimeProbeResult(
                runtime_kind=runtime_kind,
                base_url=base_url,
                reachable=False,
                http_status=response.status_code,
                error=f"HTTP {response.status_code} from {url}",
                latency_ms=latency_ms,
                checked_at_utc=checked_at_utc,
            )
        try:
            models = _normalise_models(response.json(), keys=model_keys)
        except (ValueError, LocalRuntimeProbeError) as exc:
            logger.warning("Handled recoverable failure before fallback.", exc_info=True)
            return LocalRuntimeProbeResult(
                runtime_kind=runtime_kind,
                base_url=base_url,
                reachable=False,
                http_status=response.status_code,
                error=f"Malformed JSON from {url}: {exc}",
                latency_ms=latency_ms,
                checked_at_utc=checked_at_utc,
            )
        return LocalRuntimeProbeResult(
            runtime_kind=runtime_kind,
            base_url=base_url,
            reachable=True,
            discovered_models=models,
            http_status=response.status_code,
            latency_ms=latency_ms,
            checked_at_utc=checked_at_utc,
        )
    except httpx.HTTPError as exc:
        logger.warning("Handled recoverable failure before fallback.", exc_info=True)
        latency_ms = int((time.perf_counter() - started) * 1000)
        return LocalRuntimeProbeResult(
            runtime_kind=runtime_kind,
            base_url=base_url,
            reachable=False,
            error=f"{exc.__class__.__name__}: {exc}",
            latency_ms=latency_ms,
            checked_at_utc=checked_at_utc,
        )


def _probe_lmstudio(client: httpx.Client, base_url: str = _DEFAULT_LMSTUDIO_BASE) -> LocalRuntimeProbeResult:
    return _probe_json_endpoint(
        client,
        runtime_kind=LocalRuntimeKind.LMSTUDIO,
        base_url=base_url,
        path="/v1/models",
        model_keys=("data", "models"),
    )


def _probe_jan(client: httpx.Client, base_url: str = _DEFAULT_JAN_BASE) -> LocalRuntimeProbeResult:
    return _probe_json_endpoint(
        client,
        runtime_kind=LocalRuntimeKind.JAN,
        base_url=base_url,
        path="/v1/models",
        model_keys=("data", "models"),
    )


def _probe_openwebui(client: httpx.Client, base_url: str = _DEFAULT_OPENWEBUI_BASE) -> LocalRuntimeProbeResult:
    return _probe_json_endpoint(
        client,
        runtime_kind=LocalRuntimeKind.OPENWEBUI,
        base_url=base_url,
        path="/api/models",
        model_keys=("data", "models"),
    )


def _dispatch_probe(
    runtime_kind: LocalRuntimeKind,
    client: httpx.Client,
    *,
    base_urls: Mapping[LocalRuntimeKind, str] | None = None,
) -> LocalRuntimeProbeResult:
    bases = {
        LocalRuntimeKind.LMSTUDIO: _DEFAULT_LMSTUDIO_BASE,
        LocalRuntimeKind.JAN: _DEFAULT_JAN_BASE,
        LocalRuntimeKind.OPENWEBUI: _DEFAULT_OPENWEBUI_BASE,
    }
    if base_urls:
        bases.update(base_urls)
    if runtime_kind is LocalRuntimeKind.LMSTUDIO:
        return _probe_lmstudio(client, bases[runtime_kind])
    if runtime_kind is LocalRuntimeKind.JAN:
        return _probe_jan(client, bases[runtime_kind])
    if runtime_kind is LocalRuntimeKind.OPENWEBUI:
        return _probe_openwebui(client, bases[runtime_kind])
    raise LocalRuntimeProbeError(f"unsupported local runtime: {runtime_kind!r}")


def detect_system_resources() -> dict[str, int]:
    """Return conservative host resources used by hardware-fit checks.

    Returns:
        dict[str, int] value produced by detect_system_resources().
    """
    try:
        import psutil

        memory_gb = int(psutil.virtual_memory().total / (1024**3))
    except Exception:
        memory_gb = 0
    return {"memory_gb": max(memory_gb, 0)}


def _compute_hardware_fit(
    entry: ModelRegistryEntry,
    *,
    resources: Mapping[str, int] | None = None,
    select_target_fn: Callable[[RoutedInferenceRequest, dict[str, Any]], ComputeTarget] = select_target,
) -> HardwareFit:
    available_gb = int((resources or detect_system_resources()).get("memory_gb", 0))
    required_gb = int(entry.memory_requirements_gb)
    fits_memory = available_gb >= required_gb if available_gb > 0 else False
    selected_target = "unavailable"
    reason = "model fits available memory" if fits_memory else "model exceeds detected system memory"
    if entry.requires_cpu_offload:
        reason = "model requires CPU offload"
    try:
        target = select_target_fn(
            RoutedInferenceRequest(
                capability="general",
                prompt="local runtime onboarding",
                max_tokens=_HARDWARE_FIT_PROBE_MAX_TOKENS,
                lane=Lane.INTERACTIVE.value,
                latency_budget_s=30.0,
                caller_subsystem="workbench-onboarding",
            ),
            {
                "capabilities": {
                    "general": {
                        "targets": [
                            {
                                "compute": "cpu-tier" if entry.requires_cpu_offload else "local-runtime",
                                "model": entry.model_id,
                                "p95_ms_per_tok": 10,
                                "p95_per_request_ms": 0,
                                "quality": "standard",
                            }
                        ]
                    }
                }
            },
        )
        selected_target = target.compute
    except Exception as exc:
        reason = f"{reason}; scheduler target unavailable: {exc}"
    return HardwareFit(
        model_id=entry.model_id,
        fits=fits_memory or entry.requires_cpu_offload,
        required_memory_gb=required_gb,
        available_memory_gb=available_gb,
        requires_cpu_offload=bool(entry.requires_cpu_offload),
        selected_target=selected_target,
        reason=reason,
    )


def _compute_lane_readiness(scheduler: WorkbenchScheduler | None = None) -> dict[str, bool]:
    """Return lane readiness, failing closed if scheduler internals drift."""
    try:
        scheduler = scheduler or WorkbenchScheduler()
        lane_state = scheduler._lane_state
        readiness: dict[str, bool] = {}
        for lane in Lane:
            state = lane_state[lane]
            readiness[lane.name] = int(state.active_count) < int(state.capacity)
        return readiness
    except Exception:
        logger.warning("Handled recoverable failure before fallback.", exc_info=True)
        return {lane.name: False for lane in Lane}


def _collect_blockers(
    *,
    probes: tuple[LocalRuntimeProbeResult, ...],
    hardware_fit_by_model: Mapping[str, HardwareFit],
    scheduler_lanes_ready: Mapping[str, bool],
    deps_00_hook_present: bool,
    dry_run: bool = False,
) -> tuple[LocalRuntimeBlocker, ...]:
    blockers: list[LocalRuntimeBlocker] = []
    for probe in probes:
        if not probe.reachable:
            raw_error = probe.error or "runtime endpoint is unreachable"
            lowered = raw_error.lower()
            if "address already in use" in lowered or ("port" in lowered and "use" in lowered):
                kind = BlockerKind.PORT_COLLISION
                remediation = (
                    "Stop the process occupying the runtime port, or configure the runtime to use another port."
                )
            elif probe.http_status is not None:
                kind = BlockerKind.HTTP_ERROR
                remediation = "Open the runtime app and confirm its local API server is enabled and healthy."
            elif "json" in lowered or "malformed" in lowered:
                kind = BlockerKind.MALFORMED_RESPONSE
                remediation = "Upgrade or restart the runtime so its model endpoint returns OpenAI-compatible JSON."
            else:
                kind = BlockerKind.NETWORK_UNREACHABLE
                remediation = (
                    "Install or start the runtime, then verify its local OpenAI-compatible endpoint is listening."
                )
            blockers.append(
                LocalRuntimeBlocker(
                    kind=kind,
                    runtime_kind=probe.runtime_kind,
                    message=f"{probe.runtime_kind.value} probe failed: {raw_error}",
                    remediation=remediation,
                )
            )
        elif not probe.discovered_models:
            blockers.append(
                LocalRuntimeBlocker(
                    kind=BlockerKind.MODEL_DOWNLOAD_FAILURE,
                    runtime_kind=probe.runtime_kind,
                    message=f"{probe.runtime_kind.value} is running but reported no downloaded models",
                    remediation="Download at least one local model in the runtime and rerun onboarding.",
                )
            )
    blockers.extend(
        LocalRuntimeBlocker(
            kind=BlockerKind.HARDWARE_INSUFFICIENT,
            model_id=fit.model_id,
            message=f"{fit.model_id} needs {fit.required_memory_gb} GB but only {fit.available_memory_gb} GB was detected",
            remediation="Choose a smaller quantisation, enable CPU offload, or use a smaller local model.",
        )
        for fit in hardware_fit_by_model.values()
        if not fit.fits
    )
    for lane_name, ready in scheduler_lanes_ready.items():
        if not ready:
            blockers.append(
                LocalRuntimeBlocker(
                    kind=BlockerKind.SCHEDULER_LANE_NOT_READY,
                    message=f"workbench scheduler lane {lane_name} is not ready",
                    remediation="Wait for the active workload to finish or free scheduler capacity before running local inference.",
                )
            )
    if not deps_00_hook_present and not dry_run:
        blockers.append(
            LocalRuntimeBlocker(
                kind=BlockerKind.DEPS_00_HOOK_MISSING,
                message="configuration writeback hook is not registered",
                remediation=(
                    "Wait for Pack AS DEPS-00 to register the writeback hook, or run the runtime-onboarding API "
                    "in dry-run mode with body field dry_run=true."
                ),
            )
        )
    return tuple(blockers)
