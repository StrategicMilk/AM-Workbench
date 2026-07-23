"""General writing mode contract for AM Workbench."""

from __future__ import annotations

from dataclasses import dataclass

WRITING_TEMPLATE_ID = "writing"
WRITING_REQUIRED_ARTIFACTS = (
    "audience_style_template",
    "draft_branch",
    "redline_plan",
    "fact_check_ledger",
    "export_manifest",
)


class WritingModeRejected(ValueError):
    """Raised when a writing draft lacks review proof."""


@dataclass(frozen=True, slots=True)
class AudienceStyleTemplate:
    """Audience and style constraints for a writing draft."""

    audience: str
    tone: str
    reading_level: str
    prohibited_claims: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_non_empty(self.audience, "audience")
        _require_non_empty(self.tone, "tone")
        _require_non_empty(self.reading_level, "reading_level")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"AudienceStyleTemplate(audience={self.audience!r}, tone={self.tone!r}, reading_level={self.reading_level!r})"


@dataclass(frozen=True, slots=True)
class RedlineItem:
    """One proposed writing edit with a reason."""

    target_span: str
    replacement: str
    reason: str

    def __post_init__(self) -> None:
        _require_non_empty(self.target_span, "target_span")
        _require_non_empty(self.replacement, "replacement")
        _require_non_empty(self.reason, "reason")


@dataclass(frozen=True, slots=True)
class FactCheckItem:
    """A factual claim and the evidence backing it."""

    claim: str
    evidence_refs: tuple[str, ...]
    status: str

    def __post_init__(self) -> None:
        _require_non_empty(self.claim, "claim")
        _require_non_empty_tuple(self.evidence_refs, "evidence_refs")
        _require_non_empty(self.status, "status")


@dataclass(frozen=True, slots=True)
class WritingModeState:
    """Promotion-ready writing workspace state."""

    style_template: AudienceStyleTemplate
    draft_branch_id: str
    redlines: tuple[RedlineItem, ...]
    fact_checks: tuple[FactCheckItem, ...]
    export_targets: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_non_empty(self.draft_branch_id, "draft_branch_id")
        _require_non_empty_tuple(self.export_targets, "export_targets")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"WritingModeState(style_template={self.style_template!r}, draft_branch_id={self.draft_branch_id!r}, redlines={self.redlines!r})"


def require_writing_ready(state: WritingModeState) -> None:
    """Reject writing output with unresolved fact-check or export gaps.

    Raises:
        Exception: Propagates validation or runtime failures from the underlying operation.
    """
    unresolved = [item.claim for item in state.fact_checks if item.status != "verified"]
    if unresolved:
        raise WritingModeRejected(f"unverified factual claims: {unresolved}")
    if not state.redlines:
        raise WritingModeRejected("writing mode requires at least one redline review item")


def _require_non_empty(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise WritingModeRejected(f"{field_name} must be non-empty")


def _require_non_empty_tuple(values: tuple[str, ...], field_name: str) -> None:
    if (
        not isinstance(values, tuple)
        or not values
        or not all(isinstance(value, str) and value.strip() for value in values)
    ):
        raise WritingModeRejected(f"{field_name} must contain non-empty strings")


__all__ = [
    "WRITING_REQUIRED_ARTIFACTS",
    "WRITING_TEMPLATE_ID",
    "AudienceStyleTemplate",
    "FactCheckItem",
    "RedlineItem",
    "WritingModeRejected",
    "WritingModeState",
    "require_writing_ready",
]
