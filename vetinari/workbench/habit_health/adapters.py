"""Read-only adapters from dependency systems into habit-health signals."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from vetinari.workbench.habit_health.contracts import HabitHealthScope, HabitHealthSignal, HabitHealthSignalKind
from vetinari.workbench.habit_health.privacy import HabitHealthScopePolicy, HabitHealthUse, evaluate_habit_health_scope
from vetinari.workbench.memory_scopes.runtime import MemoryScopePolicy
from vetinari.workbench.security_primitives import scrub_payload


def habit_signal_from_run_snapshot(
    snapshot: Any,
    policy: HabitHealthScopePolicy | None = None,
    *,
    downstream_use: HabitHealthUse = HabitHealthUse.SCHEDULING,
) -> HabitHealthSignal:
    """Map a run snapshot into an agent-rhythm signal without mutating it.

    Args:
        snapshot: Snapshot value consumed by habit_signal_from_run_snapshot().
        policy: Policy value consumed by habit_signal_from_run_snapshot().
        downstream_use: Downstream use value consumed by habit_signal_from_run_snapshot().

    Returns:
        HabitHealthSignal value produced by habit_signal_from_run_snapshot().
    """
    payload = _payload(snapshot)
    source_context = f"run:{payload.get('run_id', '')}".strip(":")
    return _signal(
        policy,
        downstream_use,
        kind=HabitHealthSignalKind.AGENT_RHYTHM,
        scope=HabitHealthScope.AGENT_RUN_TELEMETRY,
        source_context=source_context,
        payload=payload,
    )


def habit_signal_from_watcher_decision(
    decision: Any,
    policy: HabitHealthScopePolicy | None = None,
    *,
    downstream_use: HabitHealthUse = HabitHealthUse.SCHEDULING,
) -> HabitHealthSignal:
    """Map watcher decisions into reviewable rhythm signals.

    Args:
        decision: Decision value consumed by habit_signal_from_watcher_decision().
        policy: Policy value consumed by habit_signal_from_watcher_decision().
        downstream_use: Downstream use value consumed by habit_signal_from_watcher_decision().

    Returns:
        HabitHealthSignal value produced by habit_signal_from_watcher_decision().
    """
    payload = _payload(decision)
    source_context = f"watcher:{payload.get('decision_id') or payload.get('observation_id', '')}".strip(":")
    return _signal(
        policy,
        downstream_use,
        kind=HabitHealthSignalKind.AGENT_RHYTHM,
        scope=HabitHealthScope.AGENT_RUN_TELEMETRY,
        source_context=source_context,
        payload=payload,
    )


def habit_signal_from_resource_lease(
    lease: Any,
    policy: HabitHealthScopePolicy | None = None,
    *,
    downstream_use: HabitHealthUse = HabitHealthUse.RESOURCE_PLANNING,
) -> HabitHealthSignal:
    """Map resource leases into review suggestions only.

    Args:
        lease: Lease value consumed by habit_signal_from_resource_lease().
        policy: Policy value consumed by habit_signal_from_resource_lease().
        downstream_use: Downstream use value consumed by habit_signal_from_resource_lease().

    Returns:
        HabitHealthSignal value produced by habit_signal_from_resource_lease().
    """
    payload = _payload(lease)
    if payload.get("status") in {"denied", "approval_required"}:
        payload = payload | {"review_suggestion": "resource-state-needs-user-review"}
    return _signal(
        policy,
        downstream_use,
        kind=HabitHealthSignalKind.AGENT_RHYTHM,
        scope=HabitHealthScope.PROJECT_TELEMETRY,
        source_context=f"resource:{payload.get('lease_id', '')}".strip(":"),
        payload=payload,
    )


def habit_signal_from_user_input_signal(
    signal: Any,
    policy: HabitHealthScopePolicy | None = None,
    *,
    downstream_use: HabitHealthUse = HabitHealthUse.PERSONALIZATION,
) -> HabitHealthSignal:
    """Map user-observability records into consent-scoped habit signals.

    Args:
        signal: Signal value consumed by habit_signal_from_user_input_signal().
        policy: Policy value consumed by habit_signal_from_user_input_signal().
        downstream_use: Downstream use value consumed by habit_signal_from_user_input_signal().

    Returns:
        HabitHealthSignal value produced by habit_signal_from_user_input_signal().
    """
    payload = _payload(signal)
    source_context = f"user-input:{payload.get('signal_id', '')}".strip(":")
    return _signal(
        policy,
        downstream_use,
        kind=HabitHealthSignalKind.ENERGY_FOCUS,
        scope=HabitHealthScope.PERSONAL_WELLNESS,
        source_context=source_context,
        payload=payload,
    )


def memory_scope_for_habit_signal(
    signal: HabitHealthSignal,
    memory_policy: MemoryScopePolicy | None = None,
    policy: HabitHealthScopePolicy | None = None,
) -> HabitHealthSignal:
    """Attach memory scope only when both habit and memory policy are explicit.

    Args:
        signal: Signal value consumed by memory_scope_for_habit_signal().
        memory_policy: Memory policy value consumed by memory_scope_for_habit_signal().
        policy: Policy value consumed by memory_scope_for_habit_signal().

    Returns:
        HabitHealthSignal value produced by memory_scope_for_habit_signal().
    """
    if memory_policy is None:
        return _replace_signal(signal, allowed=False, reasons=("memory-policy-missing",))
    verdict = evaluate_habit_health_scope(
        policy,
        HabitHealthUse.MEMORY_CONTEXT,
        requested_scope=HabitHealthScope.SENSITIVE_MEMORY_CONTEXT,
        source_context=signal.source_context,
        consent_refs=signal.consent_refs,
        provenance_ref=signal.provenance_ref,
    )
    if not verdict.allowed:
        return _replace_signal(signal, allowed=False, reasons=verdict.reasons)
    payload = signal.payload | {
        "memory_scope": memory_policy.scope.value,
        "memory_review_visible": memory_policy.review_visible,
    }
    return _replace_signal(signal, allowed=True, reasons=("allowed",), payload=payload)


def _signal(
    policy: HabitHealthScopePolicy | None,
    downstream_use: HabitHealthUse,
    *,
    kind: HabitHealthSignalKind,
    scope: HabitHealthScope,
    source_context: str,
    payload: dict[str, Any],
) -> HabitHealthSignal:
    verdict = evaluate_habit_health_scope(
        policy,
        downstream_use,
        requested_scope=scope,
        source_context=source_context,
        consent_refs=policy.consent_refs if policy else (),
        provenance_ref=policy.provenance_ref if policy else "",
    )
    return HabitHealthSignal(
        signal_id=f"habit-signal-{uuid4().hex[:12]}",
        user_id=policy.user_id if policy else "",
        signal_kind=kind,
        scope=scope,
        source_context=source_context,
        consent_refs=policy.consent_refs if policy else (),
        provenance_ref=policy.provenance_ref if policy else "",
        downstream_use=downstream_use.value,
        allowed=verdict.allowed,
        reasons=verdict.reasons,
        payload=payload,
    )


def _replace_signal(
    signal: HabitHealthSignal,
    *,
    allowed: bool,
    reasons: tuple[str, ...],
    payload: dict[str, Any] | None = None,
) -> HabitHealthSignal:
    return HabitHealthSignal(
        signal_id=signal.signal_id,
        user_id=signal.user_id,
        signal_kind=signal.signal_kind,
        scope=signal.scope,
        source_context=signal.source_context,
        consent_refs=signal.consent_refs,
        provenance_ref=signal.provenance_ref,
        downstream_use=signal.downstream_use,
        allowed=allowed,
        reasons=reasons,
        payload=payload if payload is not None else signal.payload,
    )


def _payload(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if hasattr(value, "to_dict"):
        return dict(value.to_dict())
    if hasattr(value, "__dict__"):
        return scrub_payload(dict(value.__dict__))
    return {"value": str(value)}


__all__ = [
    "habit_signal_from_resource_lease",
    "habit_signal_from_run_snapshot",
    "habit_signal_from_user_input_signal",
    "habit_signal_from_watcher_decision",
    "memory_scope_for_habit_signal",
]
