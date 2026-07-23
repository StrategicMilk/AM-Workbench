"""Side-effect-free collaboration records and audit views for Workbench.

The first pass is deliberately local-first: callers pass explicit project,
member, review, comment, decision, approval, and queue records. This module
does not persist tenancy state or register routes; it creates accountability
views that card and export services can embed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_TRAVERSAL_MARKERS = ("/", "\\", "..", "\x00")


class CollaborationError(ValueError):
    """Raised when collaboration metadata cannot be trusted."""


class CollaborationRole(str, Enum):
    """Project member roles used by local-first governance views."""

    OWNER = "owner"
    REVIEWER = "reviewer"
    CONTRIBUTOR = "contributor"
    VIEWER = "viewer"


class ReviewStatus(str, Enum):
    """Review assignment state."""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    BLOCKED = "blocked"


class DecisionStatus(str, Enum):
    """Decision thread state."""

    OPEN = "open"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"


@dataclass(frozen=True, slots=True)
class CollaborationMember:
    """One optional project member in a local-first workspace."""

    member_id: str
    display_name: str
    role: CollaborationRole

    def __post_init__(self) -> None:
        _validate_id(self.member_id, "member_id")
        _require_non_empty(self.display_name, "display_name")
        if not isinstance(self.role, CollaborationRole):
            raise CollaborationError("role must be a CollaborationRole")


@dataclass(frozen=True, slots=True)
class ReviewAssignment:
    """A review request against an asset, proposal, export, or decision."""

    assignment_id: str
    target_id: str
    reviewer_id: str
    status: ReviewStatus
    requested_at_utc: str
    due_at_utc: str = ""
    rationale: str = ""

    def __post_init__(self) -> None:
        _validate_id(self.assignment_id, "assignment_id")
        _validate_id(self.target_id, "target_id")
        _validate_id(self.reviewer_id, "reviewer_id")
        _require_non_empty(self.requested_at_utc, "requested_at_utc")
        if not isinstance(self.status, ReviewStatus):
            raise CollaborationError("status must be a ReviewStatus")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"ReviewAssignment(assignment_id={self.assignment_id!r}, target_id={self.target_id!r}, reviewer_id={self.reviewer_id!r})"


@dataclass(frozen=True, slots=True)
class DecisionComment:
    """One human-readable comment in a decision thread."""

    comment_id: str
    author_id: str
    body: str
    created_at_utc: str

    def __post_init__(self) -> None:
        _validate_id(self.comment_id, "comment_id")
        _validate_id(self.author_id, "author_id")
        _require_non_empty(self.body, "body")
        _require_non_empty(self.created_at_utc, "created_at_utc")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"DecisionComment(comment_id={self.comment_id!r}, author_id={self.author_id!r}, body={self.body!r})"


@dataclass(frozen=True, slots=True)
class ApprovalRecord:
    """Immutable approval-history row for audit and export templates."""

    record_id: str
    target_id: str
    reviewer_id: str
    status: ReviewStatus
    decided_at_utc: str
    rationale: str

    def __post_init__(self) -> None:
        _validate_id(self.record_id, "record_id")
        _validate_id(self.target_id, "target_id")
        _validate_id(self.reviewer_id, "reviewer_id")
        _require_non_empty(self.decided_at_utc, "decided_at_utc")
        if not isinstance(self.status, ReviewStatus):
            raise CollaborationError("status must be a ReviewStatus")
        if self.status in {ReviewStatus.REJECTED, ReviewStatus.BLOCKED}:
            _require_non_empty(self.rationale, "rationale")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"ApprovalRecord(record_id={self.record_id!r}, target_id={self.target_id!r}, reviewer_id={self.reviewer_id!r})"


@dataclass(frozen=True, slots=True)
class DecisionThread:
    """A decision thread with comments and approval-history references."""

    thread_id: str
    target_id: str
    title: str
    status: DecisionStatus
    opened_at_utc: str
    comments: tuple[DecisionComment, ...] = ()
    approval_record_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _validate_id(self.thread_id, "thread_id")
        _validate_id(self.target_id, "target_id")
        _require_non_empty(self.title, "title")
        _require_non_empty(self.opened_at_utc, "opened_at_utc")
        if not isinstance(self.status, DecisionStatus):
            raise CollaborationError("status must be a DecisionStatus")
        for comment in self.comments:
            if not isinstance(comment, DecisionComment):
                raise CollaborationError("comments must contain DecisionComment instances")
        for record_id in self.approval_record_ids:
            _validate_id(record_id, "approval_record_ids entry")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"DecisionThread(thread_id={self.thread_id!r}, target_id={self.target_id!r}, title={self.title!r})"


@dataclass(frozen=True, slots=True)
class SharedQueueItem:
    """One item in a shared review/export queue."""

    queue_item_id: str
    target_id: str
    kind: str
    priority: int
    status: str
    assigned_to: str = ""
    risk_note: str = ""

    def __post_init__(self) -> None:
        _validate_id(self.queue_item_id, "queue_item_id")
        _validate_id(self.target_id, "target_id")
        _require_non_empty(self.kind, "kind")
        _require_non_empty(self.status, "status")
        if not 0 <= self.priority <= 100:
            raise CollaborationError("priority must be between 0 and 100")
        if self.assigned_to:
            _validate_id(self.assigned_to, "assigned_to")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return (
            f"SharedQueueItem(queue_item_id={self.queue_item_id!r}, target_id={self.target_id!r}, kind={self.kind!r})"
        )


@dataclass(frozen=True, slots=True)
class WorkbenchProject:
    """Local-first project/workspace container for governance metadata."""

    project_id: str
    name: str
    members: tuple[CollaborationMember, ...] = ()
    review_assignments: tuple[ReviewAssignment, ...] = ()
    decision_threads: tuple[DecisionThread, ...] = ()
    approval_history: tuple[ApprovalRecord, ...] = ()
    shared_queue: tuple[SharedQueueItem, ...] = ()

    def __post_init__(self) -> None:
        _validate_id(self.project_id, "project_id")
        _require_non_empty(self.name, "name")
        member_ids = {member.member_id for member in self.members}
        for review in self.review_assignments:
            if review.reviewer_id not in member_ids:
                raise CollaborationError(f"reviewer {review.reviewer_id!r} is not a project member")
        for approval in self.approval_history:
            if approval.reviewer_id not in member_ids:
                raise CollaborationError(f"approver {approval.reviewer_id!r} is not a project member")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"WorkbenchProject(project_id={self.project_id!r}, name={self.name!r}, members={self.members!r})"


@dataclass(frozen=True, slots=True)
class CollaborationAuditView:
    """Human audit view embedded in generated cards and export packages."""

    project_id: str
    member_roles: tuple[tuple[str, str], ...]
    pending_review_count: int
    blocked_review_count: int
    open_decision_count: int
    approval_history_count: int
    shared_queue_count: int
    queue_targets: tuple[str, ...]

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"CollaborationAuditView(project_id={self.project_id!r}, member_roles={self.member_roles!r}, pending_review_count={self.pending_review_count!r})"


class CollaborationBoard:
    """Build collaboration-light governance views from explicit project records."""

    def audit_view(self, project: WorkbenchProject) -> CollaborationAuditView:
        """Return a deterministic human audit summary for cards and exports.

        Returns:
            CollaborationAuditView value produced by audit_view().
        """
        pending = sum(1 for row in project.review_assignments if row.status is ReviewStatus.PENDING)
        blocked = sum(1 for row in project.review_assignments if row.status is ReviewStatus.BLOCKED)
        open_decisions = sum(1 for row in project.decision_threads if row.status is DecisionStatus.OPEN)
        return CollaborationAuditView(
            project_id=project.project_id,
            member_roles=tuple(sorted((member.member_id, member.role.value) for member in project.members)),
            pending_review_count=pending,
            blocked_review_count=blocked,
            open_decision_count=open_decisions,
            approval_history_count=len(project.approval_history),
            shared_queue_count=len(project.shared_queue),
            queue_targets=tuple(sorted(item.target_id for item in project.shared_queue)),
        )


def _require_non_empty(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise CollaborationError(f"{field_name} must be non-empty")


def _validate_id(value: str, field_name: str) -> None:
    _require_non_empty(value, field_name)
    if len(value) > 128 or _ID_RE.fullmatch(value) is None:
        raise CollaborationError(f"{field_name} contains forbidden characters")
    if any(marker in value for marker in _TRAVERSAL_MARKERS):
        raise CollaborationError(f"{field_name} contains traversal markers")


def normalize_collaboration_user_id(value: str) -> str:
    """Normalize a collaboration user id to its canonical lookup form (FSA-0397).

    Collaboration callers may supply user ids with surrounding whitespace
    or inconsistent case ("Alice", " alice ", "ALICE").  The
    PreferencesManager profile API and the per-user preferences store key
    on the normalized form so the same human resolves to the same bucket
    regardless of how the caller spells it.

    Args:
        value: Raw user id.

    Returns:
        Lower-case, stripped id.

    Raises:
        CollaborationError: If ``value`` is empty after stripping or
            contains characters that ``_validate_id`` rejects.
    """
    if not isinstance(value, str):
        raise CollaborationError("user_id must be a non-empty string")
    stripped = value.strip().lower()
    _validate_id(stripped, "user_id")
    return stripped
