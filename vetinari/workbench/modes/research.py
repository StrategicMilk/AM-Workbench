"""Research mode contract for AM Workbench."""

from __future__ import annotations

from dataclasses import dataclass

RESEARCH_TEMPLATE_ID = "research"
RESEARCH_REQUIRED_ARTIFACTS = (
    "research_brief",
    "source_plan",
    "claim_ledger",
    "contradiction_log",
    "freshness_checks",
    "confidence_summary",
)


class ResearchModeRejected(ValueError):
    """Raised when a research mode state cannot be promoted."""


@dataclass(frozen=True, slots=True)
class SourcePlanItem:
    """A planned research source with freshness and citation obligations."""

    source_card_id: str
    question: str
    required_freshness: str
    citation_required: bool

    def __post_init__(self) -> None:
        _require_non_empty(self.source_card_id, "source_card_id")
        _require_non_empty(self.question, "question")
        _require_non_empty(self.required_freshness, "required_freshness")
        if self.citation_required is not True:
            raise ResearchModeRejected("research sources must require citations")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"SourcePlanItem(source_card_id={self.source_card_id!r}, question={self.question!r}, required_freshness={self.required_freshness!r})"


@dataclass(frozen=True, slots=True)
class ClaimLedgerEntry:
    """A sourced claim and its current confidence."""

    claim_id: str
    statement: str
    source_card_ids: tuple[str, ...]
    confidence: float
    freshness_passed: bool

    def __post_init__(self) -> None:
        _require_non_empty(self.claim_id, "claim_id")
        _require_non_empty(self.statement, "statement")
        _require_non_empty_tuple(self.source_card_ids, "source_card_ids")
        if not 0 <= self.confidence <= 1:
            raise ResearchModeRejected("confidence must be between 0 and 1")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"ClaimLedgerEntry(claim_id={self.claim_id!r}, statement={self.statement!r}, source_card_ids={self.source_card_ids!r})"


@dataclass(frozen=True, slots=True)
class ResearchContradictionRecord:
    """A contradiction that must remain visible in the brief."""

    claim_id: str
    conflicting_source_card_ids: tuple[str, ...]
    summary: str
    resolution_status: str

    def __post_init__(self) -> None:
        _require_non_empty(self.claim_id, "claim_id")
        _require_non_empty_tuple(self.conflicting_source_card_ids, "conflicting_source_card_ids")
        _require_non_empty(self.summary, "summary")
        _require_non_empty(self.resolution_status, "resolution_status")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"ResearchContradictionRecord(claim_id={self.claim_id!r}, conflicting_source_card_ids={self.conflicting_source_card_ids!r}, summary={self.summary!r})"


@dataclass(frozen=True, slots=True)
class ResearchModeState:
    """Promotion-ready research workspace state."""

    brief: str
    source_plan: tuple[SourcePlanItem, ...]
    claim_ledger: tuple[ClaimLedgerEntry, ...]
    contradictions: tuple[ResearchContradictionRecord, ...]
    rerun_diff_ref: str

    def __post_init__(self) -> None:
        _require_non_empty(self.brief, "brief")
        _require_non_empty_sequence(self.source_plan, "source_plan")
        _require_non_empty_sequence(self.claim_ledger, "claim_ledger")
        _require_non_empty(self.rerun_diff_ref, "rerun_diff_ref")

    @property
    def confidence_summary(self) -> str:
        average = sum(claim.confidence for claim in self.claim_ledger) / len(self.claim_ledger)
        stale = sum(1 for claim in self.claim_ledger if not claim.freshness_passed)
        return f"average={average:.2f}; stale_or_unverified={stale}"

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"ResearchModeState(brief={self.brief!r}, source_plan={self.source_plan!r}, claim_ledger={self.claim_ledger!r})"


def require_research_ready(state: ResearchModeState) -> None:
    """Reject research output missing source, freshness, or contradiction proof.

    Raises:
        Exception: Propagates validation or runtime failures from the underlying operation.
    """
    planned_sources = {source.source_card_id for source in state.source_plan}
    for claim in state.claim_ledger:
        missing_sources = set(claim.source_card_ids) - planned_sources
        if missing_sources:
            raise ResearchModeRejected(f"claim references unplanned sources: {sorted(missing_sources)}")
        if not claim.freshness_passed:
            raise ResearchModeRejected(f"claim {claim.claim_id!r} lacks a passing freshness check")
    contradiction_claims = {record.claim_id for record in state.contradictions}
    if state.contradictions and not contradiction_claims <= {claim.claim_id for claim in state.claim_ledger}:
        raise ResearchModeRejected("contradiction log references unknown claims")


def _require_non_empty(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ResearchModeRejected(f"{field_name} must be non-empty")


def _require_non_empty_tuple(values: tuple[str, ...], field_name: str) -> None:
    if (
        not isinstance(values, tuple)
        or not values
        or not all(isinstance(value, str) and value.strip() for value in values)
    ):
        raise ResearchModeRejected(f"{field_name} must contain non-empty strings")


def _require_non_empty_sequence(values: tuple[object, ...], field_name: str) -> None:
    if not isinstance(values, tuple) or not values:
        raise ResearchModeRejected(f"{field_name} must be a non-empty tuple")


__all__ = [
    "RESEARCH_REQUIRED_ARTIFACTS",
    "RESEARCH_TEMPLATE_ID",
    "ClaimLedgerEntry",
    "ResearchContradictionRecord",
    "ResearchModeRejected",
    "ResearchModeState",
    "SourcePlanItem",
    "require_research_ready",
]
