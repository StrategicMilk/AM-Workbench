"""Read-only probe adapters for Workbench status health."""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from vetinari.workbench.hardware.probes import emit_surface_error_signal
from vetinari.workbench.metadata_spine import WorkbenchSpine, get_workbench_spine
from vetinari.workbench.status.contracts import (
    WorkbenchHealthDomain,
    WorkbenchHealthResult,
    WorkbenchHealthState,
    WorkbenchStatusConfig,
    WorkbenchStatusSeverity,
)

logger = logging.getLogger(__name__)


_DEFAULT_STATES: dict[WorkbenchHealthDomain, WorkbenchHealthState] = {
    WorkbenchHealthDomain.PROVIDERS: WorkbenchHealthState.DEGRADED,
    WorkbenchHealthDomain.MODELS: WorkbenchHealthState.CONFIGURED,
    WorkbenchHealthDomain.CHANNELS: WorkbenchHealthState.CONFIGURED,
    WorkbenchHealthDomain.MEMORY: WorkbenchHealthState.STALE,
    WorkbenchHealthDomain.TOOLS: WorkbenchHealthState.CONFIGURED,
    WorkbenchHealthDomain.MCP: WorkbenchHealthState.BUSY,
    WorkbenchHealthDomain.AGENT_SAFETY: WorkbenchHealthState.APPROVAL_REQUIRED,
    WorkbenchHealthDomain.CAPABILITY_PACKS: WorkbenchHealthState.CONFIGURED,
    WorkbenchHealthDomain.SCHEDULER_RESOURCES: WorkbenchHealthState.APPROVAL_REQUIRED,
    WorkbenchHealthDomain.ACTIVE_RUNS: WorkbenchHealthState.BUSY,
    WorkbenchHealthDomain.QUEUES: WorkbenchHealthState.BUSY,
    WorkbenchHealthDomain.LOGS_ERRORS: WorkbenchHealthState.BROKEN,
    WorkbenchHealthDomain.UPDATES: WorkbenchHealthState.STALE,
    WorkbenchHealthDomain.SETTINGS: WorkbenchHealthState.APPROVAL_REQUIRED,
    WorkbenchHealthDomain.SUPPORT_BUNDLE: WorkbenchHealthState.DEGRADED,
}


def probe_live_capability(
    domain: WorkbenchHealthDomain,
    probe: Callable[[], bool],
    *,
    config: WorkbenchStatusConfig,
    now: datetime | None = None,
) -> WorkbenchHealthResult:
    """Run a live capability probe and fail closed on false or exceptions.

    Args:
        domain: Health domain being probed.
        probe: Callable that returns True when the capability is healthy.
        config: Status configuration for severity, settings, and fix actions.
        now: Optional timestamp override for tests.

    Returns:
        Health result for the probed domain.
    """
    checked_at = _iso(now)
    try:
        healthy = bool(probe())
    except Exception as exc:
        logger.warning("Workbench live probe failed for %s: %s", domain.value, exc)
        return _closed_result(domain, WorkbenchHealthState.BROKEN, "live probe failed", config, checked_at)
    if not healthy:
        return _closed_result(domain, WorkbenchHealthState.BROKEN, "capability unavailable", config, checked_at)
    return WorkbenchHealthResult(
        domain=domain,
        key=f"{domain.value}.live",
        state=WorkbenchHealthState.CONFIGURED,
        severity=WorkbenchStatusSeverity.INFO,
        summary=f"{domain.value} live probe passed",
        evidence_refs=(f"status-live:{domain.value}",),
        checked_at_utc=checked_at,
        settings_target=config.settings_targets.get(domain),
        fix_action=config.fix_actions.get(domain),
    )


def build_default_probe_results(
    *,
    config: WorkbenchStatusConfig,
    now: datetime | None = None,
) -> tuple[WorkbenchHealthResult, ...]:
    """Return conservative fail-closed defaults for every configured domain.

    Returns:
        Newly constructed default probe results value.
    """
    checked_at = _iso(now)
    stale_after = _iso((now or datetime.now(UTC)) - timedelta(hours=1))
    results: list[WorkbenchHealthResult] = []
    for domain in config.required_domains:
        state = _DEFAULT_STATES.get(domain, WorkbenchHealthState.DEGRADED)
        results.append(
            WorkbenchHealthResult(
                domain=domain,
                key=f"{domain.value}.default",
                state=state,
                severity=_severity_for_state(state),
                summary=_default_summary(domain, state),
                evidence_refs=(f"status-probe:{domain.value}",),
                checked_at_utc=checked_at,
                settings_target=config.settings_targets.get(domain),
                fix_action=config.fix_actions.get(domain),
                stale_after_utc=stale_after if state is WorkbenchHealthState.STALE else None,
                stale_reason="last-known state requires refresh" if state is WorkbenchHealthState.STALE else None,
            )
        )
    return tuple(results)


def normalize_probe_results(
    dependency_snapshots: Mapping[str, Any] | None,
    *,
    config: WorkbenchStatusConfig,
    now: datetime | None = None,
) -> tuple[WorkbenchHealthResult, ...]:
    """Convert caller-provided dependency health into status rows.

        Missing, malformed, or unknown inputs fail closed rather than disappearing.

    Returns:
        Normalized probe results value.
    """
    if dependency_snapshots is None:
        return build_default_probe_results(config=config, now=now)
    checked_at = _iso(now)
    results: list[WorkbenchHealthResult] = []
    for domain in config.required_domains:
        raw = dependency_snapshots.get(domain.value) if isinstance(dependency_snapshots, Mapping) else None
        results.append(_coerce_domain_result(domain, raw, config=config, checked_at=checked_at))
    return tuple(results)


def build_metadata_spine_probe_snapshots(
    *,
    spine: WorkbenchSpine | None = None,
) -> dict[str, dict[str, object]]:
    """Build Workbench health inputs from the metadata spine read API.

    Returns:
        Dependency snapshot rows keyed by health domain.
    """
    try:
        resolved_spine = spine if spine is not None else get_workbench_spine()
        assets = resolved_spine.list_assets()
        runs = resolved_spine.list_runs()
        evals = resolved_spine.list_evals()
        proposals = resolved_spine.list_proposals()
        leases = resolved_spine.list_leases()
    except Exception:
        logger.warning(
            "Workbench metadata spine probe failed; status inputs failed closed",
            exc_info=True,
            extra={
                "action": "read_workbench_metadata_spine",
                "impact": "workbench status domains marked broken",
            },
        )
        return {
            domain.value: {
                "state": WorkbenchHealthState.BROKEN.value,
                "severity": WorkbenchStatusSeverity.ERROR.value,
                "summary": f"{domain.value} metadata spine read failed; status failed closed",
                "evidence_refs": [f"metadata-spine:error:{domain.value}"],
            }
            for domain in WorkbenchHealthDomain
        }

    return {
        WorkbenchHealthDomain.CAPABILITY_PACKS.value: _count_snapshot(
            domain=WorkbenchHealthDomain.CAPABILITY_PACKS,
            count=len(assets),
            configured_summary=f"metadata spine has {len(assets)} asset(s)",
            empty_summary="metadata spine has no asset records",
        ),
        WorkbenchHealthDomain.ACTIVE_RUNS.value: _count_snapshot(
            domain=WorkbenchHealthDomain.ACTIVE_RUNS,
            count=len(runs),
            configured_summary=f"metadata spine has {len(runs)} run(s)",
            empty_summary="metadata spine has no run records",
        ),
        WorkbenchHealthDomain.LOGS_ERRORS.value: _count_snapshot(
            domain=WorkbenchHealthDomain.LOGS_ERRORS,
            count=len(evals),
            configured_summary=f"metadata spine has {len(evals)} eval result(s)",
            empty_summary="metadata spine has no eval result records",
        ),
        WorkbenchHealthDomain.UPDATES.value: _count_snapshot(
            domain=WorkbenchHealthDomain.UPDATES,
            count=len(proposals),
            configured_summary=f"metadata spine has {len(proposals)} proposal(s)",
            empty_summary="metadata spine has no proposal records",
        ),
        WorkbenchHealthDomain.SCHEDULER_RESOURCES.value: _count_snapshot(
            domain=WorkbenchHealthDomain.SCHEDULER_RESOURCES,
            count=len(leases),
            configured_summary=f"metadata spine has {len(leases)} lease(s)",
            empty_summary="metadata spine has no lease records",
        ),
    }


def _coerce_domain_result(
    domain: WorkbenchHealthDomain,
    raw: Any,
    *,
    config: WorkbenchStatusConfig,
    checked_at: str,
) -> WorkbenchHealthResult:
    if raw is None:
        return _closed_result(domain, WorkbenchHealthState.DEGRADED, "state missing", config, checked_at)
    if not isinstance(raw, Mapping):
        return _closed_result(domain, WorkbenchHealthState.BROKEN, "state unreadable", config, checked_at)
    try:
        state = WorkbenchHealthState(str(raw.get("state", "")))
    except ValueError:
        logger.warning(
            "Workbench status probe value was unknown; status input failed closed",
            exc_info=True,
            extra={
                "action": "normalize_workbench_status_probe",
                "impact": "domain result marked broken",
            },
        )
        return _closed_result(domain, WorkbenchHealthState.BROKEN, "state unknown", config, checked_at)
    evidence = tuple(str(ref) for ref in raw.get("evidence_refs", ()) if str(ref).strip())
    return WorkbenchHealthResult(
        domain=domain,
        key=str(raw.get("key") or f"{domain.value}.provided"),
        state=state,
        severity=WorkbenchStatusSeverity(str(raw.get("severity") or _severity_for_state(state).value)),
        summary=str(raw.get("summary") or _default_summary(domain, state)),
        evidence_refs=evidence or (f"status-input:{domain.value}",),
        checked_at_utc=str(raw.get("checked_at_utc") or checked_at),
        settings_target=str(raw.get("settings_target") or config.settings_targets.get(domain) or ""),
        fix_action=str(raw.get("fix_action") or config.fix_actions.get(domain) or ""),
        assistant_visible=bool(raw.get("assistant_visible", True)),
        ui_visible=bool(raw.get("ui_visible", True)),
        stale_after_utc=raw.get("stale_after_utc") if state is WorkbenchHealthState.STALE else None,
        stale_reason=str(raw.get("stale_reason") or "state is stale") if state is WorkbenchHealthState.STALE else None,
        informational=bool(raw.get("informational", False)),
    )


def _closed_result(
    domain: WorkbenchHealthDomain,
    state: WorkbenchHealthState,
    reason: str,
    config: WorkbenchStatusConfig,
    checked_at: str,
) -> WorkbenchHealthResult:
    emit_surface_error_signal(
        surface_id=f"workbench-status:{domain.value}",
        error_kind=state.value,
        message=reason,
    )
    return WorkbenchHealthResult(
        domain=domain,
        key=f"{domain.value}.closed",
        state=state,
        severity=_severity_for_state(state),
        summary=f"{domain.value} {reason}; status failed closed",
        evidence_refs=(f"status-fail-closed:{domain.value}",),
        checked_at_utc=checked_at,
        settings_target=config.settings_targets.get(domain),
        fix_action=config.fix_actions.get(domain),
        stale_reason=reason if state is WorkbenchHealthState.STALE else None,
    )


def _default_summary(domain: WorkbenchHealthDomain, state: WorkbenchHealthState) -> str:
    if state is WorkbenchHealthState.CONFIGURED:
        return f"{domain.value} is configured"
    if state is WorkbenchHealthState.APPROVAL_REQUIRED:
        return f"{domain.value} requires approval before mutation"
    return f"{domain.value} is {state.value}"


def _severity_for_state(state: WorkbenchHealthState) -> WorkbenchStatusSeverity:
    if state is WorkbenchHealthState.CONFIGURED:
        return WorkbenchStatusSeverity.INFO
    if state in {WorkbenchHealthState.DEGRADED, WorkbenchHealthState.STALE, WorkbenchHealthState.BUSY}:
        return WorkbenchStatusSeverity.WARNING
    if state is WorkbenchHealthState.APPROVAL_REQUIRED:
        return WorkbenchStatusSeverity.BLOCKING
    return WorkbenchStatusSeverity.ERROR


def _count_snapshot(
    *,
    domain: WorkbenchHealthDomain,
    count: int,
    configured_summary: str,
    empty_summary: str,
) -> dict[str, object]:
    configured = count > 0
    return {
        "state": WorkbenchHealthState.CONFIGURED.value if configured else WorkbenchHealthState.DEGRADED.value,
        "severity": WorkbenchStatusSeverity.INFO.value if configured else WorkbenchStatusSeverity.WARNING.value,
        "summary": configured_summary if configured else f"{empty_summary}; status failed closed",
        "evidence_refs": [f"metadata-spine:{domain.value}:count={count}"],
    }


def _iso(value: datetime | None) -> str:
    return (value or datetime.now(UTC)).astimezone(UTC).isoformat().replace("+00:00", "Z")


__all__ = [
    "build_default_probe_results",
    "build_metadata_spine_probe_snapshots",
    "normalize_probe_results",
    "probe_live_capability",
]
