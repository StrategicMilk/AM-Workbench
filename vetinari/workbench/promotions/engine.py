"""Explicit recipes for promoting conversation material to typed artifacts."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any


class PromotionEngineError(ValueError):
    """Raised when a conversation promotion cannot be offered safely."""


class PromotionKind(str, Enum):
    """Wave 18 supported promotion outputs."""

    CONVERSATION_EXCERPT = "conversation_excerpt"
    EVIDENCE_ASSET = "evidence_asset"
    EVIDENCE_NOTEBOOK = "evidence_notebook"
    PREFERENCE_CARD = "preference_card"
    EVAL_CASE = "eval_case"
    AUTOMATION_RECIPE = "automation_recipe"
    DATA_ASSET = "data_asset"


@dataclass(frozen=True, slots=True)
class SourceConversationRange:
    """Reversible source range in a conversation."""

    conversation_id: str
    start_turn: int
    end_turn: int
    excerpt_ref: str
    provenance_ref: str

    def __post_init__(self) -> None:
        for field_name in ("conversation_id", "excerpt_ref", "provenance_ref"):
            _require_text(getattr(self, field_name), field_name)
        if self.start_turn < 0 or self.end_turn < self.start_turn:
            raise PromotionEngineError("source conversation range must be ordered")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"SourceConversationRange(conversation_id={self.conversation_id!r}, start_turn={self.start_turn!r}, end_turn={self.end_turn!r})"


@dataclass(frozen=True, slots=True)
class PromotionRecipe:
    """Contextual action recipe for one supported promotion kind."""

    recipe_id: str
    kind: PromotionKind
    target_schema_ref: str
    dependency_api_ref: str
    reversible: bool
    contextual_action: bool
    authority_ref: str
    safety_ref: str
    budget_ref: str

    def __post_init__(self) -> None:
        for field_name in (
            "recipe_id",
            "target_schema_ref",
            "dependency_api_ref",
            "authority_ref",
            "safety_ref",
            "budget_ref",
        ):
            _require_text(getattr(self, field_name), field_name)
        if not isinstance(self.kind, PromotionKind):
            raise PromotionEngineError("kind must be PromotionKind")
        if not self.reversible:
            raise PromotionEngineError("promotion recipes must be reversible")
        if not self.contextual_action:
            raise PromotionEngineError("promotion recipes must be contextual actions")

    def to_dict(self) -> dict[str, Any]:
        """Execute the to dict operation.

        Returns:
            dict[str, Any] value produced by to_dict().
        """
        payload = asdict(self)
        payload["kind"] = self.kind.value
        return payload

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"PromotionRecipe(recipe_id={self.recipe_id!r}, kind={self.kind!r}, target_schema_ref={self.target_schema_ref!r})"


@dataclass(frozen=True, slots=True)
class PromotionDraft:
    """Structured artifact draft created from a source conversation range."""

    draft_id: str
    recipe: PromotionRecipe
    source_range: SourceConversationRange
    artifact_ref: str
    reversible_provenance_ref: str

    def __post_init__(self) -> None:
        _require_text(self.draft_id, "draft_id")
        _require_text(self.artifact_ref, "artifact_ref")
        _require_text(self.reversible_provenance_ref, "reversible_provenance_ref")
        if not isinstance(self.recipe, PromotionRecipe):
            raise PromotionEngineError("recipe must be PromotionRecipe")
        if not isinstance(self.source_range, SourceConversationRange):
            raise PromotionEngineError("source_range must be SourceConversationRange")

    def to_dict(self) -> dict[str, Any]:
        return {
            "draft_id": self.draft_id,
            "recipe": self.recipe.to_dict(),
            "source_range": self.source_range.to_dict(),
            "artifact_ref": self.artifact_ref,
            "reversible_provenance_ref": self.reversible_provenance_ref,
        }

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"PromotionDraft(draft_id={self.draft_id!r}, recipe={self.recipe!r}, source_range={self.source_range!r})"


def supported_promotion_recipes() -> tuple[PromotionRecipe, ...]:
    """Return the Wave 18 typed initial promotion subset."""
    return tuple(
        PromotionRecipe(
            recipe_id=f"promotion:{kind.value}",
            kind=kind,
            target_schema_ref=f"schema:{kind.value}",
            dependency_api_ref=_dependency_api_ref(kind),
            reversible=True,
            contextual_action=True,
            authority_ref="authority:promotion-engine",
            safety_ref="safety:promotion-engine",
            budget_ref="budget:promotion-engine",
        )
        for kind in PromotionKind
    )


def promote_conversation_material(
    *,
    kind: PromotionKind | str,
    source_range: SourceConversationRange,
    recipes: tuple[PromotionRecipe, ...] | None = None,
) -> PromotionDraft:
    """Create a typed draft only for the supported subset and a valid source range.

    Returns:
        PromotionDraft value produced by promote_conversation_material().

    Raises:
        Exception: Propagates validation or runtime failures from the underlying operation.
    """
    selected = PromotionKind(kind)
    if not isinstance(source_range, SourceConversationRange):
        raise PromotionEngineError("source_range must be SourceConversationRange")
    candidates = recipes or supported_promotion_recipes()
    for recipe in candidates:
        if recipe.kind == selected:
            return PromotionDraft(
                draft_id=f"draft:{source_range.conversation_id}:{selected.value}",
                recipe=recipe,
                source_range=source_range,
                artifact_ref=f"artifact:{selected.value}:{source_range.conversation_id}",
                reversible_provenance_ref=f"provenance:{source_range.conversation_id}:{source_range.start_turn}-{source_range.end_turn}",
            )
    raise PromotionEngineError(f"unsupported promotion kind {selected.value!r}")


def _dependency_api_ref(kind: PromotionKind) -> str:
    return {
        PromotionKind.CONVERSATION_EXCERPT: "vetinari.workbench.conversation.core",
        PromotionKind.EVIDENCE_ASSET: "vetinari.workbench.evidence_assets",
        PromotionKind.EVIDENCE_NOTEBOOK: "vetinari.workbench.evidence_notebooks",
        PromotionKind.PREFERENCE_CARD: "vetinari.workbench.preferences.cards",
        PromotionKind.EVAL_CASE: "vetinari.workbench.evals",
        PromotionKind.AUTOMATION_RECIPE: "vetinari.workbench.automation_assets.recipes",
        PromotionKind.DATA_ASSET: "vetinari.workbench.data_assets",
    }[kind]


def _require_text(value: object, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise PromotionEngineError(f"{field_name} must be non-empty")


__all__ = [
    "PromotionDraft",
    "PromotionEngineError",
    "PromotionKind",
    "PromotionRecipe",
    "SourceConversationRange",
    "promote_conversation_material",
    "supported_promotion_recipes",
]
