"""Durable chat mode contract for AM Workbench."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

CHAT_TEMPLATE_ID = "chat"
CHAT_REQUIRED_ARTIFACTS = (
    "workspace_state",
    "branch_graph",
    "pinned_context",
    "conversion_manifest",
)


class ChatModeRejected(ValueError):
    """Raised when chat workspace state cannot be promoted."""


class ChatConversionKind(StrEnum):
    """Supported chat-to-artifact conversion targets."""

    PLAN = "plan"
    EVAL = "eval"
    DATASET = "dataset"
    PROMPT = "prompt"
    TEMPLATE = "template"
    EVIDENCE_NOTEBOOK = "evidence_notebook"


@dataclass(frozen=True, slots=True)
class ChatBranch:
    """Durable conversation branch metadata."""

    branch_id: str
    parent_branch_id: str | None
    title: str
    message_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_non_empty(self.branch_id, "branch_id")
        if self.parent_branch_id is not None:
            _require_non_empty(self.parent_branch_id, "parent_branch_id")
        _require_non_empty(self.title, "title")
        _require_non_empty_tuple(self.message_ids, "message_ids")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"ChatBranch(branch_id={self.branch_id!r}, parent_branch_id={self.parent_branch_id!r}, title={self.title!r})"


@dataclass(frozen=True, slots=True)
class PinnedContextRef:
    """Pinned context that must survive chat branch changes."""

    context_id: str
    source_ref: str
    reason: str

    def __post_init__(self) -> None:
        _require_non_empty(self.context_id, "context_id")
        _require_non_empty(self.source_ref, "source_ref")
        _require_non_empty(self.reason, "reason")


@dataclass(frozen=True, slots=True)
class ChatConversionAction:
    """An enabled chat conversion action."""

    kind: ChatConversionKind
    source_branch_id: str
    required_context_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_non_empty(self.source_branch_id, "source_branch_id")
        _require_non_empty_tuple(self.required_context_ids, "required_context_ids")


@dataclass(frozen=True, slots=True)
class ChatWorkspaceState:
    """Promotion-ready durable chat workspace state."""

    workspace_id: str
    active_branch_id: str
    branches: tuple[ChatBranch, ...]
    pinned_context: tuple[PinnedContextRef, ...]
    conversion_actions: tuple[ChatConversionAction, ...]

    def __post_init__(self) -> None:
        _require_non_empty(self.workspace_id, "workspace_id")
        _require_non_empty(self.active_branch_id, "active_branch_id")
        if not self.branches:
            raise ChatModeRejected("chat workspace requires at least one branch")
        if not self.pinned_context:
            raise ChatModeRejected("chat workspace requires pinned context")
        if not self.conversion_actions:
            raise ChatModeRejected("chat workspace requires conversion actions")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"ChatWorkspaceState(workspace_id={self.workspace_id!r}, active_branch_id={self.active_branch_id!r}, branches={self.branches!r})"


def require_chat_ready(state: ChatWorkspaceState) -> None:
    """Reject chat state with orphan branches or unpinned conversion context.

    Raises:
        Exception: Propagates validation or runtime failures from the underlying operation.
    """
    branch_ids = {branch.branch_id for branch in state.branches}
    if state.active_branch_id not in branch_ids:
        raise ChatModeRejected("active branch is not present in branch graph")
    for branch in state.branches:
        if branch.parent_branch_id is not None and branch.parent_branch_id not in branch_ids:
            raise ChatModeRejected(f"branch {branch.branch_id!r} has missing parent")
    context_ids = {context.context_id for context in state.pinned_context}
    for action in state.conversion_actions:
        if action.source_branch_id not in branch_ids:
            raise ChatModeRejected(f"conversion action references missing branch {action.source_branch_id!r}")
        missing = set(action.required_context_ids) - context_ids
        if missing:
            raise ChatModeRejected(f"conversion action missing pinned context: {sorted(missing)}")


def _require_non_empty(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ChatModeRejected(f"{field_name} must be non-empty")


def _require_non_empty_tuple(values: tuple[str, ...], field_name: str) -> None:
    if (
        not isinstance(values, tuple)
        or not values
        or not all(isinstance(value, str) and value.strip() for value in values)
    ):
        raise ChatModeRejected(f"{field_name} must contain non-empty strings")


__all__ = [
    "CHAT_REQUIRED_ARTIFACTS",
    "CHAT_TEMPLATE_ID",
    "ChatBranch",
    "ChatConversionAction",
    "ChatConversionKind",
    "ChatModeRejected",
    "ChatWorkspaceState",
    "PinnedContextRef",
    "require_chat_ready",
]
