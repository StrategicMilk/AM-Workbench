"""Competitive drift watch for evidence-backed Workbench gap proposals."""

from __future__ import annotations

from vetinari.workbench.competitive_drift.runtime import (
    CompetitiveBaseline,
    CompetitiveDriftError,
    CompetitiveGapEvidence,
    DriftProposal,
    DriftProposalKind,
    SourceFreshness,
    TaskSuiteResult,
    create_drift_proposal,
    record_competitive_gap,
)

__all__ = [
    "CompetitiveBaseline",
    "CompetitiveDriftError",
    "CompetitiveGapEvidence",
    "DriftProposal",
    "DriftProposalKind",
    "SourceFreshness",
    "TaskSuiteResult",
    "create_drift_proposal",
    "record_competitive_gap",
]
