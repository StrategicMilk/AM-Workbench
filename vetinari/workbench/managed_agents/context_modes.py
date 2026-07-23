"""Managed-agent context isolation modes."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ManagedAgentContextMode(str, Enum):
    """Isolation modes for managed-agent child runs."""

    STATELESS = "stateless"
    BRANCH_ISOLATED = "branch_isolated"
    PROJECT_SHARED = "project_shared"
    SENSITIVE_ISOLATED = "sensitive_isolated"


@dataclass(frozen=True, slots=True)
class ManagedAgentContextRequest:
    """Context admission request for a managed-agent run."""

    mode: ManagedAgentContextMode
    parent_thread_id: str
    child_thread_id: str
    memory_scope: tuple[str, ...]
    requested_workspace_writes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "mode", ManagedAgentContextMode(self.mode))
        for field_name in ("parent_thread_id", "child_thread_id"):
            _require_text(getattr(self, field_name), field_name)

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"ManagedAgentContextRequest(mode={self.mode!r}, parent_thread_id={self.parent_thread_id!r}, child_thread_id={self.child_thread_id!r})"


@dataclass(frozen=True, slots=True)
class ManagedAgentContextDecision:
    """Fail-closed context-mode decision."""

    accepted: bool
    mode: ManagedAgentContextMode
    child_thread_id: str
    blockers: tuple[str, ...]

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"ManagedAgentContextDecision(accepted={self.accepted!r}, mode={self.mode!r}, child_thread_id={self.child_thread_id!r})"


def evaluate_managed_agent_context(request: ManagedAgentContextRequest) -> ManagedAgentContextDecision:
    """Enforce thread, memory, and workspace boundaries for context modes.

    Returns:
        ManagedAgentContextDecision value produced by evaluate_managed_agent_context().
    """
    blockers: list[str] = []
    if (
        request.mode in {ManagedAgentContextMode.STATELESS, ManagedAgentContextMode.SENSITIVE_ISOLATED}
        and request.memory_scope
    ):
        blockers.append("memory_scope_not_allowed")
    if (
        request.mode in {ManagedAgentContextMode.BRANCH_ISOLATED, ManagedAgentContextMode.SENSITIVE_ISOLATED}
        and request.child_thread_id == request.parent_thread_id
    ):
        blockers.append("child_thread_must_be_isolated")
    if request.mode is ManagedAgentContextMode.SENSITIVE_ISOLATED and request.requested_workspace_writes:
        blockers.append("workspace_writes_not_allowed")
    return ManagedAgentContextDecision(
        accepted=not blockers,
        mode=request.mode,
        child_thread_id=request.child_thread_id,
        blockers=tuple(blockers),
    )


def _require_text(value: object, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be non-empty")


__all__ = [
    "ManagedAgentContextDecision",
    "ManagedAgentContextMode",
    "ManagedAgentContextRequest",
    "evaluate_managed_agent_context",
]
