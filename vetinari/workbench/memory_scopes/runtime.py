"""Memory-scope policies for private, sensitive, and project context."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any

BLOCKER_EXPLICIT_SAVE_REQUIRED = "explicit_save_required"
BLOCKER_PROMOTION_REQUIRED = "promotion_required"
BLOCKER_CROSS_SCOPE_NOT_ALLOWED = "cross_scope_not_allowed"


class MemoryScopeError(ValueError):
    """Raised when memory-scope policy is incomplete or unsafe."""


class MemoryScope(str, Enum):
    """Runtime contract for MemoryScope."""

    PRIVATE_PERSONAL = "private_personal"
    CONVERSATION_ONLY = "conversation_only"
    CREATIVE = "creative"
    PROFESSIONAL = "professional"
    SENSITIVE = "sensitive"
    PROJECT = "project"


class SensitiveMemoryCategory(str, Enum):
    """Runtime contract for SensitiveMemoryCategory."""

    TAXES = "taxes"
    FINANCE = "finance"
    LEGAL = "legal"
    HEALTH = "health"
    IDENTITY = "identity"
    FAMILY_LIFE_ADMIN = "family_life_admin"


@dataclass(frozen=True, slots=True)
class MemoryScopePolicy:
    """Visibility and governance policy for one memory scope."""

    scope: MemoryScope
    label: str
    explicit_save_required: bool
    promotion_required: bool
    recall_visible: bool
    deletion_visible: bool
    review_visible: bool
    decay_days: int
    cross_scope_allowed: tuple[MemoryScope, ...]
    sensitive_categories: tuple[SensitiveMemoryCategory, ...]
    authority_ref: str
    provenance_ref: str
    persisted_state_ref: str

    def __post_init__(self) -> None:
        if not isinstance(self.scope, MemoryScope):
            raise MemoryScopeError("scope must be MemoryScope")
        for field_name in ("label", "authority_ref", "provenance_ref", "persisted_state_ref"):
            _require_text(getattr(self, field_name), field_name)
        if self.decay_days < 0:
            raise MemoryScopeError("decay_days must be non-negative")
        if any(not isinstance(scope, MemoryScope) for scope in self.cross_scope_allowed):
            raise MemoryScopeError("cross_scope_allowed must contain MemoryScope values")
        if any(not isinstance(category, SensitiveMemoryCategory) for category in self.sensitive_categories):
            raise MemoryScopeError("sensitive_categories must contain SensitiveMemoryCategory values")
        if self.scope is MemoryScope.SENSITIVE and not (self.explicit_save_required and self.promotion_required):
            raise MemoryScopeError("sensitive memory requires explicit save and promotion")
        if not (self.recall_visible and self.deletion_visible and self.review_visible):
            raise MemoryScopeError("recall, deletion, and review visibility are required")

    def to_dict(self) -> dict[str, Any]:
        """Execute the to dict operation.

        Returns:
            dict[str, Any] value produced by to_dict().
        """
        payload = asdict(self)
        payload["scope"] = self.scope.value
        payload["cross_scope_allowed"] = [scope.value for scope in self.cross_scope_allowed]
        payload["sensitive_categories"] = [category.value for category in self.sensitive_categories]
        return payload

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"MemoryScopePolicy(scope={self.scope!r}, label={self.label!r}, explicit_save_required={self.explicit_save_required!r})"


@dataclass(frozen=True, slots=True)
class MemoryScopeDecision:
    """Runtime contract for MemoryScopeDecision."""

    scope: MemoryScope
    approved: bool
    blockers: tuple[str, ...]
    review_visible: bool
    deletion_visible: bool

    def __post_init__(self) -> None:
        _require_string_tuple(self.blockers, "blockers", allow_empty=True)
        if self.approved and self.blockers:
            raise MemoryScopeError("approved memory decision cannot include blockers")
        if not self.approved and not self.blockers:
            raise MemoryScopeError("blocked memory decision requires blockers")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"MemoryScopeDecision(scope={self.scope!r}, approved={self.approved!r}, blockers={self.blockers!r})"


def default_memory_scope_policies() -> tuple[MemoryScopePolicy, ...]:
    """Return the initial Workbench memory scopes.

    Returns:
        tuple[MemoryScopePolicy, ...] value produced by default_memory_scope_policies().
    """
    common = {
        "recall_visible": True,
        "deletion_visible": True,
        "review_visible": True,
        "authority_ref": "authority:memory-scopes",
        "provenance_ref": "provenance:memory-scopes",
        "persisted_state_ref": "state:memory-scopes",
    }
    return (
        MemoryScopePolicy(
            scope=MemoryScope.PRIVATE_PERSONAL,
            label="Private Personal",
            explicit_save_required=True,
            promotion_required=False,
            decay_days=365,
            cross_scope_allowed=(),
            sensitive_categories=(),
            **common,
        ),
        MemoryScopePolicy(
            scope=MemoryScope.CONVERSATION_ONLY,
            label="Conversation Only",
            explicit_save_required=False,
            promotion_required=False,
            decay_days=0,
            cross_scope_allowed=(),
            sensitive_categories=(),
            **common,
        ),
        MemoryScopePolicy(
            scope=MemoryScope.CREATIVE,
            label="Creative",
            explicit_save_required=True,
            promotion_required=False,
            decay_days=730,
            cross_scope_allowed=(MemoryScope.PROJECT,),
            sensitive_categories=(),
            **common,
        ),
        MemoryScopePolicy(
            scope=MemoryScope.PROFESSIONAL,
            label="Professional",
            explicit_save_required=True,
            promotion_required=False,
            decay_days=365,
            cross_scope_allowed=(MemoryScope.PROJECT,),
            sensitive_categories=(),
            **common,
        ),
        MemoryScopePolicy(
            scope=MemoryScope.SENSITIVE,
            label="Sensitive",
            explicit_save_required=True,
            promotion_required=True,
            decay_days=90,
            cross_scope_allowed=(),
            sensitive_categories=tuple(SensitiveMemoryCategory),
            **common,
        ),
        MemoryScopePolicy(
            scope=MemoryScope.PROJECT,
            label="Project",
            explicit_save_required=True,
            promotion_required=False,
            decay_days=730,
            cross_scope_allowed=(MemoryScope.PROFESSIONAL,),
            sensitive_categories=(),
            **common,
        ),
    )


def decide_memory_save(
    policy: MemoryScopePolicy,
    *,
    explicit_save: bool,
    promotion_ref: str = "",
    target_scope: MemoryScope | str | None = None,
) -> MemoryScopeDecision:
    """Approve memory save only when scope policy allows it.

    Returns:
        MemoryScopeDecision value produced by decide_memory_save().

    Raises:
        Exception: Propagates validation or runtime failures from the underlying operation.
    """
    if not isinstance(policy, MemoryScopePolicy):
        raise MemoryScopeError("policy must be MemoryScopePolicy")
    blockers: list[str] = []
    if policy.explicit_save_required and not explicit_save:
        blockers.append(BLOCKER_EXPLICIT_SAVE_REQUIRED)
    if policy.promotion_required and not promotion_ref.strip():
        blockers.append(BLOCKER_PROMOTION_REQUIRED)
    if target_scope is not None:
        selected_target = MemoryScope(target_scope)
        if selected_target != policy.scope and selected_target not in policy.cross_scope_allowed:
            blockers.append(BLOCKER_CROSS_SCOPE_NOT_ALLOWED)
    return MemoryScopeDecision(
        scope=policy.scope,
        approved=not blockers,
        blockers=tuple(dict.fromkeys(blockers)),
        review_visible=policy.review_visible,
        deletion_visible=policy.deletion_visible,
    )


def _require_text(value: object, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise MemoryScopeError(f"{field_name} must be non-empty")


def _require_string_tuple(values: tuple[str, ...], field_name: str, *, allow_empty: bool = False) -> None:
    if not isinstance(values, tuple) or (not allow_empty and not values):
        raise MemoryScopeError(f"{field_name} must be a non-empty tuple")
    if any(not isinstance(value, str) or not value.strip() for value in values):
        raise MemoryScopeError(f"{field_name} must contain non-empty strings")


__all__ = [
    "BLOCKER_CROSS_SCOPE_NOT_ALLOWED",
    "BLOCKER_EXPLICIT_SAVE_REQUIRED",
    "BLOCKER_PROMOTION_REQUIRED",
    "MemoryScope",
    "MemoryScopeDecision",
    "MemoryScopeError",
    "MemoryScopePolicy",
    "SensitiveMemoryCategory",
    "decide_memory_save",
    "default_memory_scope_policies",
]
