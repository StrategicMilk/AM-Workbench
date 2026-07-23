"""Fail-closed data mart for RAG and runtime outcomes.

The mart is intentionally import-safe and side-effect free. It accepts records
emitted by retrieval, serving, training, governance, and source-health lanes and
returns typed decisions/proposals without mutating those dependency-owned
surfaces.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from math import isfinite

from vetinari.workbench.outcomes.contracts import (
    OutcomeDecision,
    OutcomeFailureKind,
    OutcomeMartPolicy,
    OutcomeProposal,
    OutcomeProposalKind,
    OutcomeRecord,
    OutcomeStage,
    ResourcePressure,
    RetentionGate,
    RuntimeOutcomeGovernance,
)
from vetinari.workbench.outcomes.contracts import (
    OutcomeStageScore as OutcomeStageScore,
)


class OutcomeMart:
    """In-memory outcome mart that never writes dependency-owned state."""

    def __init__(self, records: Sequence[OutcomeRecord] = (), *, policy: OutcomeMartPolicy | None = None) -> None:
        self._policy = policy or OutcomeMartPolicy()
        self._records: dict[str, OutcomeRecord] = {}
        for record in records:
            self.record(record)

    @property
    def records(self) -> tuple[OutcomeRecord, ...]:
        """Return accepted records in insertion order."""
        return tuple(self._records.values())

    def record(self, record: OutcomeRecord) -> OutcomeDecision:
        """Accept one trusted record or return a degraded fail-closed decision.

        Returns:
            OutcomeDecision value produced by record().
        """
        decision = evaluate_outcome_record(record, policy=self._policy)
        if decision.accepted:
            if record.outcome_id in self._records:
                return OutcomeDecision(
                    outcome_id=record.outcome_id,
                    accepted=False,
                    blockers=("duplicate_outcome_id",),
                    degraded_status="blocked",
                    evidence_refs=decision.evidence_refs,
                    failure_kinds=decision.failure_kinds,
                )
            self._records[record.outcome_id] = record
        return decision

    def proposals(self) -> tuple[OutcomeProposal, ...]:
        """Return candidate proposals derived from accepted records."""
        return tuple(propose_runtime_remediation(tuple(self._records.values()), policy=self._policy))


def evaluate_outcome_record(record: OutcomeRecord, *, policy: OutcomeMartPolicy | None = None) -> OutcomeDecision:
    """Return a fail-closed decision for an outcome record.

    Returns:
        OutcomeDecision value produced by evaluate_outcome_record().
    """
    selected_policy = policy or OutcomeMartPolicy()
    blockers: list[str] = []
    failures: list[OutcomeFailureKind] = []
    evidence_refs: list[str] = []

    stage_by_name = {score.stage for score in record.stage_scores}
    required_stages = {
        OutcomeStage.QUERY,
        OutcomeStage.RETRIEVAL,
        OutcomeStage.RERANK,
        OutcomeStage.CONTEXT,
        OutcomeStage.ANSWER,
        OutcomeStage.RUNTIME,
    }
    missing_stages = sorted(stage.value for stage in required_stages - stage_by_name)
    if missing_stages:
        blockers.append("missing_stage_scores:" + ",".join(missing_stages))

    for score in record.stage_scores:
        evidence_refs.extend(score.evidence_refs)
        failures.extend(score.failure_kinds)
        if not score.evidence_refs:
            blockers.append(f"{score.stage.value}_evidence_missing")
        if score.score < selected_policy.min_stage_score:
            blockers.append(f"{score.stage.value}_score_below_policy")

    _append_governance_blockers(blockers, evidence_refs, record.governance, selected_policy)
    _append_retention_blockers(blockers, record.retention)
    _append_resource_blockers(blockers, failures, record.resource_pressure, selected_policy)

    accepted = not blockers
    return OutcomeDecision(
        outcome_id=record.outcome_id,
        accepted=accepted,
        blockers=tuple(dict.fromkeys(blockers)),
        degraded_status="accepted" if accepted else "blocked",
        evidence_refs=tuple(dict.fromkeys(evidence_refs)),
        failure_kinds=tuple(dict.fromkeys(failures)),
    )


def propose_runtime_remediation(
    records: Iterable[OutcomeRecord],
    *,
    policy: OutcomeMartPolicy | None = None,
) -> list[OutcomeProposal]:
    """Derive model, route, RAG, source, and runtime proposals from accepted records.

    Returns:
        list[OutcomeProposal] value produced by propose_runtime_remediation().
    """
    selected_policy = policy or OutcomeMartPolicy()
    proposals: list[OutcomeProposal] = []
    for record in records:
        decision = evaluate_outcome_record(record, policy=selected_policy)
        if not decision.accepted:
            continue
        proposals.extend(_proposals_for_accepted_record(record, decision, selected_policy))
    return proposals


def _proposals_for_accepted_record(
    record: OutcomeRecord,
    decision: OutcomeDecision,
    policy: OutcomeMartPolicy,
) -> list[OutcomeProposal]:
    proposals: list[OutcomeProposal] = []
    failure_kinds = set(decision.failure_kinds)
    stage_scores = {score.stage: score.score for score in record.stage_scores}
    evidence_refs = decision.evidence_refs
    pressure = record.resource_pressure
    if {
        OutcomeFailureKind.CITATION_FAILURE,
        OutcomeFailureKind.PARSER_FAILURE,
        OutcomeFailureKind.DUPLICATE_CHUNK,
        OutcomeFailureKind.EMBEDDING_DRIFT,
    } & failure_kinds:
        proposals.append(
            _proposal(
                OutcomeProposalKind.SOURCE_REFRESH,
                record,
                "source health or parser evidence degraded retrieval trust",
                record.source_health_report_id,
                evidence_refs,
            )
        )
    if min(stage_scores.get(OutcomeStage.RETRIEVAL, 1.0), stage_scores.get(OutcomeStage.RERANK, 1.0)) < 0.9:
        proposals.append(
            _proposal(
                OutcomeProposalKind.RAG_TUNING,
                record,
                "retrieval or rerank score is below tuning target",
                record.retrieval_index_ref,
                evidence_refs,
            )
        )
    proposals.extend(_resource_pressure_proposals(record, pressure, policy, evidence_refs))
    return proposals


def _resource_pressure_proposals(
    record: OutcomeRecord,
    pressure: ResourcePressure,
    policy: OutcomeMartPolicy,
    evidence_refs: tuple[str, ...],
) -> list[OutcomeProposal]:
    proposals: list[OutcomeProposal] = []
    if pressure.provider_acceptance_failed or pressure.fallback_used:
        proposals.append(
            _proposal(
                OutcomeProposalKind.ROUTE_POLICY_CHANGE,
                record,
                "provider acceptance or fallback evidence indicates route policy drift",
                record.runtime_ref,
                evidence_refs,
            )
        )
    if pressure.memory_pressure > 0.75 or pressure.gpu_pressure > 0.75:
        proposals.append(
            _proposal(
                OutcomeProposalKind.QUANTIZATION_REVIEW,
                record,
                "local memory or GPU pressure is near capacity",
                record.model_version_id,
                evidence_refs,
            )
        )
    if pressure.latency_ms > policy.max_latency_ms or pressure.queue_delay_ms > policy.max_queue_delay_ms:
        proposals.append(
            _proposal(
                OutcomeProposalKind.RUNTIME_REMEDIATION,
                record,
                "latency or queue delay exceeded runtime policy",
                record.runtime_ref,
                evidence_refs,
            )
        )
    if pressure.provider_acceptance_failed and pressure.memory_pressure < 0.6 and pressure.gpu_pressure < 0.6:
        proposals.append(
            _proposal(
                OutcomeProposalKind.MODEL_DOWNLOAD,
                record,
                "remote provider rejected work while local capacity appears available",
                record.model_version_id,
                evidence_refs,
            )
        )
    return proposals


def _append_governance_blockers(
    blockers: list[str],
    evidence_refs: list[str],
    governance: RuntimeOutcomeGovernance,
    policy: OutcomeMartPolicy,
) -> None:
    evidence_refs.extend(governance.evidence_refs)
    evidence_refs.extend(governance.provenance_refs)
    evidence_refs.extend(governance.authority_refs)
    evidence_refs.extend(governance.safety_refs)
    if not governance.evidence_refs:
        blockers.append("missing_evidence")
    if not governance.provenance_refs:
        blockers.append("missing_provenance")
    if not governance.authority_refs:
        blockers.append("missing_authority")
    if not governance.safety_refs:
        blockers.append("missing_safety")
    if not governance.budget_ref.strip():
        blockers.append("missing_budget")
    else:
        evidence_refs.append(governance.budget_ref)
    if not governance.persisted_state_ref.strip():
        blockers.append("persisted_state_unavailable")
    else:
        evidence_refs.append(governance.persisted_state_ref)
    if governance.confidence is None or not isfinite(governance.confidence):
        blockers.append("confidence_unavailable")
    elif not 0 <= governance.confidence <= 1:
        blockers.append("confidence_out_of_range")
    elif governance.confidence < policy.min_confidence:
        blockers.append("confidence_below_policy")


def _append_retention_blockers(blockers: list[str], retention: RetentionGate) -> None:
    taint = retention.content_taint.lower()
    if taint not in {"clean", "private", "restricted"}:
        blockers.append("content_taint_unrecognized")
    if taint != "clean" and not retention.redaction_ref.strip():
        blockers.append("private_content_redaction_missing")
    if taint != "clean" and retention.raw_content_ref.strip():
        blockers.append("raw_private_content_not_allowed")
    if taint != "clean" and retention.raw_content_retention_days != 0:
        blockers.append("private_content_retention_not_zero")


def _append_resource_blockers(
    blockers: list[str],
    failures: list[OutcomeFailureKind],
    pressure: ResourcePressure,
    policy: OutcomeMartPolicy,
) -> None:
    if pressure.latency_ms > policy.max_latency_ms:
        blockers.append("latency_exceeded")
        failures.append(OutcomeFailureKind.LATENCY_EXCEEDED)
    if pressure.queue_delay_ms > policy.max_queue_delay_ms:
        blockers.append("queue_delay_exceeded")
        failures.append(OutcomeFailureKind.QUEUE_DELAY)
    if pressure.token_cost_usd > policy.max_token_cost_usd:
        blockers.append("token_cost_exceeded")
        failures.append(OutcomeFailureKind.COST_EXCEEDED)
    if pressure.memory_pressure > policy.max_memory_pressure:
        blockers.append("memory_pressure_exceeded")
        failures.append(OutcomeFailureKind.MEMORY_PRESSURE)
    if pressure.gpu_pressure > policy.max_gpu_pressure:
        blockers.append("gpu_pressure_exceeded")
        failures.append(OutcomeFailureKind.GPU_PRESSURE)
    if pressure.fallback_used:
        failures.append(OutcomeFailureKind.FALLBACK_USED)
    if pressure.cancelled:
        blockers.append("request_cancelled")
        failures.append(OutcomeFailureKind.CANCELLATION)
    if pressure.provider_acceptance_failed:
        failures.append(OutcomeFailureKind.PROVIDER_ACCEPTANCE_FAILED)


def _proposal(
    kind: OutcomeProposalKind,
    record: OutcomeRecord,
    reason: str,
    target_ref: str,
    evidence_refs: tuple[str, ...],
) -> OutcomeProposal:
    return OutcomeProposal(
        proposal_id=f"{kind.value}:{record.outcome_id}",
        kind=kind,
        reason=reason,
        source_outcome_ids=(record.outcome_id,),
        evidence_refs=evidence_refs,
        target_ref=target_ref,
    )
