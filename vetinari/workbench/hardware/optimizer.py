"""Governed hardware optimization proposals."""

from __future__ import annotations

from vetinari.workbench.hardware.contracts import (
    HardwareTwinSnapshot,
    MeasurementStatus,
    ObservationKind,
    OptimizationProposal,
    OptimizationScope,
    ProposalRisk,
)
from vetinari.workbench.hardware.profiles import HardwareProfilePolicy, ProposalRiskPolicy, load_hardware_profiles


def propose_hardware_optimizations(
    snapshot: HardwareTwinSnapshot,
    *,
    measured_quality_gap: bool = False,
    user_prefers_host_change: bool = False,
    policy: HardwareProfilePolicy | None = None,
) -> tuple[OptimizationProposal, ...]:
    """Return advisory-only optimization proposals from measured snapshot evidence.

    Returns:
        tuple[OptimizationProposal, ...] value produced by propose_hardware_optimizations().
    """
    active_policy = policy or load_hardware_profiles()
    if not snapshot.ready:
        risk_policy = active_policy.risk_policy(ProposalRisk.SAFE_ADAPTATION)
        return (
            OptimizationProposal(
                proposal_id="hardware-evidence-blocked",
                scope=OptimizationScope.SCHEDULING,
                risk=ProposalRisk.SAFE_ADAPTATION,
                title="Rebenchmark before optimization",
                rationale="optimizer blocked because snapshot evidence is degraded or stale",
                affected_surface="workbench.hardware",
                confidence=0.0,
                evidence_ids=snapshot.evidence_ids,
                before_measurement_evidence_ids=snapshot.evidence_ids,
                expected_after_evidence_requirements=("fresh-hardware-twin-snapshot",),
                review_required=risk_policy.review_required,
                rollback_notes="restore prior Workbench scheduling policy" if risk_policy.rollback_required else "",
                status="blocked",
                locally_executable=risk_policy.locally_executable,
            ),
        )

    proposals: list[OptimizationProposal] = [
        _safe_adaptation(snapshot, active_policy.risk_policy(ProposalRisk.SAFE_ADAPTATION))
    ]
    if measured_quality_gap or user_prefers_host_change:
        proposals.append(
            _reviewable_host_change(
                snapshot,
                active_policy.risk_policy(ProposalRisk.RISKY_HOST_CHANGE),
                measured_quality_gap=measured_quality_gap,
            )
        )
    return tuple(proposals)


def _safe_adaptation(snapshot: HardwareTwinSnapshot, risk_policy: ProposalRiskPolicy) -> OptimizationProposal:
    model_load = snapshot.observation(ObservationKind.MODEL_LOAD)
    service = snapshot.observation(ObservationKind.SERVICE_RESIDENCY)
    return OptimizationProposal(
        proposal_id="adapt-model-residency-first",
        scope=OptimizationScope.MODEL_RESIDENCY,
        risk=ProposalRisk.SAFE_ADAPTATION,
        title="Prefer Workbench residency and scheduling before host changes",
        rationale="measured warm-load and service-residency evidence can improve reuse without changing host settings",
        affected_surface="workbench.model_residency",
        confidence=0.86,
        evidence_ids=(model_load.evidence_id, service.evidence_id),
        before_measurement_evidence_ids=(model_load.evidence_id, service.evidence_id),
        expected_after_evidence_requirements=("warm-load-after-residency-change", "service-residency-after-change"),
        review_required=risk_policy.review_required,
        rollback_notes="revert Workbench residency/scheduling policy to prior values"
        if risk_policy.rollback_required
        else "",
        status="ready",
        locally_executable=risk_policy.locally_executable,
    )


def _reviewable_host_change(
    snapshot: HardwareTwinSnapshot,
    risk_policy: ProposalRiskPolicy,
    *,
    measured_quality_gap: bool,
) -> OptimizationProposal:
    gpu = snapshot.observation(ObservationKind.GPU_VRAM)
    disk = snapshot.observation(ObservationKind.DISK)
    reason = (
        "measured quality gap remains after Workbench adaptation options"
        if measured_quality_gap
        else "operator preference requests host-level review after measured evidence"
    )
    return OptimizationProposal(
        proposal_id="review-host-runtime-change",
        scope=OptimizationScope.OS_RECOMMENDATION,
        risk=ProposalRisk.RISKY_HOST_CHANGE,
        title="Review host runtime change outside this pack",
        rationale=reason,
        affected_surface="host.runtime",
        confidence=0.61,
        evidence_ids=(gpu.evidence_id, disk.evidence_id),
        before_measurement_evidence_ids=(gpu.evidence_id, disk.evidence_id),
        expected_after_evidence_requirements=("host-change-before-after-benchmark", "rollback-verification-benchmark"),
        review_required=risk_policy.review_required,
        rollback_notes=(
            "restore prior driver/runtime/storage setting and rerun hardware twin benchmarks"
            if risk_policy.rollback_required
            else ""
        ),
        status="review_required",
        locally_executable=risk_policy.locally_executable,
    )


def proposals_ready_for_action(proposals: tuple[OptimizationProposal, ...]) -> tuple[OptimizationProposal, ...]:
    """Return only non-host-mutation proposals ready for downstream UI display."""
    return tuple(
        proposal
        for proposal in proposals
        if proposal.status == MeasurementStatus.READY.value or proposal.status == "ready"
    )


__all__ = ["proposals_ready_for_action", "propose_hardware_optimizations"]
