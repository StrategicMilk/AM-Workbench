"""Creative memory-scope guard."""

from __future__ import annotations

from dataclasses import dataclass

from vetinari.workbench.memory_scopes import MemoryScope, MemoryScopePolicy, decide_memory_save


class CreativeScopeLeakRejected(ValueError):
    """Raised when creative state would leak into protected memory scopes."""

    def __init__(self, blockers: tuple[str, ...]) -> None:
        self.blockers = blockers
        super().__init__(", ".join(blockers))


@dataclass(frozen=True, slots=True)
class CreativeScopeDecision:
    """Runtime contract for CreativeScopeDecision."""

    approved: bool
    target_scope: MemoryScope
    blockers: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.target_scope, MemoryScope):
            object.__setattr__(self, "target_scope", MemoryScope(self.target_scope))
        if self.approved and self.blockers:
            raise CreativeScopeLeakRejected(("approved-decision-has-blockers",))


def assert_creative_scope(
    policy: MemoryScopePolicy,
    target_scope: MemoryScope | str,
    *,
    explicit_save: bool,
    promotion_ref: str = "",
) -> CreativeScopeDecision:
    """Approve creative saves only through the existing memory-scope policy.

    Args:
        policy: Policy value consumed by assert_creative_scope().
        target_scope: Target object or path updated by the operation.
        explicit_save: Explicit save value consumed by assert_creative_scope().
        promotion_ref: Promotion ref value consumed by assert_creative_scope().

    Returns:
        CreativeScopeDecision value produced by assert_creative_scope().

    Raises:
        Exception: Propagates validation or runtime failures from the underlying operation.
    """
    if not isinstance(policy, MemoryScopePolicy):
        raise CreativeScopeLeakRejected(("policy-required",))
    selected_target = MemoryScope(target_scope)
    blockers: list[str] = []
    is_promotion = bool(promotion_ref.strip())
    if policy.scope is not MemoryScope.CREATIVE and not is_promotion:
        blockers.append("creative-source-scope-required")
    if selected_target in {MemoryScope.SENSITIVE, MemoryScope.PROFESSIONAL}:
        if selected_target not in policy.cross_scope_allowed:
            blockers.append(f"creative-to-{selected_target.value}-not-authorized")
        if not is_promotion:
            blockers.append("promotion-ref-required")

    dependency_decision = decide_memory_save(
        policy,
        explicit_save=explicit_save,
        promotion_ref=promotion_ref,
        target_scope=selected_target,
    )
    blockers.extend(dependency_decision.blockers)
    deduped = tuple(dict.fromkeys(blockers))
    if deduped:
        raise CreativeScopeLeakRejected(deduped)
    return CreativeScopeDecision(approved=True, target_scope=selected_target, blockers=())


__all__ = [
    "CreativeScopeDecision",
    "CreativeScopeLeakRejected",
    "assert_creative_scope",
]
