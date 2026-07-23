"""Request-time capability detection gates."""

from __future__ import annotations

import importlib
import logging
import shutil
import threading
import time
from collections.abc import Callable
from datetime import datetime, timezone

import httpx

from vetinari.capabilities.registry import get_capability_registry
from vetinari.capabilities.types import (
    CapabilityHealthState,
    CapabilityKind,
    CapabilityNotInstalled,
    CapabilityProbeResult,
    DetectionRule,
    DetectionRuleKind,
)

logger = logging.getLogger(__name__)


_CALLABLE_PROBES: dict[str, Callable[[], bool]] = {}
_CALLABLE_PROBES_LOCK = threading.Lock()
_MAX_PROBE_TIMEOUT_SECONDS = 10.0


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _timeout(rule: DetectionRule) -> float:
    return max(0.1, min(float(rule.timeout_s), _MAX_PROBE_TIMEOUT_SECONDS))


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _probe_import(kind: CapabilityKind, rule: DetectionRule) -> CapabilityProbeResult:
    """Check whether a Python module is installed without executing it."""
    started = time.monotonic()
    try:
        found = importlib.util.find_spec(rule.target) is not None
    except Exception as exc:
        logger.warning("Handled recoverable failure before fallback.", exc_info=True)
        return CapabilityProbeResult(
            kind,
            False,
            CapabilityHealthState.BROKEN,
            _utc_now_iso(),
            f"module spec probe {rule.target!r} failed: {exc}",
            _elapsed_ms(started),
        )
    if not found:
        return CapabilityProbeResult(
            kind,
            False,
            CapabilityHealthState.BROKEN,
            _utc_now_iso(),
            f"module {rule.target!r} not found",
            _elapsed_ms(started),
        )
    return CapabilityProbeResult(kind, True, CapabilityHealthState.HEALTHY, _utc_now_iso(), None, _elapsed_ms(started))


def _probe_binary(kind: CapabilityKind, rule: DetectionRule) -> CapabilityProbeResult:
    """Find an executable on PATH."""
    started = time.monotonic()
    path = shutil.which(rule.target)
    if path:
        return CapabilityProbeResult(
            kind, True, CapabilityHealthState.HEALTHY, _utc_now_iso(), None, _elapsed_ms(started)
        )
    return CapabilityProbeResult(
        kind,
        False,
        CapabilityHealthState.BROKEN,
        _utc_now_iso(),
        f"binary {rule.target!r} not found on PATH",
        _elapsed_ms(started),
    )


def _probe_http(kind: CapabilityKind, rule: DetectionRule) -> CapabilityProbeResult:
    """GET a URL with a bounded timeout."""
    started = time.monotonic()
    try:
        response = httpx.get(rule.target, timeout=_timeout(rule))
    except httpx.HTTPError as exc:
        logger.warning("Handled recoverable failure before fallback.", exc_info=True)
        return CapabilityProbeResult(
            kind, False, CapabilityHealthState.BROKEN, _utc_now_iso(), str(exc), _elapsed_ms(started)
        )
    if 200 <= response.status_code < 300:
        return CapabilityProbeResult(
            kind, True, CapabilityHealthState.HEALTHY, _utc_now_iso(), None, _elapsed_ms(started)
        )
    health = CapabilityHealthState.DEGRADED if response.status_code >= 500 else CapabilityHealthState.BROKEN
    return CapabilityProbeResult(
        kind, False, health, _utc_now_iso(), f"HTTP {response.status_code}", _elapsed_ms(started)
    )


def _probe_callable(kind: CapabilityKind, rule: DetectionRule) -> CapabilityProbeResult:
    """Run a registered callable probe."""
    started = time.monotonic()
    probe = _CALLABLE_PROBES.get(rule.target)
    if probe is None:
        return CapabilityProbeResult(
            kind,
            False,
            CapabilityHealthState.BROKEN,
            _utc_now_iso(),
            f"callable probe {rule.target!r} is not registered",
            _elapsed_ms(started),
        )
    try:
        reachable = bool(probe())
    except Exception as exc:
        logger.warning("Handled recoverable failure before fallback.", exc_info=True)
        return CapabilityProbeResult(
            kind,
            False,
            CapabilityHealthState.BROKEN,
            _utc_now_iso(),
            f"callable probe {rule.target!r} failed: {exc}",
            _elapsed_ms(started),
        )
    return CapabilityProbeResult(
        kind,
        reachable,
        CapabilityHealthState.HEALTHY if reachable else CapabilityHealthState.BROKEN,
        _utc_now_iso(),
        None if reachable else f"callable probe {rule.target!r} returned false",
        _elapsed_ms(started),
    )


def register_callable_probe(name: str, probe: Callable[[], bool]) -> None:
    """Register or replace a callable capability probe.

    Args:
        name: Name used to identify the target object.
        probe: Probe value consumed by register_callable_probe().
    """
    with _CALLABLE_PROBES_LOCK:
        if name in _CALLABLE_PROBES and _CALLABLE_PROBES[name] is not probe:
            logger.warning("Re-registering callable probe %r", name)
        _CALLABLE_PROBES[name] = probe


def probe_capability(kind: CapabilityKind) -> CapabilityProbeResult:
    """Probe a capability and persist the resulting health state.

    Returns:
        CapabilityProbeResult value produced by probe_capability().
    """
    registry = get_capability_registry()
    metadata = registry.lookup(kind)
    dispatch = {
        DetectionRuleKind.IMPORT_PROBE: _probe_import,
        DetectionRuleKind.BINARY_PROBE: _probe_binary,
        DetectionRuleKind.HTTP_PROBE: _probe_http,
        DetectionRuleKind.CALLABLE_PROBE: _probe_callable,
    }
    result = dispatch[metadata.detection_rule.kind](kind, metadata.detection_rule)
    registry.record_health_probe(result)
    return result


def detect_missing_capability(kind: CapabilityKind, *, request_context: str) -> CapabilityProbeResult:
    """Return a healthy probe result or raise a metadata-rich missing-capability error.

    Returns:
        CapabilityProbeResult value produced by detect_missing_capability().

    Raises:
        Exception: Propagates validation or runtime failures from the underlying operation.
    """
    registry = get_capability_registry()
    metadata = registry.lookup(kind)
    if registry.is_available(kind):
        result = probe_capability(kind)
        if result.reachable and result.health_state is CapabilityHealthState.HEALTHY:
            return result
    else:
        probe_capability(kind)
    raise CapabilityNotInstalled(
        f"capability {kind.value!r} is required for {request_context!r} and is not installed",
        kind=kind,
        metadata=metadata,
    )


__all__ = ["detect_missing_capability", "probe_capability", "register_callable_probe"]
