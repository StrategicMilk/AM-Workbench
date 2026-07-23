"""Casual-first Workbench conversation contract.

The default path is ordinary chat with no project setup. Durable or structured
actions are explicit routes and fail closed until project context, consent,
authority, and evidence are all present.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any
from uuid import uuid4

from vetinari.workbench.spine import validate_project_id


class ConversationMode(StrEnum):
    """Visible mode selected for the conversation surface."""

    CASUAL = "casual"
    CHAT = "chat"
    RESEARCH = "research"
    WRITING = "writing"
    CREATIVE_WRITING = "creative_writing"


class ConversationRouteKind(StrEnum):
    """Action classes available from the casual-first front door."""

    CONTINUE_CASUAL = "continue_casual"
    SAVE_TRANSCRIPT = "save_transcript"
    PROMOTE_TO_PLAN = "promote_to_plan"
    PROMOTE_TO_EVIDENCE_NOTEBOOK = "promote_to_evidence_notebook"
    ATTACH_WORKSPACE = "attach_workspace"


class ConversationRoutingRejected(ValueError):
    """Raised when a structured conversation route lacks required proof."""


@dataclass(frozen=True, slots=True)
class WorkbenchConversationMessage:
    """One message in a conversation branch."""

    message_id: str
    role: str
    content: str
    branch_id: str
    created_at_utc: str
    evidence_refs: tuple[str, ...] = ()
    authority_ref: str | None = None

    def __post_init__(self) -> None:
        _require_non_empty(self.message_id, "message_id")
        if self.role not in {"user", "assistant", "system"}:
            raise ConversationRoutingRejected("message role must be user, assistant, or system")
        _require_non_empty(self.content, "content")
        _require_non_empty(self.branch_id, "branch_id")
        _require_non_empty(self.created_at_utc, "created_at_utc")
        _require_string_tuple(self.evidence_refs, "evidence_refs", allow_empty=True)
        if self.authority_ref is not None:
            _require_non_empty(self.authority_ref, "authority_ref")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"WorkbenchConversationMessage(message_id={self.message_id!r}, role={self.role!r}, content={self.content!r})"


@dataclass(frozen=True, slots=True)
class ConversationBranch:
    """Branch metadata for a casual or structured conversation."""

    branch_id: str
    title: str
    parent_branch_id: str | None
    message_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_non_empty(self.branch_id, "branch_id")
        _require_non_empty(self.title, "title")
        if self.parent_branch_id is not None:
            _require_non_empty(self.parent_branch_id, "parent_branch_id")
        _require_string_tuple(self.message_ids, "message_ids")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"ConversationBranch(branch_id={self.branch_id!r}, title={self.title!r}, parent_branch_id={self.parent_branch_id!r})"


@dataclass(frozen=True, slots=True)
class WorkspaceBinding:
    """Optional binding to the typed Workbench spine and project context."""

    workspace_id: str
    project_id: str
    spine_object_refs: tuple[str, ...]
    bound_by_authority_ref: str
    consent_ref: str
    evidence_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_non_empty(self.workspace_id, "workspace_id")
        object.__setattr__(self, "project_id", validate_project_id(self.project_id))
        _require_string_tuple(self.spine_object_refs, "spine_object_refs", allow_empty=True)
        _require_non_empty(self.bound_by_authority_ref, "bound_by_authority_ref")
        _require_non_empty(self.consent_ref, "consent_ref")
        _require_string_tuple(self.evidence_refs, "evidence_refs")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"WorkspaceBinding(workspace_id={self.workspace_id!r}, project_id={self.project_id!r}, spine_object_refs={self.spine_object_refs!r})"


@dataclass(frozen=True, slots=True)
class ConversationAffordance:
    """Non-invasive action exposed beside a conversation."""

    kind: ConversationRouteKind
    label: str
    enabled: bool
    requires_consent: bool
    requires_project_context: bool
    requires_authority: bool
    requires_evidence: bool
    blocked_reason: str | None = None

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"ConversationAffordance(kind={self.kind!r}, label={self.label!r}, enabled={self.enabled!r})"


@dataclass(frozen=True, slots=True)
class Conversation:
    """Conversation object consumed by Python callers and the Svelte shell."""

    conversation_id: str
    active_mode: ConversationMode
    active_branch_id: str
    messages: tuple[WorkbenchConversationMessage, ...]
    branches: tuple[ConversationBranch, ...]
    workspace_binding: WorkspaceBinding | None
    state: Mapping[str, Any] = field(default_factory=dict)
    affordances: tuple[ConversationAffordance, ...] = ()

    def __post_init__(self) -> None:
        _require_non_empty(self.conversation_id, "conversation_id")
        if not self.messages:
            raise ConversationRoutingRejected("conversation requires at least one message")
        if not self.branches:
            raise ConversationRoutingRejected("conversation requires at least one branch")
        branch_ids = {branch.branch_id for branch in self.branches}
        if self.active_branch_id not in branch_ids:
            raise ConversationRoutingRejected("active branch is not present")
        message_ids = {message.message_id for message in self.messages}
        for branch in self.branches:
            missing = set(branch.message_ids) - message_ids
            if missing:
                raise ConversationRoutingRejected(f"branch {branch.branch_id!r} references missing messages")

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable payload."""
        return _jsonify(asdict(self))

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"Conversation(conversation_id={self.conversation_id!r}, active_mode={self.active_mode!r}, active_branch_id={self.active_branch_id!r})"


@dataclass(frozen=True, slots=True)
class ConversationSafetyContext:
    """Fail-closed context for durable or structured routes."""

    consent_ref: str | None = None
    authority_ref: str | None = None
    project_id: str | None = None
    evidence_refs: tuple[str, ...] = ()

    def require_structured(self) -> WorkspaceBinding:
        """Return a workspace binding or reject the route with a precise reason.

        Returns:
            WorkspaceBinding value produced by require_structured().

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        if not self.consent_ref:
            raise ConversationRoutingRejected("conversation route requires explicit user consent")
        if not self.authority_ref:
            raise ConversationRoutingRejected("conversation route requires authority context")
        if not self.project_id:
            raise ConversationRoutingRejected("conversation route requires project context")
        if not self.evidence_refs:
            raise ConversationRoutingRejected("conversation route requires evidence refs")
        project_id = validate_project_id(self.project_id)
        return WorkspaceBinding(
            workspace_id=f"workspace-{project_id}",
            project_id=project_id,
            spine_object_refs=(),
            bound_by_authority_ref=self.authority_ref,
            consent_ref=self.consent_ref,
            evidence_refs=self.evidence_refs,
        )

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"ConversationSafetyContext(consent_ref={self.consent_ref!r}, authority_ref={self.authority_ref!r}, project_id={self.project_id!r})"


@dataclass(frozen=True, slots=True)
class ConversationRouteRequest:
    """Request to continue, save, promote, or bind a conversation."""

    route_kind: ConversationRouteKind
    user_text: str
    mode: ConversationMode = ConversationMode.CASUAL
    safety_context: ConversationSafetyContext = field(default_factory=ConversationSafetyContext)

    def __post_init__(self) -> None:
        _require_non_empty(self.user_text, "user_text")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"ConversationRouteRequest(route_kind={self.route_kind!r}, user_text={self.user_text!r}, mode={self.mode!r})"


@dataclass(frozen=True, slots=True)
class ConversationRouteDecision:
    """Result of a validated conversation route."""

    route_kind: ConversationRouteKind
    conversation: Conversation
    status: str
    requires_worker: bool
    promotion_target: str | None

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"ConversationRouteDecision(route_kind={self.route_kind!r}, conversation={self.conversation!r}, status={self.status!r})"


def build_casual_conversation(
    user_text: str,
    *,
    mode: ConversationMode = ConversationMode.CASUAL,
    safety_context: ConversationSafetyContext | None = None,
) -> Conversation:
    """Build the casual front door without requiring project setup.

    Returns:
        Newly constructed casual conversation value.
    """
    _require_non_empty(user_text, "user_text")
    context = safety_context or ConversationSafetyContext()
    now = _utc_now_iso()
    branch_id = "main"
    message = WorkbenchConversationMessage(
        message_id=f"msg-{uuid4().hex}",
        role="user",
        content=user_text,
        branch_id=branch_id,
        created_at_utc=now,
        evidence_refs=context.evidence_refs,
        authority_ref=context.authority_ref,
    )
    branch = ConversationBranch(
        branch_id=branch_id,
        title=_title_from_text(user_text),
        parent_branch_id=None,
        message_ids=(message.message_id,),
    )
    binding = _optional_binding(context)
    return Conversation(
        conversation_id=f"conv-{uuid4().hex}",
        active_mode=mode,
        active_branch_id=branch_id,
        messages=(message,),
        branches=(branch,),
        workspace_binding=binding,
        state={
            "casual_first": True,
            "trace_console_visible": False,
            "acceptance_flow_required": False,
        },
        affordances=_affordances(context),
    )


def route_conversation_request(request: ConversationRouteRequest) -> ConversationRouteDecision:
    """Validate and route a conversation action.

    Returns:
        Outcome produced by route_conversation_request().
    """
    if request.route_kind == ConversationRouteKind.CONTINUE_CASUAL:
        conversation = build_casual_conversation(
            request.user_text,
            mode=request.mode,
            safety_context=request.safety_context,
        )
        return ConversationRouteDecision(
            route_kind=request.route_kind,
            conversation=conversation,
            status="ready",
            requires_worker=False,
            promotion_target=None,
        )

    binding = request.safety_context.require_structured()
    conversation = build_casual_conversation(
        request.user_text,
        mode=request.mode if request.mode != ConversationMode.CASUAL else ConversationMode.CHAT,
        safety_context=request.safety_context,
    )
    conversation = Conversation(
        conversation_id=conversation.conversation_id,
        active_mode=conversation.active_mode,
        active_branch_id=conversation.active_branch_id,
        messages=conversation.messages,
        branches=conversation.branches,
        workspace_binding=binding,
        state={**conversation.state, "structured_route": request.route_kind.value},
        affordances=_affordances(request.safety_context),
    )
    return ConversationRouteDecision(
        route_kind=request.route_kind,
        conversation=conversation,
        status="queued",
        requires_worker=True,
        promotion_target=_promotion_target(request.route_kind),
    )


def _affordances(context: ConversationSafetyContext) -> tuple[ConversationAffordance, ...]:
    missing = _missing_context(context)
    structured_enabled = not missing
    blocked_reason = None if structured_enabled else "Missing " + ", ".join(missing)
    return (
        ConversationAffordance(
            kind=ConversationRouteKind.CONTINUE_CASUAL,
            label="Continue",
            enabled=True,
            requires_consent=False,
            requires_project_context=False,
            requires_authority=False,
            requires_evidence=False,
        ),
        ConversationAffordance(
            kind=ConversationRouteKind.SAVE_TRANSCRIPT,
            label="Save",
            enabled=structured_enabled,
            requires_consent=True,
            requires_project_context=True,
            requires_authority=True,
            requires_evidence=True,
            blocked_reason=blocked_reason,
        ),
        ConversationAffordance(
            kind=ConversationRouteKind.PROMOTE_TO_PLAN,
            label="Promote",
            enabled=structured_enabled,
            requires_consent=True,
            requires_project_context=True,
            requires_authority=True,
            requires_evidence=True,
            blocked_reason=blocked_reason,
        ),
        ConversationAffordance(
            kind=ConversationRouteKind.PROMOTE_TO_EVIDENCE_NOTEBOOK,
            label="Evidence",
            enabled=structured_enabled,
            requires_consent=True,
            requires_project_context=True,
            requires_authority=True,
            requires_evidence=True,
            blocked_reason=blocked_reason,
        ),
        ConversationAffordance(
            kind=ConversationRouteKind.ATTACH_WORKSPACE,
            label="Attach workspace",
            enabled=structured_enabled,
            requires_consent=True,
            requires_project_context=True,
            requires_authority=True,
            requires_evidence=True,
            blocked_reason=blocked_reason,
        ),
    )


def _optional_binding(context: ConversationSafetyContext) -> WorkspaceBinding | None:
    if _missing_context(context):
        return None
    return context.require_structured()


def _missing_context(context: ConversationSafetyContext) -> tuple[str, ...]:
    missing: list[str] = []
    if not context.consent_ref:
        missing.append("consent")
    if not context.authority_ref:
        missing.append("authority")
    if not context.project_id:
        missing.append("project context")
    if not context.evidence_refs:
        missing.append("evidence")
    return tuple(missing)


def _promotion_target(route_kind: ConversationRouteKind) -> str | None:
    if route_kind == ConversationRouteKind.PROMOTE_TO_PLAN:
        return "plan"
    if route_kind == ConversationRouteKind.PROMOTE_TO_EVIDENCE_NOTEBOOK:
        return "evidence_notebook"
    if route_kind == ConversationRouteKind.SAVE_TRANSCRIPT:
        return "conversation_transcript"
    if route_kind == ConversationRouteKind.ATTACH_WORKSPACE:
        return "workspace"
    return None


def _title_from_text(text: str) -> str:
    return text.strip().splitlines()[0][:64] or "Conversation"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _require_non_empty(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ConversationRoutingRejected(f"{field_name} must be non-empty")


def _require_string_tuple(values: tuple[str, ...], field_name: str, *, allow_empty: bool = False) -> None:
    if not isinstance(values, tuple):
        raise ConversationRoutingRejected(f"{field_name} must be a tuple")
    if not allow_empty and not values:
        raise ConversationRoutingRejected(f"{field_name} must contain non-empty strings")
    if not all(isinstance(value, str) and value.strip() for value in values):
        raise ConversationRoutingRejected(f"{field_name} must contain non-empty strings")


def _jsonify(value: Any) -> Any:
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, tuple):
        return [_jsonify(item) for item in value]
    if isinstance(value, list):
        return [_jsonify(item) for item in value]
    if isinstance(value, dict):
        return {key: _jsonify(item) for key, item in value.items()}
    return value
