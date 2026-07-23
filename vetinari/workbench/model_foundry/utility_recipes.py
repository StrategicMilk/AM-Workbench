"""Utility-model recipe contracts for narrow Workbench model helpers."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path
from typing import Any

import yaml

from vetinari.constants import PROJECT_ROOT

DEFAULT_RECIPE_PATH = PROJECT_ROOT / "config" / "workbench" / "utility_model_recipes.yaml"
DEFAULT_ASSET_REGISTRY_PATH = PROJECT_ROOT / "config" / "workbench" / "utility_model_assets.yaml"

BLOCKER_CALLER_NOT_ALLOWED = "caller_not_allowed"
BLOCKER_FEATURE_FLAG_DISABLED = "feature_flag_disabled"
BLOCKER_CONFIDENCE_BELOW_ABSTAIN = "confidence_below_abstain"


class UtilityModelRecipeError(ValueError):
    """Raised when a utility-model recipe is unavailable or unsafe."""


class UtilityModelTask(str, Enum):
    """Narrow utility-model tasks that cannot be promoted as chat models."""

    ROUTE_CLASSIFIER = "route_classifier"
    SOURCE_QUALITY_CLASSIFIER = "source_quality_classifier"
    PROMPT_INJECTION_DETECTOR = "prompt_injection_detector"
    FAILURE_CAUSE_CLASSIFIER = "failure_cause_classifier"
    PLAN_QUALITY_DISCRIMINATOR = "plan_quality_discriminator"
    RETRIEVAL_RERANKER = "retrieval_reranker"


class UtilityModelSurface(str, Enum):
    """The only runtime surfaces a passing utility model may occupy."""

    TYPED_TOOL = "typed_tool"
    ROUTE_HELPER = "route_helper"


@dataclass(frozen=True, slots=True)
class UtilityModelRecipe:
    """Governed recipe for one narrow internal utility model."""

    recipe_id: str
    task: UtilityModelTask
    dataset_source_ref: str
    eval_suite_ref: str
    abstain_threshold: float
    calibration_ref: str
    allowed_callers: tuple[str, ...]
    fallback_behavior: str
    promotion_gate_ref: str
    feature_flag: str
    enabled: bool
    surface: UtilityModelSurface
    safety_ref: str
    budget_ref: str
    authority_ref: str
    provenance_ref: str
    persisted_state_ref: str
    override_feedback_ref: str

    def __post_init__(self) -> None:
        for field_name in (
            "recipe_id",
            "dataset_source_ref",
            "eval_suite_ref",
            "calibration_ref",
            "fallback_behavior",
            "promotion_gate_ref",
            "feature_flag",
            "safety_ref",
            "budget_ref",
            "authority_ref",
            "provenance_ref",
            "persisted_state_ref",
            "override_feedback_ref",
        ):
            _require_text(getattr(self, field_name), field_name)
        if not isinstance(self.task, UtilityModelTask):
            raise UtilityModelRecipeError("task must be a UtilityModelTask")
        if not isinstance(self.surface, UtilityModelSurface):
            raise UtilityModelRecipeError("surface must be a UtilityModelSurface")
        _require_string_tuple(self.allowed_callers, "allowed_callers")
        if self.abstain_threshold < 0 or self.abstain_threshold > 1:
            raise UtilityModelRecipeError("abstain_threshold must be between 0 and 1")

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON/YAML-safe representation.

        Returns:
            dict[str, Any] value produced by to_dict().
        """
        payload = asdict(self)
        payload["task"] = self.task.value
        payload["surface"] = self.surface.value
        payload["allowed_callers"] = list(self.allowed_callers)
        return payload

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"UtilityModelRecipe(recipe_id={self.recipe_id!r}, task={self.task!r}, dataset_source_ref={self.dataset_source_ref!r})"


@dataclass(frozen=True, slots=True)
class UtilityModelCallDecision:
    """Fail-closed decision for using a utility recipe at a call site."""

    recipe_id: str
    caller: str
    approved: bool
    blockers: tuple[str, ...]
    fallback_behavior: str
    calibration_ref: str
    override_feedback_ref: str

    def __post_init__(self) -> None:
        _require_text(self.recipe_id, "recipe_id")
        _require_text(self.caller, "caller")
        _require_text(self.fallback_behavior, "fallback_behavior")
        _require_text(self.calibration_ref, "calibration_ref")
        _require_text(self.override_feedback_ref, "override_feedback_ref")
        _require_string_tuple(self.blockers, "blockers", allow_empty=True)
        if self.approved and self.blockers:
            raise UtilityModelRecipeError("approved decision cannot include blockers")
        if not self.approved and not self.blockers:
            raise UtilityModelRecipeError("blocked decision requires blockers")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"UtilityModelCallDecision(recipe_id={self.recipe_id!r}, caller={self.caller!r}, approved={self.approved!r})"


def load_utility_model_recipes(path: Path | str = DEFAULT_RECIPE_PATH) -> tuple[UtilityModelRecipe, ...]:
    """Load and validate utility-model recipes from a governed YAML file.

    Returns:
        Resolved utility model recipes value.

    Raises:
        Exception: Propagates validation or runtime failures from the underlying operation.
    """
    recipe_path = Path(path)
    if not recipe_path.exists():
        raise UtilityModelRecipeError(f"utility recipe config not found: {recipe_path}")
    raw = yaml.safe_load(recipe_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not isinstance(raw.get("recipes"), list):
        raise UtilityModelRecipeError("utility recipe config must contain a recipes list")
    recipes = tuple(_recipe_from_mapping(item) for item in raw["recipes"])
    if len({recipe.recipe_id for recipe in recipes}) != len(recipes):
        raise UtilityModelRecipeError("utility recipe ids must be unique")
    expected_tasks = set(UtilityModelTask)
    observed_tasks = {recipe.task for recipe in recipes}
    if observed_tasks != expected_tasks:
        missing = sorted(task.value for task in expected_tasks - observed_tasks)
        extra = sorted(task.value for task in observed_tasks - expected_tasks)
        raise UtilityModelRecipeError(f"utility recipe task coverage mismatch missing={missing} extra={extra}")
    if recipe_path.resolve() == DEFAULT_RECIPE_PATH.resolve():
        _validate_recipe_asset_refs(recipes, DEFAULT_ASSET_REGISTRY_PATH)
    return recipes


def select_utility_model_recipe(
    *,
    task: UtilityModelTask | str,
    caller: str,
    recipes: tuple[UtilityModelRecipe, ...] | None = None,
    path: Path | str = DEFAULT_RECIPE_PATH,
) -> UtilityModelRecipe:
    """Return the recipe for a task after proving the caller is allowed.

    Returns:
        Resolved utility model recipe value.

    Raises:
        Exception: Propagates validation or runtime failures from the underlying operation.
    """
    selected_task = UtilityModelTask(task)
    candidates = recipes if recipes is not None else load_utility_model_recipes(path)
    for recipe in candidates:
        if recipe.task == selected_task:
            _require_text(caller, "caller")
            if caller not in recipe.allowed_callers:
                raise UtilityModelRecipeError(f"{caller!r} is not allowed to call {recipe.recipe_id}")
            return recipe
    raise UtilityModelRecipeError(f"no utility recipe for task {selected_task.value!r}")


def decide_utility_model_call(
    recipe: UtilityModelRecipe, *, caller: str, confidence: float
) -> UtilityModelCallDecision:
    """Approve or fall back for a narrow utility-model call.

    Returns:
        UtilityModelCallDecision value produced by decide_utility_model_call().

    Raises:
        Exception: Propagates validation or runtime failures from the underlying operation.
    """
    if not isinstance(recipe, UtilityModelRecipe):
        raise UtilityModelRecipeError("recipe must be a UtilityModelRecipe")
    _require_text(caller, "caller")
    blockers: list[str] = []
    if caller not in recipe.allowed_callers:
        blockers.append(BLOCKER_CALLER_NOT_ALLOWED)
    if not recipe.enabled:
        blockers.append(BLOCKER_FEATURE_FLAG_DISABLED)
    if confidence < recipe.abstain_threshold:
        blockers.append(BLOCKER_CONFIDENCE_BELOW_ABSTAIN)
    return UtilityModelCallDecision(
        recipe_id=recipe.recipe_id,
        caller=caller,
        approved=not blockers,
        blockers=tuple(blockers),
        fallback_behavior=recipe.fallback_behavior,
        calibration_ref=recipe.calibration_ref,
        override_feedback_ref=recipe.override_feedback_ref,
    )


def _recipe_from_mapping(raw: object) -> UtilityModelRecipe:
    if not isinstance(raw, dict):
        raise UtilityModelRecipeError("each utility recipe must be a mapping")
    return UtilityModelRecipe(
        recipe_id=str(raw.get("recipe_id", "")),
        task=UtilityModelTask(str(raw.get("task", ""))),
        dataset_source_ref=str(raw.get("dataset_source_ref", "")),
        eval_suite_ref=str(raw.get("eval_suite_ref", "")),
        abstain_threshold=float(raw.get("abstain_threshold", -1)),
        calibration_ref=str(raw.get("calibration_ref", "")),
        allowed_callers=tuple(str(item) for item in raw.get("allowed_callers", ())),
        fallback_behavior=str(raw.get("fallback_behavior", "")),
        promotion_gate_ref=str(raw.get("promotion_gate_ref", "")),
        feature_flag=str(raw.get("feature_flag", "")),
        enabled=bool(raw.get("enabled", False)),
        surface=UtilityModelSurface(str(raw.get("surface", ""))),
        safety_ref=str(raw.get("safety_ref", "")),
        budget_ref=str(raw.get("budget_ref", "")),
        authority_ref=str(raw.get("authority_ref", "")),
        provenance_ref=str(raw.get("provenance_ref", "")),
        persisted_state_ref=str(raw.get("persisted_state_ref", "")),
        override_feedback_ref=str(raw.get("override_feedback_ref", "")),
    )


def _require_text(value: object, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise UtilityModelRecipeError(f"{field_name} must be non-empty")


def _require_string_tuple(values: tuple[str, ...], field_name: str, *, allow_empty: bool = False) -> None:
    if not isinstance(values, tuple) or (not allow_empty and not values):
        raise UtilityModelRecipeError(f"{field_name} must be a non-empty tuple")
    if any(not isinstance(value, str) or not value.strip() for value in values):
        raise UtilityModelRecipeError(f"{field_name} must contain non-empty strings")


def _validate_recipe_asset_refs(recipes: tuple[UtilityModelRecipe, ...], asset_registry_path: Path) -> None:
    if not asset_registry_path.exists():
        raise UtilityModelRecipeError(f"utility asset registry not found: {asset_registry_path}")
    raw = yaml.safe_load(asset_registry_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise UtilityModelRecipeError("utility asset registry must be a mapping")
    datasets = raw.get("datasets")
    eval_suites = raw.get("eval_suites")
    if not isinstance(datasets, dict) or not isinstance(eval_suites, dict):
        raise UtilityModelRecipeError("utility asset registry must define datasets and eval_suites")
    missing: list[str] = []
    unavailable: list[str] = []
    for recipe in recipes:
        for ref, registry in ((recipe.dataset_source_ref, datasets), (recipe.eval_suite_ref, eval_suites)):
            row = registry.get(ref)
            if not isinstance(row, dict):
                missing.append(f"{recipe.recipe_id}:{ref}")
            elif row.get("status") != "available":
                unavailable.append(f"{recipe.recipe_id}:{ref}:{row.get('status')}")
    if missing or unavailable:
        raise UtilityModelRecipeError(
            f"utility recipe asset refs unresolved missing={missing} unavailable={unavailable}"
        )


__all__ = [
    "BLOCKER_CALLER_NOT_ALLOWED",
    "BLOCKER_CONFIDENCE_BELOW_ABSTAIN",
    "BLOCKER_FEATURE_FLAG_DISABLED",
    "DEFAULT_ASSET_REGISTRY_PATH",
    "DEFAULT_RECIPE_PATH",
    "UtilityModelCallDecision",
    "UtilityModelRecipe",
    "UtilityModelRecipeError",
    "UtilityModelSurface",
    "UtilityModelTask",
    "decide_utility_model_call",
    "load_utility_model_recipes",
    "select_utility_model_recipe",
]
