"""Exhaustive labels for professional workflow outcomes and artifact kinds."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from vetinari.workbench.life_admin import WorkflowOutcomeKind
from vetinari.workbench.professional.contracts import PromotedArtifactKind

OUTCOME_KIND_LABELS: Mapping[WorkflowOutcomeKind, str] = MappingProxyType({
    WorkflowOutcomeKind.CHECKLIST: "Checklist",
    WorkflowOutcomeKind.DOCUMENT_PACKET: "Document packet",
    WorkflowOutcomeKind.PROFESSIONAL_MEMO: "Professional memo",
    WorkflowOutcomeKind.SOURCE_BACKED_NOTE: "Source-backed note",
    WorkflowOutcomeKind.REMINDER: "Reminder",
    WorkflowOutcomeKind.EVIDENCE_NOTEBOOK_ENTRY: "Evidence notebook entry",
    WorkflowOutcomeKind.QUESTIONS_FOR_PROFESSIONAL: "Questions for a professional",
    WorkflowOutcomeKind.ORGANIZE_DOCUMENTS: "Organize documents",
    WorkflowOutcomeKind.EXPLAIN_CONCEPT: "Explain concept",
})

PROMOTION_KIND_LABELS: Mapping[PromotedArtifactKind, str] = MappingProxyType({
    PromotedArtifactKind.CHECKLIST: "Checklist",
    PromotedArtifactKind.DOCUMENT_PACKET: "Document packet",
    PromotedArtifactKind.PROFESSIONAL_MEMO: "Professional memo",
    PromotedArtifactKind.SOURCE_BACKED_NOTE: "Source-backed note",
    PromotedArtifactKind.REMINDER: "Reminder",
    PromotedArtifactKind.EVIDENCE_NOTEBOOK_ENTRY: "Evidence notebook entry",
    PromotedArtifactKind.MEETING_PREP_BRIEF: "Meeting prep brief",
})


def validate_professional_label_contract() -> None:
    """Fail closed when enum labels drift from runtime contracts.

    Raises:
        RuntimeError: If outcome or promotion labels drift from their runtime enum contracts.
    """
    missing_outcomes = set(WorkflowOutcomeKind) - set(OUTCOME_KIND_LABELS)
    extra_outcomes = set(OUTCOME_KIND_LABELS) - set(WorkflowOutcomeKind)
    missing_promotions = set(PromotedArtifactKind) - set(PROMOTION_KIND_LABELS)
    extra_promotions = set(PROMOTION_KIND_LABELS) - set(PromotedArtifactKind)
    if missing_outcomes or extra_outcomes or missing_promotions or extra_promotions:
        raise RuntimeError(
            "professional label contract drift: "
            f"missing_outcomes={sorted(item.value for item in missing_outcomes)} "
            f"extra_outcomes={sorted(item.value for item in extra_outcomes)} "
            f"missing_promotions={sorted(item.value for item in missing_promotions)} "
            f"extra_promotions={sorted(item.value for item in extra_promotions)}"
        )


validate_professional_label_contract()

__all__ = [
    "OUTCOME_KIND_LABELS",
    "PROMOTION_KIND_LABELS",
    "validate_professional_label_contract",
]
