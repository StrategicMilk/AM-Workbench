"""Resource cockpit shown after governor and concurrency lease decisions."""

from __future__ import annotations

import logging
import math
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass
from enum import Enum
from itertools import starmap
from typing import Any

from vetinari.api.responses import json_safe as _json_safe
from vetinari.workbench.approval_diff import ApprovalDiffTarget, DiffDimension
from vetinari.workbench.private_ai_appliance import (
    PrivateAIApplianceConfigError,
    RuntimeCockpit,
)
from vetinari.workbench.resource_cockpit.cost_calculator import ResourceAccountingError
from vetinari.workbench.resources.cockpit_helpers import (
    RESOURCE_COCKPIT_PUBLIC_EXPORTS,
    _enum_value,
    _now_utc,
    _require_text,
    _require_text_tuple,
)
from vetinari.workbench.resources.cockpit_helpers import (
    scheduler_machine_profile_provider as _scheduler_machine_profile_provider,
)
from vetinari.workbench.resources.concurrency_profiles import (
    ActiveUserConcurrencyProfile,
    ConcurrencyAction,
    ConcurrencyDecision,
    ConcurrencyProfileError,
)
from vetinari.workbench.resources.governor import (
    MachineProfile,
    ProsumerResourceGovernor,
    ResourceGovernorError,
    ResourceLease,
    ResourceLeaseStatus,
    WorkloadEnvelope,
)

logger = logging.getLogger(__name__)


EXPECTED_WAIT_BUCKETS: tuple[str, ...] = ("under-1m", "1-5m", "5-15m", "over-15m", "unknown")
SAFE_ACTION_IDS: frozenset[str] = frozenset({
    "pin",
    "unpin",
    "pause",
    "resume",
    "cancel",
    "adjust_hot_budget",
    "adjust_training_window",
    "adjust_interactive_reserve",
    "adjust_specialist_placement",
})
_ALLOWED_POLICY_TARGETS = frozenset({
    ApprovalDiffTarget.ROUTE_POLICY_CHANGE,
    ApprovalDiffTarget.MODEL_DEFAULT,
    ApprovalDiffTarget.TRAINING_RECIPE_ACTIVATION,
    ApprovalDiffTarget.AUTOMATION_PROMOTION,
})


@dataclass(frozen=True, slots=True)
class LeaseSummary:
    """Operator-facing row for a decided resource lease."""

    lease_id: str
    workload_id: str
    lane: str
    workload_kind: str
    model_id: str
    status: str
    placement: str
    residency_action: str
    reasons: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    decided_at_utc: str

    def __post_init__(self) -> None:
        for field_name in (
            "lease_id",
            "workload_id",
            "lane",
            "workload_kind",
            "model_id",
            "status",
            "placement",
            "residency_action",
            "decided_at_utc",
        ):
            _require_text(getattr(self, field_name), field_name)
        _require_text_tuple(self.reasons, "reasons")
        _require_text_tuple(self.evidence_ids, "evidence_ids")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"LeaseSummary(lease_id={self.lease_id!r}, workload_id={self.workload_id!r}, lane={self.lane!r})"


@dataclass(frozen=True, slots=True)
class QueuedJobSummary:
    """Operator-facing row for queued concurrency work."""

    workload_id: str
    lane: str
    workload_kind: str
    model_id: str
    reason: str
    expected_wait_label: str
    over_cap_action: str

    def __post_init__(self) -> None:
        for field_name in ("workload_id", "lane", "workload_kind", "model_id", "reason", "over_cap_action"):
            _require_text(getattr(self, field_name), field_name)
        if self.expected_wait_label not in EXPECTED_WAIT_BUCKETS:
            raise ValueError("expected_wait_label must be one of EXPECTED_WAIT_BUCKETS")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"QueuedJobSummary(workload_id={self.workload_id!r}, lane={self.lane!r}, workload_kind={self.workload_kind!r})"


@dataclass(frozen=True, slots=True)
class SafeActionRow:
    """Operator-safe action justified by the current cockpit snapshot."""

    action_id: str
    target_ref: str
    label: str
    requires_approval: bool
    reason: str

    def __post_init__(self) -> None:
        if self.action_id not in SAFE_ACTION_IDS:
            raise ValueError("action_id must be one of SAFE_ACTION_IDS")
        _require_text(self.target_ref, "target_ref")
        _require_text(self.label, "label")
        _require_text(self.reason, "reason")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"SafeActionRow(action_id={self.action_id!r}, target_ref={self.target_ref!r}, label={self.label!r})"


@dataclass(frozen=True, slots=True)
class PolicyProposal:
    """Policy tuning candidate that must pass through approval diff review."""

    proposal_id: str
    target: ApprovalDiffTarget
    dimension_changes: Mapping[str, Mapping[str, str]]
    rollback_target_ref: str
    evidence_ids: tuple[str, ...]
    confidence: float
    created_at_utc: str

    def __post_init__(self) -> None:
        _require_text(self.proposal_id, "proposal_id")
        _require_text(self.rollback_target_ref, "rollback_target_ref")
        _require_text(self.created_at_utc, "created_at_utc")
        try:
            raw_target = self.target.value if isinstance(self.target, Enum) else self.target
            target = self.target if isinstance(self.target, ApprovalDiffTarget) else ApprovalDiffTarget(raw_target)
        except ValueError as exc:
            raise ValueError("target must be ApprovalDiffTarget") from exc
        object.__setattr__(self, "target", target)
        if target not in _ALLOWED_POLICY_TARGETS:
            raise ValueError("target must be one of the resource-cockpit policy targets")
        _require_text_tuple(self.evidence_ids, "evidence_ids")
        if not math.isfinite(self.confidence) or not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence must be between 0 and 1")
        if not self.dimension_changes:
            raise ValueError("dimension_changes must be non-empty")
        normalized: dict[str, dict[str, str]] = {}
        for dimension, change in self.dimension_changes.items():
            dimension_key = DiffDimension(str(dimension)).value
            if not isinstance(change, Mapping):
                raise ValueError("dimension_changes values must be mappings")
            before = str(change.get("before", "")).strip()
            after = str(change.get("after", "")).strip()
            rationale = str(change.get("rationale", "")).strip()
            if not before or not after or not rationale:
                raise ValueError("dimension_changes entries require before, after, and rationale")
            normalized[dimension_key] = {"before": before, "after": after, "rationale": rationale}
        object.__setattr__(self, "dimension_changes", normalized)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe policy proposal."""
        return _json_safe(self)

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"PolicyProposal(proposal_id={self.proposal_id!r}, target={self.target!r}, dimension_changes={self.dimension_changes!r})"


@dataclass(frozen=True, slots=True)
class ResourceCockpitSnapshot:
    """Top-level JSON-safe cockpit payload."""

    generated_at_utc: str
    overall_status: str
    machine_profile: dict[str, Any]
    active_leases: tuple[LeaseSummary, ...]
    queued_jobs: tuple[QueuedJobSummary, ...]
    safe_actions: tuple[SafeActionRow, ...]
    policy_proposals: tuple[PolicyProposal, ...]
    runtime_appliance: Mapping[str, Any]
    resource_accounting: Mapping[str, Any]
    concurrency_profile_id: str | None
    degradation_reasons: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_text(self.generated_at_utc, "generated_at_utc")
        if self.overall_status not in {"ready", "approval_required", "degraded", "unknown"}:
            raise ValueError("overall_status must be ready, approval_required, degraded, or unknown")
        if not isinstance(self.machine_profile, dict):
            raise ValueError("machine_profile must be a dict")
        if not isinstance(self.runtime_appliance, Mapping):
            raise ValueError("runtime_appliance must be a mapping")
        if not isinstance(self.resource_accounting, Mapping):
            raise ValueError("resource_accounting must be a mapping")
        for item in self.active_leases:
            if not isinstance(item, LeaseSummary):
                raise ValueError("active_leases must contain LeaseSummary")
        for item in self.queued_jobs:
            if not isinstance(item, QueuedJobSummary):
                raise ValueError("queued_jobs must contain QueuedJobSummary")
        for item in self.safe_actions:
            if not isinstance(item, SafeActionRow):
                raise ValueError("safe_actions must contain SafeActionRow")
        for item in self.policy_proposals:
            if not isinstance(item, PolicyProposal):
                raise ValueError("policy_proposals must contain PolicyProposal")

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe snapshot dictionary."""
        return _json_safe(self)

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"ResourceCockpitSnapshot(generated_at_utc={self.generated_at_utc!r}, overall_status={self.overall_status!r}, machine_profile={self.machine_profile!r})"


class ResourceCockpit:
    """Compose already-loaded resource state into a fail-closed cockpit."""

    def __init__(
        self,
        *,
        governor: ProsumerResourceGovernor | None,
        concurrency_profile: ActiveUserConcurrencyProfile | None,
        runtime_cockpit: RuntimeCockpit | None,
        machine_profile_provider: Callable[[], MachineProfile | None] | None,
        active_leases_provider: Callable[[], Iterable[tuple[ResourceLease, WorkloadEnvelope]]] | None,
        queued_jobs_provider: Callable[[], Iterable[ConcurrencyDecision]] | None,
        policy_proposal_provider: Callable[[], Iterable[PolicyProposal]] | None,
        resource_accounting_provider: Callable[[], Mapping[str, Any]] | None = None,
    ) -> None:
        self._governor = governor
        self._concurrency_profile = concurrency_profile
        self._runtime_cockpit = runtime_cockpit
        self._machine_profile_provider = machine_profile_provider
        self._active_leases_provider = active_leases_provider
        self._queued_jobs_provider = queued_jobs_provider
        self._policy_proposal_provider = policy_proposal_provider
        self._resource_accounting_provider = resource_accounting_provider

    def snapshot(self) -> ResourceCockpitSnapshot:
        """Return the current cockpit snapshot, degrading instead of raising.

        Returns:
            ResourceCockpitSnapshot value produced by snapshot().
        """
        degradation_reasons: list[str] = []
        machine_profile = self._machine_profile(degradation_reasons)
        runtime_appliance = self._runtime_appliance(degradation_reasons)
        lease_pairs = self._lease_pairs(degradation_reasons)
        active_leases = self._active_leases_from_pairs(lease_pairs)
        queued_jobs = self._queued_jobs(degradation_reasons)
        policy_proposals = self._policy_proposals(degradation_reasons)
        resource_accounting = self._resource_accounting(degradation_reasons)
        safe_actions = tuple(
            action for lease, workload in lease_pairs for action in safe_actions_for_lease(lease, workload)
        )

        if self._governor is None:
            degradation_reasons.append("resource-governor-unavailable")
        if self._concurrency_profile is None:
            degradation_reasons.append("concurrency-profile-missing")

        overall_status = _overall_status(
            degradation_reasons=tuple(dict.fromkeys(degradation_reasons)),
            active_leases=active_leases,
            queued_jobs=queued_jobs,
            machine_profile_missing=machine_profile.get("reason") == "machine-profile-missing",
            runtime_missing=runtime_appliance.get("reason") == "runtime-cockpit-unavailable",
        )
        return ResourceCockpitSnapshot(
            generated_at_utc=_now_utc(),
            overall_status=overall_status,
            machine_profile=machine_profile,
            active_leases=active_leases,
            queued_jobs=queued_jobs,
            safe_actions=safe_actions,
            policy_proposals=policy_proposals,
            runtime_appliance=runtime_appliance,
            resource_accounting=resource_accounting,
            concurrency_profile_id=self._concurrency_profile.profile_id if self._concurrency_profile else None,
            degradation_reasons=tuple(dict.fromkeys(degradation_reasons)),
        )

    def _machine_profile(self, degradation_reasons: list[str]) -> dict[str, Any]:
        if self._machine_profile_provider is None:
            degradation_reasons.append("machine-profile-missing")
            return {"status": "unknown", "reason": "machine-profile-missing"}
        try:
            profile = self._machine_profile_provider()
        except (ResourceGovernorError, ValueError) as exc:
            logger.warning("machine profile unavailable; degrading resource cockpit: %s", exc)
            degradation_reasons.append("machine-profile-unavailable")
            return {"status": "unknown", "reason": "machine-profile-unavailable"}
        if profile is None:
            degradation_reasons.append("machine-profile-missing")
            return {"status": "unknown", "reason": "machine-profile-missing"}
        return _json_safe(asdict(profile))

    def _runtime_appliance(self, degradation_reasons: list[str]) -> Mapping[str, Any]:
        if self._runtime_cockpit is None:
            degradation_reasons.append("runtime-cockpit-unavailable")
            return {"status": "unknown", "reason": "runtime-cockpit-unavailable"}
        try:
            return self._runtime_cockpit.snapshot().to_dict()
        except (PrivateAIApplianceConfigError, ValueError) as exc:
            logger.warning("runtime cockpit snapshot failed; degrading resource cockpit: %s", exc)
            degradation_reasons.append("runtime-cockpit-unavailable")
            return {"status": "unknown", "reason": "runtime-cockpit-unavailable"}

    def _lease_pairs(self, degradation_reasons: list[str]) -> tuple[tuple[ResourceLease, WorkloadEnvelope], ...]:
        if self._active_leases_provider is None:
            return ()
        try:
            return tuple(self._active_leases_provider())
        except (ResourceGovernorError, ValueError) as exc:
            logger.warning("active lease provider failed; degrading resource cockpit: %s", exc)
            degradation_reasons.append("active-leases-unavailable")
            return ()

    def _active_leases(self, degradation_reasons: list[str]) -> tuple[LeaseSummary, ...]:
        return self._active_leases_from_pairs(self._lease_pairs(degradation_reasons))

    @staticmethod
    def _active_leases_from_pairs(
        lease_pairs: tuple[tuple[ResourceLease, WorkloadEnvelope], ...],
    ) -> tuple[LeaseSummary, ...]:
        return tuple(starmap(_lease_summary, lease_pairs))

    def _queued_jobs(self, degradation_reasons: list[str]) -> tuple[QueuedJobSummary, ...]:
        if self._queued_jobs_provider is None:
            return ()
        try:
            return tuple(_queued_job_summary(decision) for decision in self._queued_jobs_provider())
        except (ConcurrencyProfileError, ValueError) as exc:
            logger.warning("queued job provider failed; degrading resource cockpit: %s", exc)
            degradation_reasons.append("queued-jobs-unavailable")
            return ()

    def _policy_proposals(self, degradation_reasons: list[str]) -> tuple[PolicyProposal, ...]:
        if self._policy_proposal_provider is None:
            return ()
        try:
            return tuple(self._policy_proposal_provider())
        except ValueError as exc:
            logger.warning("policy proposal provider failed; degrading resource cockpit: %s", exc)
            degradation_reasons.append("policy-proposals-unavailable")
            return ()

    def _resource_accounting(self, degradation_reasons: list[str]) -> Mapping[str, Any]:
        if self._resource_accounting_provider is None:
            return {"schema_version": "1.0", "pricing_configured": False, "active_lease_count": 0}
        try:
            return self._resource_accounting_provider()
        except ResourceAccountingError as exc:
            logger.warning("resource accounting unavailable; degrading resource cockpit: %s", exc)
            degradation_reasons.append("resource-accounting-unavailable")
            return {"schema_version": "1.0", "pricing_configured": False, "reason": "resource-accounting-unavailable"}


def build_policy_proposal(
    *,
    proposal_id: str,
    target: ApprovalDiffTarget | str,
    dimension_changes: Mapping[str, Mapping[str, str]],
    rollback_target_ref: str,
    resource_receipts: Iterable[Mapping[str, Any]],
    confidence: float,
    created_at_utc: str | None = None,
) -> PolicyProposal:
    """Build a policy proposal from resource-receipt-shaped evidence dicts.

    Returns:
        Newly constructed policy proposal value.
    """
    evidence_ids = tuple(
        str(receipt.get("evidence_id") or receipt.get("receipt_id") or "").strip() for receipt in resource_receipts
    )
    evidence_ids = tuple(item for item in evidence_ids if item)
    return PolicyProposal(
        proposal_id=proposal_id,
        target=ApprovalDiffTarget(str(target)),
        dimension_changes=dimension_changes,
        rollback_target_ref=rollback_target_ref,
        evidence_ids=evidence_ids,
        confidence=confidence,
        created_at_utc=created_at_utc or _now_utc(),
    )


def safe_actions_for_lease(lease: ResourceLease, workload: WorkloadEnvelope) -> tuple[SafeActionRow, ...]:
    """Return only actions justified by a lease and workload branch.

    Args:
        lease: Lease value consumed by safe_actions_for_lease().
        workload: Workload value consumed by safe_actions_for_lease().

    Returns:
        tuple[SafeActionRow, ...] value produced by safe_actions_for_lease().
    """
    reason = "; ".join(lease.reasons) if lease.reasons else "lease state"
    status = _coerce_resource_lease_status(lease.status)
    if status is ResourceLeaseStatus.APPROVED:
        return (
            SafeActionRow("pin", lease.lease_id, f"Pin {workload.model_id}", False, reason),
            SafeActionRow("pause", lease.lease_id, f"Pause {workload.workload_id}", False, reason),
            SafeActionRow("cancel", lease.lease_id, f"Cancel {workload.workload_id}", True, reason),
        )
    if status is ResourceLeaseStatus.APPROVAL_REQUIRED:
        return (
            SafeActionRow(
                "adjust_interactive_reserve",
                lease.lease_id,
                "Adjust reserve before admitting workload",
                True,
                reason,
            ),
        )
    return ()


def _coerce_resource_lease_status(value: ResourceLeaseStatus | str) -> ResourceLeaseStatus:
    raw_value = value.value if isinstance(value, Enum) else value
    return value if isinstance(value, ResourceLeaseStatus) else ResourceLeaseStatus(raw_value)


def _lease_summary(lease: ResourceLease, workload: WorkloadEnvelope) -> LeaseSummary:
    return LeaseSummary(
        lease_id=lease.lease_id,
        workload_id=lease.workload_id,
        lane=_enum_value(lease.lane),
        workload_kind=_enum_value(workload.workload_kind),
        model_id=workload.model_id,
        status=lease.status.value,
        placement=lease.model_residency.placement.value,
        residency_action=lease.model_residency.action.value,
        reasons=tuple(lease.reasons),
        evidence_ids=tuple(lease.evidence_ids),
        decided_at_utc=_now_utc(),
    )


def _queued_job_summary(decision: ConcurrencyDecision) -> QueuedJobSummary:
    return QueuedJobSummary(
        workload_id=decision.workload_id,
        lane=decision.lane.value,
        workload_kind="unknown",
        model_id="unknown",
        reason=decision.reasons[0] if decision.reasons else "queue-pressure",
        expected_wait_label=_wait_bucket(decision.active_count, decision.cap.max_active),
        over_cap_action=decision.action.value,
    )


def _wait_bucket(active_count: int, max_active: int) -> str:
    if max_active <= 0:
        return "unknown"
    overflow = max(0, active_count - max_active)
    if overflow == 0:
        return "under-1m"
    if overflow == 1:
        return "1-5m"
    if overflow <= 3:
        return "5-15m"
    return "over-15m"


def _overall_status(
    *,
    degradation_reasons: tuple[str, ...],
    active_leases: tuple[LeaseSummary, ...],
    queued_jobs: tuple[QueuedJobSummary, ...],
    machine_profile_missing: bool,
    runtime_missing: bool,
) -> str:
    if machine_profile_missing and runtime_missing and not active_leases:
        return "unknown"
    if degradation_reasons:
        return "degraded"
    if any(lease.status == ResourceLeaseStatus.APPROVAL_REQUIRED.value for lease in active_leases):
        return "approval_required"
    if any(
        job.over_cap_action in {ConcurrencyAction.APPROVAL_REQUIRED.value, ConcurrencyAction.DENY.value}
        for job in queued_jobs
    ):
        return "approval_required"
    return "ready"


def scheduler_machine_profile_provider(
    base_provider: Callable[[], MachineProfile | None],
    scheduler: Any,
) -> Callable[[], MachineProfile | None]:
    return _scheduler_machine_profile_provider(base_provider, scheduler)


__all__ = RESOURCE_COCKPIT_PUBLIC_EXPORTS
