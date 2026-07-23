"""Runtime types and state machine for Workbench artifact reviews."""

from __future__ import annotations

import hashlib
import re
import threading
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from vetinari.security.path_canonicalizer import ProjectIdRejected as SharedProjectIdRejected
from vetinari.security.path_canonicalizer import canonicalize_project_id as _shared_canonicalize_project_id

from .diff import ArtifactDiff, compute_artifact_diff

_ARTIFACT_ID_RE: re.Pattern[str] = re.compile(r"[A-Za-z0-9_.-]{1,128}")
_ARTIFACT_TRAVERSAL_MARKERS: tuple[str, ...] = ("/", "\\", "..", "\x00", " ", ";")


class ArtifactReviewProjectIdRejected(ValueError):
    """Raised when an inbound project id is not canonical."""

    def __init__(self, value: object) -> None:
        super().__init__(f"invalid project_id {value!r}; use [A-Za-z0-9_-] up to 64 characters")
        self.value = value


class ArtifactReviewArtifactIdRejected(ValueError):
    """Raised when an inbound artifact id is not canonical."""

    def __init__(self, value: object) -> None:
        super().__init__(f"invalid artifact_id {value!r}; use [A-Za-z0-9_.-] up to 128 characters")
        self.value = value


class ArtifactReviewError(Exception):
    """Raised when an artifact review transition cannot be completed safely."""


class ReviewState(str, Enum):
    """Artifact review state machine."""

    PENDING = "PENDING"
    LINT_RUNNING = "LINT_RUNNING"
    LINT_PASSED = "LINT_PASSED"
    LINT_FAILED = "LINT_FAILED"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    BLOCKED_BY_LINT = "BLOCKED_BY_LINT"


class RiskTag(str, Enum):
    """Domain-aware risk tags surfaced by lint findings."""

    OFF_POLICY_CLAIM = "OFF_POLICY_CLAIM"
    MISSING_PROVENANCE = "MISSING_PROVENANCE"
    WEAK_ACCESSIBILITY_METADATA = "WEAK_ACCESSIBILITY_METADATA"
    STALE_REFERENCE = "STALE_REFERENCE"
    BRAND_STYLE_VIOLATION = "BRAND_STYLE_VIOLATION"
    MISSING_EVIDENCE_ANCHOR = "MISSING_EVIDENCE_ANCHOR"
    BROKEN_LINK = "BROKEN_LINK"


class LintSeverity(str, Enum):
    """Severity levels ordered by policy strictness."""

    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    BLOCKER = "BLOCKER"


@dataclass(frozen=True, slots=True)
class ArtifactReviewLintFinding:
    """One lint finding attached to an artifact review."""

    rule_id: str
    severity: LintSeverity
    risk_tags: tuple[RiskTag, ...]
    message: str
    location: str

    def __post_init__(self) -> None:
        _require_non_empty(self.rule_id, "rule_id")
        _require_non_empty(self.message, "message")
        if not isinstance(self.severity, LintSeverity):
            raise ArtifactReviewError("lint finding severity must be a LintSeverity")
        if any(not isinstance(tag, RiskTag) for tag in self.risk_tags):
            raise ArtifactReviewError("lint finding risk_tags must contain only RiskTag values")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"ArtifactReviewLintFinding(rule_id={self.rule_id!r}, severity={self.severity!r}, risk_tags={self.risk_tags!r})"

    def to_dict(self) -> dict[str, Any]:
        """Return the artifact-review API JSON contract for this lint finding."""
        return {
            "rule_id": self.rule_id,
            "severity": self.severity.value,
            "risk_tags": [tag.value for tag in self.risk_tags],
            "message": self.message,
            "location": self.location,
        }


@dataclass(frozen=True, slots=True)
class ArtifactReview:
    """Reviewable before/after artifact mutation."""

    review_id: str
    project_id: str
    subject_id: str
    kind: str
    before_artifact: Any
    after_artifact: Any
    diff: ArtifactDiff
    lint_findings: tuple[ArtifactReviewLintFinding, ...]
    risk_tags: tuple[RiskTag, ...]
    review_state: ReviewState
    requested_at_utc: str
    decided_at_utc: str | None = None
    decided_by: str | None = None
    rationale: str | None = None

    def __post_init__(self) -> None:
        _canonicalize_project_id(self.project_id)
        _canonicalize_artifact_id(self.subject_id)
        _require_non_empty(self.kind, "kind")
        _require_non_empty(self.review_id, "review_id")
        if not isinstance(self.review_state, ReviewState):
            raise ArtifactReviewError("review_state must be a ReviewState")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"ArtifactReview(review_id={self.review_id!r}, project_id={self.project_id!r}, subject_id={self.subject_id!r})"

    def to_dict(
        self,
        *,
        before_artifact: Any | None = None,
        after_artifact: Any | None = None,
    ) -> dict[str, Any]:
        """Return the artifact-review API JSON contract for this review."""
        return {
            "review_id": self.review_id,
            "project_id": self.project_id,
            "subject_id": self.subject_id,
            "kind": self.kind,
            "before_artifact": self.before_artifact if before_artifact is None else before_artifact,
            "after_artifact": self.after_artifact if after_artifact is None else after_artifact,
            "diff": self.diff.to_dict(),
            "lint_findings": [finding.to_dict() for finding in self.lint_findings],
            "risk_tags": [tag.value for tag in self.risk_tags],
            "review_state": self.review_state.value,
            "requested_at_utc": self.requested_at_utc,
            "decided_at_utc": self.decided_at_utc,
            "decided_by": self.decided_by,
            "rationale": self.rationale,
        }


class ArtifactReviewService:
    """In-process artifact review service used by the API boundary."""

    def __init__(self) -> None:
        self._reviews: dict[str, ArtifactReview] = {}
        self._lock = threading.Lock()

    def start_review(
        self,
        *,
        project_id: str,
        subject_id: str,
        kind: str,
        before_artifact: Any,
        after_artifact: Any,
    ) -> ArtifactReview:
        """Create a pending review with a populated deterministic diff.

        Returns:
            ArtifactReview value produced by start_review().
        """
        canonical_project = _canonicalize_project_id(project_id)
        canonical_subject = _canonicalize_artifact_id(subject_id)
        diff = compute_artifact_diff(
            before=before_artifact,
            after=after_artifact,
            subject_id=canonical_subject,
            kind=kind,
        )
        review = ArtifactReview(
            review_id=_review_id(canonical_project, canonical_subject, kind, diff),
            project_id=canonical_project,
            subject_id=canonical_subject,
            kind=kind,
            before_artifact=before_artifact,
            after_artifact=after_artifact,
            diff=diff,
            lint_findings=(),
            risk_tags=(),
            review_state=ReviewState.PENDING,
            requested_at_utc=_now_utc(),
        )
        self._store(review)
        return review

    def attach_lint_findings(
        self,
        review: ArtifactReview,
        findings: tuple[ArtifactReviewLintFinding, ...],
        *,
        block_on_severity: LintSeverity,
    ) -> ArtifactReview:
        """Attach findings and transition to a fail-closed lint state.

        Args:
            review: Review value consumed by attach_lint_findings().
            findings: Findings value consumed by attach_lint_findings().
            block_on_severity: Block on severity value consumed by attach_lint_findings().

        Returns:
            ArtifactReview value produced by attach_lint_findings().
        """
        if any(_severity_rank(finding.severity) >= _severity_rank(block_on_severity) for finding in findings):
            state = ReviewState.BLOCKED_BY_LINT
        elif any(_severity_rank(finding.severity) >= _severity_rank(LintSeverity.ERROR) for finding in findings):
            state = ReviewState.LINT_FAILED
        else:
            state = ReviewState.LINT_PASSED
        updated = replace(
            review,
            lint_findings=tuple(findings),
            risk_tags=tuple(dict.fromkeys(tag for finding in findings for tag in finding.risk_tags)),
            review_state=state,
        )
        self._store(updated)
        return updated

    def get_review(self, review_id: str) -> ArtifactReview | None:
        """Return one review by id.

        Returns:
            Resolved review value.
        """
        with self._lock:
            return self._reviews.get(review_id)

    def approve(self, review: ArtifactReview, *, decided_by: str, rationale: str) -> ArtifactReview:
        """Approve a review unless lint has blocked the mutation.

        Returns:
            ArtifactReview value produced by approve().

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        _require_non_empty(decided_by, "decided_by")
        _require_non_empty(rationale, "rationale")
        if review.review_state is not ReviewState.LINT_PASSED:
            raise ArtifactReviewError("review must pass lint before approval")
        updated = replace(
            review,
            review_state=ReviewState.APPROVED,
            decided_at_utc=_now_utc(),
            decided_by=decided_by,
            rationale=rationale,
        )
        self._store(updated)
        return updated

    def reject(self, review: ArtifactReview, *, decided_by: str, rationale: str) -> ArtifactReview:
        """Reject a review with an operator rationale.

        Returns:
            ArtifactReview value produced by reject().
        """
        _require_non_empty(decided_by, "decided_by")
        _require_non_empty(rationale, "rationale")
        updated = replace(
            review,
            review_state=ReviewState.REJECTED,
            decided_at_utc=_now_utc(),
            decided_by=decided_by,
            rationale=rationale,
        )
        self._store(updated)
        return updated

    def raw_artifact(self, review: ArtifactReview, *, side: str) -> Any:
        """Return the original before or after payload unchanged.

        Returns:
            Any value produced by raw_artifact().

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        if side == "before":
            return review.before_artifact
        if side == "after":
            return review.after_artifact
        raise ArtifactReviewError(f"unknown raw artifact side {side!r}")

    def _store(self, review: ArtifactReview) -> None:
        with self._lock:
            self._reviews[review.review_id] = review


def _canonicalize_project_id(value: str | None) -> str:
    try:
        return _shared_canonicalize_project_id(value)
    except SharedProjectIdRejected as exc:
        raise ArtifactReviewProjectIdRejected(value) from exc


def _canonicalize_artifact_id(value: str | None) -> str:
    if not isinstance(value, str):
        raise ArtifactReviewArtifactIdRejected(value)
    if not value or len(value) > 128 or _ARTIFACT_ID_RE.fullmatch(value) is None:
        raise ArtifactReviewArtifactIdRejected(value)
    if value in {".", ".."} or any(marker in value for marker in _ARTIFACT_TRAVERSAL_MARKERS):
        raise ArtifactReviewArtifactIdRejected(value)
    return value


def _severity_rank(severity: LintSeverity) -> int:
    return {
        LintSeverity.INFO: 0,
        LintSeverity.WARNING: 1,
        LintSeverity.ERROR: 2,
        LintSeverity.BLOCKER: 3,
    }[severity]


def _require_non_empty(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ArtifactReviewError(f"{field_name} must be non-empty")


def _review_id(project_id: str, subject_id: str, kind: str, diff: ArtifactDiff) -> str:
    seed = f"{project_id}:{subject_id}:{kind}:{diff.before_signature}:{diff.after_signature}"
    return "artifact-review-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:24]


def _now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


__all__ = [
    "ArtifactReview",
    "ArtifactReviewArtifactIdRejected",
    "ArtifactReviewError",
    "ArtifactReviewLintFinding",
    "ArtifactReviewProjectIdRejected",
    "ArtifactReviewService",
    "LintSeverity",
    "ReviewState",
    "RiskTag",
]
