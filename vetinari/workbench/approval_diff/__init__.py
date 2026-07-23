"""Governed approval diff contract for Workbench promotions."""

from __future__ import annotations

from vetinari.workbench.approval_diff.runtime import (
    APPROVAL_DIFF_REQUIRED_DIMENSIONS,
    ApprovalDiff,
    ApprovalDiffDecision,
    ApprovalDiffEvidenceRef,
    ApprovalDiffGate,
    ApprovalDiffRejected,
    ApprovalDiffReview,
    ApprovalDiffStatus,
    ApprovalDiffTarget,
    DiffDimension,
    DiffEntry,
    build_approval_diff,
    build_approval_diff_from_proposal,
    evaluate_approval_diff,
    require_governed_promotion_review,
)

__all__ = [
    "APPROVAL_DIFF_REQUIRED_DIMENSIONS",
    "ApprovalDiff",
    "ApprovalDiffDecision",
    "ApprovalDiffEvidenceRef",
    "ApprovalDiffGate",
    "ApprovalDiffRejected",
    "ApprovalDiffReview",
    "ApprovalDiffStatus",
    "ApprovalDiffTarget",
    "DiffDimension",
    "DiffEntry",
    "build_approval_diff",
    "build_approval_diff_from_proposal",
    "evaluate_approval_diff",
    "require_governed_promotion_review",
]
