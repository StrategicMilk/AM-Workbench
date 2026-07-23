"""Canon and exploratory creative branch isolation."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from vetinari.workbench.conversation import ConversationBranch, ConversationSafetyContext


class CreativeBranchIsolationRejected(ValueError):
    """Raised when branch isolation or promotion proof fails."""

    def __init__(self, blocker: str) -> None:
        self.blocker = blocker
        super().__init__(blocker)


class CanonBranchKind(StrEnum):
    """Runtime contract for CanonBranchKind."""

    CANON = "canon"
    EXPLORATORY = "exploratory"


@dataclass(frozen=True, slots=True)
class CreativeBranchBinding:
    """Runtime contract for CreativeBranchBinding."""

    branch_id: str
    kind: CanonBranchKind
    world_id: str
    authority_ref: str

    def __post_init__(self) -> None:
        _require_text(self.branch_id, "branch_id")
        if not isinstance(self.kind, CanonBranchKind):
            object.__setattr__(self, "kind", CanonBranchKind(self.kind))
        _require_text(self.world_id, "world_id")
        _require_text(self.authority_ref, "authority_ref")

    @classmethod
    def from_conversation_branch(
        cls,
        branch: ConversationBranch,
        *,
        kind: CanonBranchKind,
        world_id: str,
        authority_ref: str,
    ) -> CreativeBranchBinding:
        return cls(
            branch_id=branch.branch_id,
            kind=kind,
            world_id=world_id,
            authority_ref=authority_ref,
        )

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"CreativeBranchBinding(branch_id={self.branch_id!r}, kind={self.kind!r}, world_id={self.world_id!r})"


def assert_branch_isolation(
    canon_bindings: tuple[CreativeBranchBinding, ...],
    exploratory_bindings: tuple[CreativeBranchBinding, ...],
) -> None:
    """Reject branch ids claimed by both canon and exploratory tracks.

    Args:
        canon_bindings: Canon bindings value consumed by assert_branch_isolation().
        exploratory_bindings: Exploratory bindings value consumed by assert_branch_isolation().

    Raises:
        Exception: Propagates validation or runtime failures from the underlying operation.
    """
    canon_ids = _ids_for(canon_bindings, CanonBranchKind.CANON)
    exploratory_ids = _ids_for(exploratory_bindings, CanonBranchKind.EXPLORATORY)
    collision = canon_ids & exploratory_ids
    if collision:
        raise CreativeBranchIsolationRejected(f"branch-id-collision:{min(collision)}")


def assert_promotion_allowed(
    source_binding: CreativeBranchBinding,
    target_binding: CreativeBranchBinding,
    *,
    safety_context: ConversationSafetyContext,
) -> None:
    """Reject unsafe promotion between creative branch tracks.

    Args:
        source_binding: Source object or text processed by the operation.
        target_binding: Target object or path updated by the operation.
        safety_context: Safety context value consumed by assert_promotion_allowed().

    Raises:
        Exception: Propagates validation or runtime failures from the underlying operation.
    """
    if not isinstance(safety_context, ConversationSafetyContext):
        raise CreativeBranchIsolationRejected("promotion requires conversation safety context")
    if source_binding.world_id != target_binding.world_id:
        raise CreativeBranchIsolationRejected("promotion-world-mismatch")
    if target_binding.kind is not CanonBranchKind.CANON:
        raise CreativeBranchIsolationRejected("promotion-target-must-be-canon")
    if source_binding.kind is CanonBranchKind.EXPLORATORY:
        if not safety_context.authority_ref:
            raise CreativeBranchIsolationRejected("promotion-authority-required")
        if not safety_context.evidence_refs:
            raise CreativeBranchIsolationRejected("promotion-evidence-required")
    if source_binding.kind is CanonBranchKind.CANON and source_binding.world_id != target_binding.world_id:
        raise CreativeBranchIsolationRejected("canon-cross-world-promotion-rejected")


def _ids_for(bindings: tuple[CreativeBranchBinding, ...], expected_kind: CanonBranchKind) -> set[str]:
    ids: set[str] = set()
    for binding in bindings:
        if binding.kind is not expected_kind:
            raise CreativeBranchIsolationRejected(f"branch-kind-mismatch:{binding.branch_id}")
        ids.add(binding.branch_id)
    return ids


def _require_text(value: object, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise CreativeBranchIsolationRejected(f"{field_name} must be non-empty")


__all__ = [
    "CanonBranchKind",
    "CreativeBranchBinding",
    "CreativeBranchIsolationRejected",
    "assert_branch_isolation",
    "assert_promotion_allowed",
]
