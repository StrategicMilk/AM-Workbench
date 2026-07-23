"""Read-only adapters from harness and watcher decisions into monitoring."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from vetinari.workbench.agents.harness.contracts import (
    BLOCKER_RECEIPT_MISSING,
    BLOCKER_TOOL_NOT_ALLOWED,
    BLOCKER_WORKSPACE_ESCAPE,
    AgentRunAdmission,
    AgentRunRequest,
)
from vetinari.workbench.monitoring.router import (
    MonitoringAlertRouter,
    MonitoringRouteDestination,
    MonitoringRouteResult,
)
from vetinari.workbench.monitoring.signals import (
    MonitoringSignal,
    MonitoringSignalKind,
    MonitoringSignalSeverity,
)

from .events import WatcherAction, WatcherDecision, WatcherObservation, WatcherTransitionKind

logger = logging.getLogger(__name__)


def observations_from_harness_admission(
    request: AgentRunRequest,
    admission: AgentRunAdmission,
    *,
    observed_at_utc: str | None = None,
) -> tuple[WatcherObservation, ...]:
    """Convert a harness admission result into watcher observations.

    Args:
        request: Request object sent through the operation.
        admission: Admission value consumed by observations_from_harness_admission().
        observed_at_utc: Observed at utc value consumed by observations_from_harness_admission().

    Returns:
        tuple[WatcherObservation, ...] value produced by observations_from_harness_admission().

    Raises:
        Exception: Propagates validation or runtime failures from the underlying operation.
    """
    if not isinstance(request, AgentRunRequest):
        raise TypeError(f"request must be AgentRunRequest, got {type(request).__name__!r}")
    if not isinstance(admission, AgentRunAdmission):
        raise TypeError(f"admission must be AgentRunAdmission, got {type(admission).__name__!r}")
    observed_at = observed_at_utc or _utc_now_iso()
    observations = _blocked_harness_observations(request, admission, observed_at)
    if observations:
        return observations
    return (
        _observation(
            request,
            WatcherTransitionKind.PERMISSION,
            "harness admitted run with declared permissions",
            evidence_refs=(f"harness:admitted:{admission.run_id}",),
            observed_at_utc=observed_at,
            authority_refs=(request.sandbox.authority_ref,),
        ),
    )


def _blocked_harness_observations(
    request: AgentRunRequest,
    admission: AgentRunAdmission,
    observed_at: str,
) -> tuple[WatcherObservation, ...]:
    observations: list[WatcherObservation] = []
    blockers = set(admission.blockers)
    if BLOCKER_TOOL_NOT_ALLOWED in blockers:
        observations.extend(
            (
                _observation(
                    request,
                    WatcherTransitionKind.TOOL,
                    "harness blocked a requested tool outside the sandbox permission set",
                    evidence_refs=("harness:blocker:tool_not_allowed", f"harness:run:{admission.run_id}"),
                    observed_at_utc=observed_at,
                    tool_name=",".join(request.requested_tools),
                ),
                _observation(
                    request,
                    WatcherTransitionKind.PERMISSION,
                    "harness found missing authority for requested tools",
                    evidence_refs=("harness:blocker:tool_not_allowed", f"harness:run:{admission.run_id}"),
                    observed_at_utc=observed_at,
                    tool_name=",".join(request.requested_tools),
                ),
            ),
        )
    if BLOCKER_WORKSPACE_ESCAPE in blockers:
        observations.append(
            _observation(
                request,
                WatcherTransitionKind.FILE,
                "harness blocked workspace path escape",
                evidence_refs=("harness:blocker:workspace_escape", f"harness:run:{admission.run_id}"),
                observed_at_utc=observed_at,
                workspace_path=request.workspace_path,
            ),
        )
    if BLOCKER_RECEIPT_MISSING in blockers:
        observations.append(
            _observation(
                request,
                WatcherTransitionKind.SIDE_EFFECT,
                "harness blocked missing receipt side effect",
                evidence_refs=("harness:blocker:receipt_missing", f"harness:run:{admission.run_id}"),
                observed_at_utc=observed_at,
                expected_side_effect_refs=request.sandbox.receipt_requirements,
                observed_side_effect_refs=request.receipt_refs,
            ),
        )
    return tuple(observations)


def monitoring_signal_from_watcher_decision(
    decision: WatcherDecision,
    *,
    project_id: str = "default",
    endpoint_id: str = "agent-watcher-runtime",
    asset_id: str = "agent-watcher-runtime",
    captured_at_utc: str | None = None,
) -> MonitoringSignal:
    """Represent a high-risk watcher decision as a production monitoring anomaly.

    Returns:
        MonitoringSignal value produced by monitoring_signal_from_watcher_decision().

    Raises:
        Exception: Propagates validation or runtime failures from the underlying operation.
    """
    if not isinstance(decision, WatcherDecision):
        raise TypeError(f"decision must be WatcherDecision, got {type(decision).__name__!r}")
    evidence_refs = (decision.decision_id, *decision.evidence_refs)
    severity = _severity_for(decision.action)
    return MonitoringSignal(
        signal_id=f"watcher-signal-{decision.decision_id}",
        kind=MonitoringSignalKind.AGENT_STATE_ANOMALY,
        project_id=project_id,
        run_id=decision.run_id or "unknown-run",
        endpoint_id=endpoint_id,
        asset_id=asset_id,
        severity=severity,
        score=1.0 if severity in {MonitoringSignalSeverity.ERROR, MonitoringSignalSeverity.CRITICAL} else 0.1,
        threshold=0.5,
        evidence_refs=evidence_refs,
        captured_at_utc=captured_at_utc or _utc_now_iso(),
        routing_hint=decision.action.value,
    )


def route_watcher_monitoring_signal(
    signal: MonitoringSignal,
    router: MonitoringAlertRouter,
) -> MonitoringRouteResult:
    """Route a watcher signal, converting dependency exceptions into degraded results.

    Args:
        signal: Signal value consumed by route_watcher_monitoring_signal().
        router: Router value consumed by route_watcher_monitoring_signal().

    Returns:
        Outcome produced by route_watcher_monitoring_signal().
    """
    try:
        return router.route(signal)
    except Exception as exc:
        logger.warning("Handled recoverable failure before fallback.", exc_info=True)
        return MonitoringRouteResult(
            signal_id=signal.signal_id,
            destination=MonitoringRouteDestination.NO_OP_DEGRADED,
            passed=False,
            degraded=True,
            artifact_id="",
            blockers=(f"{type(exc).__name__}: {exc}",),
            evidence_refs=signal.evidence_refs,
        )


def _observation(
    request: AgentRunRequest,
    transition_kind: WatcherTransitionKind,
    summary: str,
    *,
    evidence_refs: tuple[str, ...],
    observed_at_utc: str,
    tool_name: str = "",
    workspace_path: str = "",
    expected_side_effect_refs: tuple[str, ...] = (),
    observed_side_effect_refs: tuple[str, ...] = (),
    authority_refs: tuple[str, ...] = (),
) -> WatcherObservation:
    return WatcherObservation(
        observation_id=f"watcher-observation-{request.run_id}-{transition_kind.value}",
        run_id=request.run_id,
        actor_id=request.template_id,
        transition_kind=transition_kind,
        evidence_refs=evidence_refs,
        observed_at_utc=observed_at_utc,
        summary=summary,
        tool_name=tool_name,
        workspace_path=workspace_path,
        expected_side_effect_refs=expected_side_effect_refs,
        observed_side_effect_refs=observed_side_effect_refs,
        authority_refs=authority_refs,
    )


def _severity_for(action: WatcherAction) -> MonitoringSignalSeverity:
    return {
        WatcherAction.OBSERVE: MonitoringSignalSeverity.INFO,
        WatcherAction.PAUSE: MonitoringSignalSeverity.WARNING,
        WatcherAction.ESCALATE: MonitoringSignalSeverity.ERROR,
        WatcherAction.REQUIRE_APPROVAL: MonitoringSignalSeverity.ERROR,
        WatcherAction.TERMINATE: MonitoringSignalSeverity.CRITICAL,
    }[action]


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


__all__ = [
    "monitoring_signal_from_watcher_decision",
    "observations_from_harness_admission",
    "route_watcher_monitoring_signal",
]
