"""Prompt-promotion persistence helpers for the method library."""

from __future__ import annotations

import hashlib
import threading
from datetime import datetime, timezone

from vetinari.agents.contracts import OutcomeSignal, Provenance, ToolEvidence
from vetinari.types import AgentType, EvidenceBasis, ShardKind
from vetinari.workbench.assets import AssetKind, WorkbenchAsset
from vetinari.workbench.evals import EvalKind, EvalResult, EvalScore
from vetinari.workbench.metadata_spine import WorkbenchSpine
from vetinari.workbench.proposals import (
    Promotion,
    ProposalGate,
    ProposalStatus,
    WorkbenchProposal,
    WorkbenchProposalKind,
)
from vetinari.workbench.runs import RunKind, RunMetric, RunStatus, WorkbenchRun

_PROMPT_METHOD_PROMOTION_LOCK = threading.Lock()


class MethodPromotionRejected(ValueError):
    """Raised when a method promotion lacks measured positive evidence."""


def validate_prompt_promotion_inputs(
    *,
    agent_type: str,
    variant_id: str,
    prompt_text: str,
    provenance_ref: str,
    consent_ref: str,
    safety_ref: str,
    confidence: float,
) -> float:
    """Validate fail-closed prompt promotion signals.

    Returns:
        Normalized confidence value.

    Raises:
        MethodPromotionRejected: If required provenance, safety, consent, or confidence signals are invalid.
    """
    missing = [
        name
        for name, value in {
            "agent_type": agent_type,
            "variant_id": variant_id,
            "prompt_text": prompt_text,
            "provenance_ref": provenance_ref,
            "consent_ref": consent_ref,
            "safety_ref": safety_ref,
        }.items()
        if not isinstance(value, str) or not value.strip()
    ]
    if missing:
        raise MethodPromotionRejected(f"prompt method promotion missing required signals: {', '.join(missing)}")
    try:
        confidence_value = float(confidence)
    except (TypeError, ValueError) as exc:
        raise MethodPromotionRejected("prompt method promotion requires numeric confidence") from exc
    if confidence_value < 0.7:
        raise MethodPromotionRejected("prompt method promotion requires confidence >= 0.7")
    if safety_ref.strip().lower() in {"unsafe", "failed", "blocked"}:
        raise MethodPromotionRejected("prompt method promotion safety gate blocked this variant")
    return confidence_value


def record_prompt_method_evidence(
    *,
    spine: WorkbenchSpine,
    project_id: str,
    agent_type: str,
    variant_id: str,
    prompt_text: str,
    quality_score: float,
    baseline_score: float,
    provenance_ref: str,
    consent_ref: str,
    safety_ref: str,
    confidence: float,
    promoted_by: str,
    method_kind_value: str,
    method_kind_metric_name: str,
    min_evaluations: int,
) -> None:
    """Persist measured evidence for a promoted prompt variant."""
    confidence_value = validate_prompt_promotion_inputs(
        agent_type=agent_type,
        variant_id=variant_id,
        prompt_text=prompt_text,
        provenance_ref=provenance_ref,
        consent_ref=consent_ref,
        safety_ref=safety_ref,
        confidence=confidence,
    )
    asset_id = _prompt_method_asset_id(project_id, agent_type, variant_id)
    revision = _prompt_revision(prompt_text)
    baseline_value = max(0.0, min(1.0, float(baseline_score)))
    method_value = max(0.0, min(1.0, float(quality_score)))
    with _PROMPT_METHOD_PROMOTION_LOCK:
        if any(asset.asset_id == asset_id and asset.revision == revision for asset in spine.list_assets()):
            return
        _append_prompt_method_records(
            spine=spine,
            project_id=project_id,
            agent_type=agent_type,
            variant_id=variant_id,
            prompt_text=prompt_text,
            asset_id=asset_id,
            revision=revision,
            method_value=method_value,
            baseline_value=baseline_value,
            provenance_ref=provenance_ref,
            consent_ref=consent_ref,
            safety_ref=safety_ref,
            confidence_value=confidence_value,
            promoted_by=promoted_by,
            method_kind_value=method_kind_value,
            method_kind_metric_name=method_kind_metric_name,
            min_evaluations=min_evaluations,
        )


def _append_prompt_method_records(
    *,
    spine: WorkbenchSpine,
    project_id: str,
    agent_type: str,
    variant_id: str,
    prompt_text: str,
    asset_id: str,
    revision: str,
    method_value: float,
    baseline_value: float,
    provenance_ref: str,
    consent_ref: str,
    safety_ref: str,
    confidence_value: float,
    promoted_by: str,
    method_kind_value: str,
    method_kind_metric_name: str,
    min_evaluations: int,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    baseline_run_id = f"{asset_id}:baseline-run"
    method_run_id = f"{asset_id}:method-run"
    method_evals = _method_evals(
        asset_id=asset_id,
        revision=revision,
        run_id=method_run_id,
        method_value=method_value,
        baseline_value=baseline_value,
        variant_id=variant_id,
        promoted_by=promoted_by,
        now=now,
        min_evaluations=min_evaluations,
    )
    outcome = _prompt_outcome(method_value, baseline_value, now)
    spine.append_asset(
        _prompt_asset(
            asset_id=asset_id,
            agent_type=agent_type,
            variant_id=variant_id,
            prompt_text=prompt_text,
            revision=revision,
            now=now,
            provenance_ref=provenance_ref,
            consent_ref=consent_ref,
            safety_ref=safety_ref,
            confidence_value=confidence_value,
        )
    )
    spine.append_run(_baseline_run(asset_id, revision, baseline_run_id, project_id, now))
    spine.append_run(
        _method_run(
            asset_id,
            revision,
            method_run_id,
            project_id,
            now,
            method_value,
            baseline_value,
            confidence_value,
            outcome,
            method_kind_value,
            method_kind_metric_name,
        )
    )
    for idx in range(1, min_evaluations + 1):
        spine.append_eval(_baseline_eval(asset_id, revision, baseline_run_id, idx, baseline_value, now))
    for eval_result in method_evals:
        spine.append_eval(eval_result)
    _append_prompt_promotion(
        spine, asset_id, revision, method_evals, outcome, now, consent_ref, safety_ref, confidence_value, promoted_by
    )


def _method_evals(
    *,
    asset_id: str,
    revision: str,
    run_id: str,
    method_value: float,
    baseline_value: float,
    variant_id: str,
    promoted_by: str,
    now: str,
    min_evaluations: int,
) -> tuple[EvalResult, ...]:
    return tuple(
        _method_eval(
            asset_id=asset_id,
            revision=revision,
            run_id=run_id,
            idx=idx,
            method_value=method_value,
            baseline_value=baseline_value,
            variant_id=variant_id,
            promoted_by=promoted_by,
            now=now,
        )
        for idx in range(1, min_evaluations + 1)
    )


def _prompt_outcome(method_value: float, baseline_value: float, now: str) -> OutcomeSignal:
    return OutcomeSignal(
        passed=method_value > baseline_value,
        score=method_value,
        basis=EvidenceBasis.TOOL_EVIDENCE,
        tool_evidence=(
            ToolEvidence(
                tool_name="prompt_evolver",
                command="promote_variant_to_method_library",
                exit_code=0 if method_value > baseline_value else 1,
                stdout_snippet=f"method_score={method_value:.3f}; baseline_score={baseline_value:.3f}",
                passed=method_value > baseline_value,
            ),
        ),
        provenance=Provenance(
            source="vetinari.learning.prompt_evolver",
            timestamp_utc=now,
            tool_name="prompt_evolver",
        ),
        kind=ShardKind.STANDARD,
    )


def _prompt_asset(
    *,
    asset_id: str,
    agent_type: str,
    variant_id: str,
    prompt_text: str,
    revision: str,
    now: str,
    provenance_ref: str,
    consent_ref: str,
    safety_ref: str,
    confidence_value: float,
) -> WorkbenchAsset:
    return WorkbenchAsset(
        asset_id=asset_id,
        kind=AssetKind.PROMPT,
        name=f"Promoted prompt {agent_type}:{variant_id}",
        revision=revision,
        created_at_utc=now,
        provenance={
            "source": provenance_ref,
            "agent_type": agent_type,
            "variant_id": variant_id,
            "prompt_text": prompt_text,
            "consent_ref": consent_ref,
            "safety_ref": safety_ref,
            "confidence": str(confidence_value),
        },
    )


def _append_prompt_promotion(
    spine: WorkbenchSpine,
    asset_id: str,
    revision: str,
    method_evals: tuple[EvalResult, ...],
    outcome: OutcomeSignal,
    now: str,
    consent_ref: str,
    safety_ref: str,
    confidence_value: float,
    promoted_by: str,
) -> None:
    proposal_id = f"{asset_id}:proposal"
    spine.append_proposal(
        _proposal(
            asset_id, revision, proposal_id, method_evals, outcome, now, consent_ref, safety_ref, confidence_value
        )
    )
    spine.record_promotion(
        Promotion(
            promotion_id=f"{asset_id}:promotion",
            proposal_id=proposal_id,
            accepted=True,
            decided_at_utc=now,
            decided_by=promoted_by,
            rationale="",
        ),
    )


def _baseline_run(asset_id: str, revision: str, run_id: str, project_id: str, now: str):
    return WorkbenchRun(
        run_id=run_id,
        kind=RunKind.EVAL_RUN,
        status=RunStatus.SUCCEEDED,
        started_at_utc=now,
        finished_at_utc=now,
        actor_agent_type=AgentType.WORKBENCH,
        asset_revisions=((asset_id, revision),),
        lease_id="",
        shard_kind=ShardKind.STANDARD,
        metrics=(RunMetric("baseline_kind", 1.0, "control"),),
        project_id=project_id,
    )


def _method_run(
    asset_id: str,
    revision: str,
    run_id: str,
    project_id: str,
    now: str,
    method_value: float,
    baseline_value: float,
    confidence_value: float,
    outcome: OutcomeSignal,
    method_kind_value: str,
    method_kind_metric_name: str,
):
    return WorkbenchRun(
        run_id=run_id,
        kind=RunKind.AGENT_RUN,
        status=RunStatus.SUCCEEDED,
        started_at_utc=now,
        finished_at_utc=now,
        actor_agent_type=AgentType.WORKBENCH,
        asset_revisions=((asset_id, revision),),
        lease_id="",
        shard_kind=ShardKind.STANDARD,
        metrics=(
            RunMetric(method_kind_metric_name, 1.0, method_kind_value),
            RunMetric("prompt_quality_delta", method_value - baseline_value, "score"),
            RunMetric("prompt_confidence", confidence_value, "ratio"),
        ),
        outcome=outcome,
        project_id=project_id,
    )


def _method_eval(
    *,
    asset_id: str,
    revision: str,
    run_id: str,
    idx: int,
    method_value: float,
    baseline_value: float,
    variant_id: str,
    promoted_by: str,
    now: str,
) -> EvalResult:
    return EvalResult(
        eval_id=f"{asset_id}:method-eval-{idx}",
        kind=EvalKind.OFFLINE_SUITE,
        run_id=run_id,
        asset_id=asset_id,
        asset_revision=revision,
        scores=(
            EvalScore(
                metric_name="prompt_quality",
                value=method_value,
                threshold=baseline_value,
                passed=method_value > baseline_value,
            ),
        ),
        captured_at_utc=now,
        notes=f"Prompt variant {variant_id} promoted by {promoted_by}",
    )


def _baseline_eval(asset_id: str, revision: str, run_id: str, idx: int, baseline_value: float, now: str) -> EvalResult:
    return EvalResult(
        eval_id=f"{asset_id}:baseline-eval-{idx}",
        kind=EvalKind.OFFLINE_SUITE,
        run_id=run_id,
        asset_id=asset_id,
        asset_revision=revision,
        scores=(EvalScore(metric_name="prompt_quality", value=baseline_value, threshold=0.0, passed=True),),
        captured_at_utc=now,
        notes="measured baseline control prompt_quality",
    )


def _proposal(
    asset_id: str,
    revision: str,
    proposal_id: str,
    method_evals: tuple[EvalResult, ...],
    outcome: OutcomeSignal,
    now: str,
    consent_ref: str,
    safety_ref: str,
    confidence_value: float,
) -> WorkbenchProposal:
    return WorkbenchProposal(
        proposal_id=proposal_id,
        kind=WorkbenchProposalKind.PROMPT_VERSION,
        status=ProposalStatus.ACCEPTED,
        affected_assets=(asset_id,),
        affected_revisions=((asset_id, revision),),
        pre_promotion_evals=method_evals,
        gate=ProposalGate(provenance_present=True, eval_present=True, rollback_plan_present=True, blockers=()),
        attached_outcome=outcome,
        opened_at_utc=now,
        closed_at_utc=now,
        notes=f"consent={consent_ref}; safety={safety_ref}; confidence={confidence_value:.3f}",
    )


def _prompt_revision(prompt_text: str) -> str:
    return hashlib.sha256(prompt_text.encode("utf-8")).hexdigest()[:16]


def _prompt_method_asset_id(project_id: str, agent_type: str, variant_id: str) -> str:
    raw = f"{project_id}:{agent_type}:{variant_id}".encode()
    return f"prompt-method-{hashlib.sha256(raw).hexdigest()[:20]}"
