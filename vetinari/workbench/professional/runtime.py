"""Professional-mode artifact promotion runtime."""

from __future__ import annotations

import hashlib
import os
import threading
import uuid
from datetime import datetime, timezone

from vetinari.workbench.life_admin import SensitiveWorkflowDecision, WorkflowOutcomeKind
from vetinari.workbench.professional.contracts import PromotedArtifactKind, PromotedArtifactRecord
from vetinari.workbench.source_cards import SourceCard, SourceCardLibrary, evaluate_freshness
from vetinari.workbench.tool_cards import ToolCardLibrary

_PROFESSIONAL_RUNTIME_LOCK = threading.Lock()
_PROFESSIONAL_RUNTIME_INSTANCE: ProfessionalRuntime | None = None
_OUTCOME_TO_ARTIFACT_KIND = {
    WorkflowOutcomeKind.CHECKLIST: PromotedArtifactKind.CHECKLIST,
    WorkflowOutcomeKind.DOCUMENT_PACKET: PromotedArtifactKind.DOCUMENT_PACKET,
    WorkflowOutcomeKind.PROFESSIONAL_MEMO: PromotedArtifactKind.PROFESSIONAL_MEMO,
    WorkflowOutcomeKind.SOURCE_BACKED_NOTE: PromotedArtifactKind.SOURCE_BACKED_NOTE,
    WorkflowOutcomeKind.REMINDER: PromotedArtifactKind.REMINDER,
    WorkflowOutcomeKind.EVIDENCE_NOTEBOOK_ENTRY: PromotedArtifactKind.EVIDENCE_NOTEBOOK_ENTRY,
    WorkflowOutcomeKind.QUESTIONS_FOR_PROFESSIONAL: PromotedArtifactKind.MEETING_PREP_BRIEF,
    WorkflowOutcomeKind.ORGANIZE_DOCUMENTS: PromotedArtifactKind.DOCUMENT_PACKET,
    WorkflowOutcomeKind.EXPLAIN_CONCEPT: PromotedArtifactKind.SOURCE_BACKED_NOTE,
}


class ProfessionalPromotionRejected(Exception):
    """Raised when a workflow decision cannot be promoted to an artifact."""

    def __init__(self, reason: tuple[str, ...]) -> None:
        super().__init__(", ".join(reason))
        self.reason = reason


class ProfessionalRuntime:
    """Promote allowed workflow decisions without writing to the spine."""

    def __init__(
        self,
        *,
        source_card_library: SourceCardLibrary | None = None,
        tool_card_library: ToolCardLibrary | None = None,
        clock: object | None = None,
    ) -> None:
        self.source_card_library = source_card_library
        self.tool_card_library = tool_card_library
        self.clock = clock

    def promote_workflow_outcome(
        self,
        decision: SensitiveWorkflowDecision,
        draft_text: str,
        source_card_ids: tuple[str, ...] = (),
        tool_card_ids: tuple[str, ...] = (),
    ) -> PromotedArtifactRecord:
        """Promote an allowed decision into a deterministic artifact record.

        Args:
            decision: Decision value consumed by promote_workflow_outcome().
            draft_text: Draft text value consumed by promote_workflow_outcome().
            source_card_ids: Source object or text processed by the operation.
            tool_card_ids: Tool card ids value consumed by promote_workflow_outcome().

        Returns:
            PromotedArtifactRecord value produced by promote_workflow_outcome().

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        if decision.allowed is False:
            raise ProfessionalPromotionRejected(tuple(decision.denial_reasons or decision.missing_context))
        expected_artifact_kind = _OUTCOME_TO_ARTIFACT_KIND.get(decision.workflow_outcome_kind)
        if expected_artifact_kind is None:
            raise ProfessionalPromotionRejected(("outcome_has_no_promotable_artifact",))
        artifact_kind = (
            PromotedArtifactKind(decision.promoted_artifact_kind)
            if decision.promoted_artifact_kind
            else expected_artifact_kind
        )
        if artifact_kind != expected_artifact_kind:
            raise ProfessionalPromotionRejected(("outcome_artifact_kind_mismatch",))
        if not draft_text.strip():
            raise ProfessionalPromotionRejected(("draft_text_required",))

        sources = self._resolve_sources(decision.project_id, source_card_ids)
        now = _clock_now(self.clock)
        for source in sources:
            verdict = evaluate_freshness(source, now_utc=now)
            if not verdict.passed:
                raise ProfessionalPromotionRejected(("source_freshness_failed", source.source_card_id))

        for tool_card_id in tool_card_ids:
            tool = self._resolve_tool(decision.project_id, tool_card_id)
            promotion = tool.may_promote_to_claim(
                claim_kind=artifact_kind.value,
                sources=sources,
                caveats_acknowledged=True,
            )
            if not promotion.passed:
                raise ProfessionalPromotionRejected(("tool_caveat_blocked", *promotion.rejection_reasons))

        provenance = (
            ("policy_explanation_ref", decision.policy_explanation_ref),
            ("rigor_required", decision.rigor_required.value),
            ("mode_lens_id", decision.mode_lens_id),
            ("decision_id", decision.decision_id),
        )
        created_at = now
        draft_hash = hashlib.sha256(draft_text.encode("utf-8")).hexdigest()
        artifact_id = str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"vetinari-professional|{decision.decision_id}|{artifact_kind.value}|{draft_hash}",
            )
        )
        return PromotedArtifactRecord(
            artifact_id=artifact_id,
            artifact_kind=artifact_kind,
            project_id=decision.project_id,
            created_at_utc=created_at.isoformat(),
            provenance=provenance,
            source_card_ids=tuple(source_card_ids),
            tool_card_ids=tuple(tool_card_ids),
            claim_promotion_decision_ref=decision.policy_explanation_ref,
            mode_lens_id=decision.mode_lens_id,
            rigor_level=decision.rigor_required,
            authority_ref=decision.authority_ref,
            evidence_refs=decision.evidence_refs,
        )

    def _resolve_sources(self, project_id: str, source_card_ids: tuple[str, ...]) -> tuple[SourceCard, ...]:
        if not source_card_ids:
            return ()
        library = self.source_card_library or SourceCardLibrary()
        sources: list[SourceCard] = []
        for source_card_id in source_card_ids:
            source = library.get_card(project_id=project_id, source_card_id=source_card_id)
            if source is None:
                raise ProfessionalPromotionRejected(("source_card_not_found", source_card_id))
            sources.append(source)
        return tuple(sources)

    def _resolve_tool(self, project_id: str, tool_card_id: str):
        library = self.tool_card_library or ToolCardLibrary()
        tool = library.get_card(project_id=project_id, tool_card_id=tool_card_id)
        if tool is None:
            raise ProfessionalPromotionRejected(("tool_card_not_found", tool_card_id))
        return tool


def promote_workflow_outcome(
    decision: SensitiveWorkflowDecision,
    draft_text: str,
    source_card_ids: tuple[str, ...] = (),
    tool_card_ids: tuple[str, ...] = (),
) -> PromotedArtifactRecord:
    """Promote through the process runtime singleton."""
    return get_professional_runtime().promote_workflow_outcome(decision, draft_text, source_card_ids, tool_card_ids)


def get_professional_runtime() -> ProfessionalRuntime:
    """Return the process runtime singleton using double-checked locking.

    Returns:
        Resolved professional runtime value.
    """
    global _PROFESSIONAL_RUNTIME_INSTANCE
    if _PROFESSIONAL_RUNTIME_INSTANCE is None:
        with _PROFESSIONAL_RUNTIME_LOCK:
            if _PROFESSIONAL_RUNTIME_INSTANCE is None:
                _PROFESSIONAL_RUNTIME_INSTANCE = ProfessionalRuntime()
    return _PROFESSIONAL_RUNTIME_INSTANCE


def reset_professional_runtime_for_test() -> None:
    """Reset the singleton in pytest only.

    Raises:
        Exception: Propagates validation or runtime failures from the underlying operation.
    """
    if "PYTEST_CURRENT_TEST" not in os.environ:
        raise RuntimeError("reset_professional_runtime_for_test is pytest-only")
    global _PROFESSIONAL_RUNTIME_INSTANCE
    with _PROFESSIONAL_RUNTIME_LOCK:
        _PROFESSIONAL_RUNTIME_INSTANCE = None


def _clock_now(clock: object | None) -> datetime:
    if clock is not None and hasattr(clock, "utc_now"):
        value = clock.utc_now()
        if isinstance(value, datetime):
            return value.astimezone(timezone.utc)
    if clock is not None and hasattr(clock, "now"):
        value = clock.now(timezone.utc)
        if isinstance(value, datetime):
            return value.astimezone(timezone.utc)
    return datetime.now(timezone.utc)


__all__ = [
    "ProfessionalPromotionRejected",
    "ProfessionalRuntime",
    "get_professional_runtime",
    "promote_workflow_outcome",
    "reset_professional_runtime_for_test",
]
