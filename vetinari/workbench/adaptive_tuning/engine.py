"""Adaptive tuning hypothesis and proposal engine."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone

from vetinari.workbench.adaptive_tuning.contracts import (
    AdaptationTarget,
    AdaptiveHypothesis,
    AdaptiveTuningPolicyDecision,
    EvidenceScope,
    FrictionSignalKind,
    HypothesisStatus,
    LocalChangeProposal,
    NormalizedEvidence,
    PreviewPacket,
    PromotionEvidence,
    RiskTier,
    RollbackRequirement,
)
from vetinari.workbench.adaptive_tuning.policy import (
    AdaptiveTuningPolicy,
    classify_target_risk,
    evaluate_proposal_policy,
)


class AdaptiveTuningEngine:
    """Build reviewable hypotheses and proposals without mutating settings."""

    def __init__(self, policy: AdaptiveTuningPolicy | None = None) -> None:
        self.policy = policy or AdaptiveTuningPolicy()

    def detect_hypotheses(
        self,
        evidence: tuple[NormalizedEvidence, ...] | list[NormalizedEvidence],
        *,
        now_utc: datetime | None = None,
    ) -> tuple[AdaptiveHypothesis, ...]:
        """Detect repeated friction and return inspectable hypotheses.

        Returns:
            tuple[AdaptiveHypothesis, ...] value produced by detect_hypotheses().
        """
        current = _now(now_utc)
        grouped: dict[tuple[str, str, FrictionSignalKind], list[NormalizedEvidence]] = defaultdict(list)
        for item in evidence:
            if not item.accepted or item.scope is None:
                continue
            grouped[item.scope.project_id, item.scope.surface, item.kind].append(item)

        hypotheses: list[AdaptiveHypothesis] = []
        for (project_id, surface, kind), rows in grouped.items():
            if len(rows) < 2:
                continue
            confidence = min(0.99, sum(row.confidence for row in rows) / len(rows) + 0.1 * (len(rows) - 1))
            scope = EvidenceScope(project_id=project_id, surface=surface)
            hypothesis_id = f"{project_id}:{surface}:{kind.value}"
            title = _title_for_kind(kind, surface)
            target = _observed_target(rows)
            proposal = self.create_proposal(
                hypothesis_id=hypothesis_id,
                title=title,
                kind=kind,
                target=target,
                requested_auto_apply=False,
            )
            hypotheses.append(
                AdaptiveHypothesis(
                    hypothesis_id=hypothesis_id,
                    title=title,
                    status=HypothesisStatus.PENDING,
                    scope=scope,
                    evidence=tuple(rows),
                    confidence=confidence,
                    created_at_utc=current.isoformat().replace("+00:00", "Z"),
                    last_observed_at_utc=max(row.observed_at_utc for row in rows),
                    decay_after_days=self.policy.evidence_stale_after_days,
                    proposal=proposal,
                )
            )
        return tuple(hypotheses)

    def create_proposal(
        self,
        *,
        hypothesis_id: str,
        title: str,
        kind: FrictionSignalKind,
        target: AdaptationTarget,
        risk_tier: RiskTier | None = None,
        requested_auto_apply: bool = False,
        approval_ref: str = "",
        tests_ref: str = "",
        rollback_ref: str = "",
        promotion_evidence: PromotionEvidence | None = None,
    ) -> LocalChangeProposal:
        """Create a governed proposal packet.

        Returns:
            Newly constructed proposal value.
        """
        tier = risk_tier or classify_target_risk(target)
        proposal_id = f"proposal:{hypothesis_id}"
        preview = PreviewPacket(
            proposal_id=proposal_id,
            before={"surface": "current", "kind": kind.value},
            after={"surface": "proposed", "target": target.value},
            changed_dimensions=("surface", "control", "rollback"),
        )
        rollback = RollbackRequirement(
            required=tier is RiskTier.HIGH,
            rollback_ref=rollback_ref,
            readiness_checked=bool(rollback_ref),
        )
        return LocalChangeProposal(
            proposal_id=proposal_id,
            hypothesis_id=hypothesis_id,
            target=target,
            risk_tier=tier,
            title=title,
            summary=f"Review adaptive response to {kind.value}",
            preview=preview,
            approval_ref=approval_ref,
            tests_ref=tests_ref,
            rollback=rollback,
            promotion_evidence=promotion_evidence,
            requested_auto_apply=requested_auto_apply,
        )

    def proposal_decision(
        self, proposal: LocalChangeProposal, *, now_utc: datetime | None = None
    ) -> AdaptiveTuningPolicyDecision:
        """Evaluate proposal admission under this engine policy."""
        return evaluate_proposal_policy(proposal, self.policy, now_utc=now_utc)


def _title_for_kind(kind: FrictionSignalKind, surface: str) -> str:
    return f"{kind.value.replace('_', ' ').title()} on {surface}"


def _observed_target(rows: list[NormalizedEvidence]) -> AdaptationTarget:
    targets = {row.target for row in rows if row.target is not None}
    if len(targets) == 1:
        return next(iter(targets))
    if len(targets) > 1:
        return AdaptationTarget.PROJECT_DEFAULT
    return AdaptationTarget.LOCAL_SHORTCUT


def _now(value: datetime | None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)
