"""Governed automation recipe assets with replay-before-activation checks.

This module is import-safe. It performs no I/O, owns no global registry, and
does not execute automation actions. Callers pass schema-shaped payloads and
historical replay evidence, then receive immutable value objects and verdicts.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from math import isfinite
from typing import Any

from vetinari.workbench.assets import AssetKind, WorkbenchAsset
from vetinari.workbench.automation import ALLOWED_TRIGGER_SOURCES, AutomationFailurePolicy


class AutomationRecipeValidationError(ValueError):
    """Raised when a recipe cannot be trusted enough to persist or replay."""


class AuthorityLevel(str, Enum):
    """Maximum authority an automation recipe may exercise after replay."""

    OBSERVE = "observe"
    PROPOSE = "propose"
    LOW_RISK_EXECUTE = "low_risk_execute"
    HIGH_IMPACT = "high_impact"


@dataclass(frozen=True, slots=True)
class RecipeReplayPolicy:
    """Replay thresholds required before a recipe can activate."""

    minimum_trace_count: int
    minimum_confidence: float
    require_negative_eval: bool = True
    require_promotion_for_high_impact: bool = True

    def __post_init__(self) -> None:
        if self.minimum_trace_count <= 0:
            raise AutomationRecipeValidationError("replay_policy.minimum_trace_count must be > 0")
        if not 0 <= self.minimum_confidence <= 1:
            raise AutomationRecipeValidationError("replay_policy.minimum_confidence must be between 0 and 1")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"RecipeReplayPolicy(minimum_trace_count={self.minimum_trace_count!r}, minimum_confidence={self.minimum_confidence!r}, require_negative_eval={self.require_negative_eval!r})"


@dataclass(frozen=True, slots=True)
class RecipeReplayEvidence:
    """One historical trace replay result for a recipe candidate."""

    trace_id: str
    run_id: str
    matched_conditions: bool
    confidence: float
    evidence_refs: tuple[str, ...]
    captured_at_utc: str
    negative_eval_exercised: bool = False
    safety_violations: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_text(self.trace_id, "trace_id")
        _require_text(self.run_id, "run_id")
        _require_utc(self.captured_at_utc, "captured_at_utc")
        if not 0 <= self.confidence <= 1 or not isfinite(self.confidence):
            raise AutomationRecipeValidationError("replay confidence must be finite and between 0 and 1")
        _require_string_tuple(self.evidence_refs, "evidence_refs")
        if not isinstance(self.matched_conditions, bool):
            raise AutomationRecipeValidationError("matched_conditions must be bool")
        if not isinstance(self.negative_eval_exercised, bool):
            raise AutomationRecipeValidationError("negative_eval_exercised must be bool")
        if not isinstance(self.safety_violations, tuple):
            raise AutomationRecipeValidationError("safety_violations must be a tuple")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"RecipeReplayEvidence(trace_id={self.trace_id!r}, run_id={self.run_id!r}, matched_conditions={self.matched_conditions!r})"


@dataclass(frozen=True, slots=True)
class AutomationRecipeAsset:
    """Revisioned automation recipe bound to a WorkbenchAsset card."""

    recipe_id: str
    recipe_version: str
    workbench_asset: WorkbenchAsset
    owner: str
    trigger: str
    conditions: tuple[str, ...]
    authority_level: AuthorityLevel
    budget_ref: str
    quiet_hours_ref: str
    rollback_ref: str
    failure_policy: AutomationFailurePolicy
    eval_suite_refs: tuple[str, ...]
    replay_policy: RecipeReplayPolicy
    created_at_utc: str
    stale_after_utc: str
    promotion_decision_ref: str = ""
    provenance: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_text(self.recipe_id, "recipe_id")
        _require_text(self.recipe_version, "recipe_version")
        if self.recipe_version != self.workbench_asset.revision:
            raise AutomationRecipeValidationError("recipe_version must match WorkbenchAsset.revision")
        _require_text(self.owner, "owner")
        if self.trigger not in ALLOWED_TRIGGER_SOURCES:
            raise AutomationRecipeValidationError(f"unsupported trigger source: {self.trigger!r}")
        _require_string_tuple(self.conditions, "conditions")
        if not isinstance(self.authority_level, AuthorityLevel):
            raise AutomationRecipeValidationError("authority_level must be AuthorityLevel")
        _require_text(self.budget_ref, "budget_ref")
        _require_text(self.quiet_hours_ref, "quiet_hours_ref")
        _require_text(self.rollback_ref, "rollback_ref")
        if not isinstance(self.failure_policy, AutomationFailurePolicy):
            raise AutomationRecipeValidationError("failure_policy must be AutomationFailurePolicy")
        _require_string_tuple(self.eval_suite_refs, "eval_suite_refs")
        if not isinstance(self.replay_policy, RecipeReplayPolicy):
            raise AutomationRecipeValidationError("replay_policy must be RecipeReplayPolicy")
        _require_utc(self.created_at_utc, "created_at_utc")
        _require_utc(self.stale_after_utc, "stale_after_utc")
        if _parse_utc(self.stale_after_utc) <= _parse_utc(self.created_at_utc):
            raise AutomationRecipeValidationError("stale_after_utc must be after created_at_utc")
        if not self.provenance.get("source", "").strip():
            raise AutomationRecipeValidationError("provenance.source must be non-empty")

    def to_dict(self) -> dict[str, Any]:
        """Return a schema-shaped representation for persistence or APIs.

        Returns:
            dict[str, Any] value produced by to_dict().
        """
        payload = asdict(self)
        payload["authority_level"] = self.authority_level.value
        payload["failure_policy"] = self.failure_policy.value
        payload["workbench_asset"]["kind"] = self.workbench_asset.kind.value
        return payload

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"AutomationRecipeAsset(recipe_id={self.recipe_id!r}, recipe_version={self.recipe_version!r}, workbench_asset={self.workbench_asset!r})"


@dataclass(frozen=True, slots=True)
class RecipeReplayVerdict:
    """Replay verdict for a candidate automation recipe."""

    recipe_id: str
    recipe_version: str
    accepted: bool
    activation_allowed: bool
    proposed_only: bool
    blockers: tuple[str, ...]
    replayed_trace_count: int
    average_confidence: float
    evidence_refs: tuple[str, ...]
    evaluated_at_utc: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"RecipeReplayVerdict(recipe_id={self.recipe_id!r}, recipe_version={self.recipe_version!r}, accepted={self.accepted!r})"


def build_automation_recipe_asset(payload: Mapping[str, Any]) -> AutomationRecipeAsset:
    """Build and validate an automation recipe asset from schema-shaped data.

    Returns:
        Newly constructed automation recipe asset value.

    Raises:
        Exception: Propagates validation or runtime failures from the underlying operation.
    """
    _require_mapping(payload, "payload")
    required = (
        "recipe_id",
        "recipe_version",
        "name",
        "owner",
        "trigger",
        "conditions",
        "authority_level",
        "budget_ref",
        "quiet_hours_ref",
        "rollback_ref",
        "failure_policy",
        "eval_suite_refs",
        "replay_policy",
        "created_at_utc",
        "stale_after_utc",
        "provenance",
    )
    missing = [field_name for field_name in required if field_name not in payload]
    if missing:
        raise AutomationRecipeValidationError(f"recipe asset missing fields: {', '.join(missing)}")

    provenance = _string_mapping(payload["provenance"], "provenance")
    recipe_id = str(payload["recipe_id"])
    recipe_version = str(payload["recipe_version"])
    workbench_asset = WorkbenchAsset(
        asset_id=f"automation-recipe:{recipe_id}",
        kind=AssetKind.TOOL,
        name=str(payload["name"]),
        revision=recipe_version,
        created_at_utc=str(payload["created_at_utc"]),
        provenance=dict(provenance),
    )
    replay_policy_payload = _require_mapping(payload["replay_policy"], "replay_policy")
    return AutomationRecipeAsset(
        recipe_id=recipe_id,
        recipe_version=recipe_version,
        workbench_asset=workbench_asset,
        owner=str(payload["owner"]),
        trigger=str(payload["trigger"]),
        conditions=_string_tuple(payload["conditions"], "conditions"),
        authority_level=AuthorityLevel(str(payload["authority_level"])),
        budget_ref=str(payload["budget_ref"]),
        quiet_hours_ref=str(payload["quiet_hours_ref"]),
        rollback_ref=str(payload["rollback_ref"]),
        failure_policy=AutomationFailurePolicy(str(payload["failure_policy"])),
        eval_suite_refs=_string_tuple(payload["eval_suite_refs"], "eval_suite_refs"),
        replay_policy=RecipeReplayPolicy(
            minimum_trace_count=int(replay_policy_payload.get("minimum_trace_count", 0)),
            minimum_confidence=float(replay_policy_payload.get("minimum_confidence", -1)),
            require_negative_eval=bool(replay_policy_payload.get("require_negative_eval", True)),
            require_promotion_for_high_impact=bool(
                replay_policy_payload.get("require_promotion_for_high_impact", True)
            ),
        ),
        created_at_utc=str(payload["created_at_utc"]),
        stale_after_utc=str(payload["stale_after_utc"]),
        promotion_decision_ref=str(payload.get("promotion_decision_ref", "")),
        provenance=provenance,
    )


def validate_recipe_upgrade(previous: AutomationRecipeAsset, candidate: AutomationRecipeAsset) -> None:
    """Reject stale or same-version candidate recipes for the same recipe id.

    Args:
        previous: Previous value consumed by validate_recipe_upgrade().
        candidate: Candidate value consumed by validate_recipe_upgrade().

    Raises:
        Exception: Propagates validation or runtime failures from the underlying operation.
    """
    if previous.recipe_id != candidate.recipe_id:
        raise AutomationRecipeValidationError("candidate recipe_id must match previous recipe_id")
    if _version_key(candidate.recipe_version) <= _version_key(previous.recipe_version):
        raise AutomationRecipeValidationError("candidate recipe_version must be newer than previous recipe_version")


def replay_automation_recipe(
    recipe: AutomationRecipeAsset,
    replay_evidence: Sequence[RecipeReplayEvidence],
    *,
    now_utc: datetime | None = None,
) -> RecipeReplayVerdict:
    """Replay a recipe against historical traces and fail closed on uncertainty.

    Args:
        recipe: Recipe value consumed by replay_automation_recipe().
        replay_evidence: Replay evidence value consumed by replay_automation_recipe().
        now_utc: Now utc value consumed by replay_automation_recipe().

    Returns:
        RecipeReplayVerdict value produced by replay_automation_recipe().
    """
    now = (now_utc or datetime.now(timezone.utc)).astimezone(timezone.utc)
    blockers: list[str] = []
    evidence = tuple(replay_evidence)

    if _parse_utc(recipe.stale_after_utc) <= now:
        blockers.append("recipe-stale")
    if len(evidence) < recipe.replay_policy.minimum_trace_count:
        blockers.append("replay-trace-count-below-policy")
    if not recipe.eval_suite_refs:
        blockers.append("eval-suite-missing")
    if not recipe.provenance.get("source", "").strip():
        blockers.append("provenance-source-missing")

    evidence_refs: list[str] = []
    confidence_values: list[float] = []
    negative_eval_seen = False
    for item in evidence:
        confidence_values.append(item.confidence)
        evidence_refs.extend(item.evidence_refs)
        negative_eval_seen = negative_eval_seen or item.negative_eval_exercised
        if not item.matched_conditions:
            blockers.append(f"replay-condition-mismatch:{item.trace_id}")
        if item.confidence < recipe.replay_policy.minimum_confidence:
            blockers.append(f"replay-confidence-below-policy:{item.trace_id}")
        if item.safety_violations:
            blockers.append(f"unsafe-replay:{item.trace_id}")

    if recipe.replay_policy.require_negative_eval and not negative_eval_seen:
        blockers.append("negative-eval-replay-missing")
    if (
        recipe.authority_level is AuthorityLevel.HIGH_IMPACT
        and recipe.replay_policy.require_promotion_for_high_impact
        and not recipe.promotion_decision_ref.strip()
    ):
        blockers.append("promotion-inbox-approval-required")

    unique_blockers = tuple(dict.fromkeys(blockers))
    average_confidence = sum(confidence_values) / len(confidence_values) if confidence_values else 0.0
    accepted = not unique_blockers
    activation_allowed = accepted and recipe.authority_level is not AuthorityLevel.PROPOSE
    proposed_only = not activation_allowed or recipe.authority_level in {AuthorityLevel.OBSERVE, AuthorityLevel.PROPOSE}
    return RecipeReplayVerdict(
        recipe_id=recipe.recipe_id,
        recipe_version=recipe.recipe_version,
        accepted=accepted,
        activation_allowed=activation_allowed,
        proposed_only=proposed_only,
        blockers=unique_blockers,
        replayed_trace_count=len(evidence),
        average_confidence=average_confidence,
        evidence_refs=tuple(dict.fromkeys(evidence_refs)),
        evaluated_at_utc=now.isoformat(),
    )


def _require_text(value: str, field_name: str) -> None:
    if not value or not value.strip():
        raise AutomationRecipeValidationError(f"{field_name} must be non-empty")


def _require_string_tuple(values: tuple[str, ...], field_name: str) -> None:
    if not isinstance(values, tuple):
        raise AutomationRecipeValidationError(f"{field_name} must be a tuple")
    if not values:
        raise AutomationRecipeValidationError(f"{field_name} must be non-empty")
    if not all(isinstance(value, str) and value.strip() for value in values):
        raise AutomationRecipeValidationError(f"{field_name} must contain non-empty strings")


def _string_tuple(values: Any, field_name: str) -> tuple[str, ...]:
    if isinstance(values, str) or not isinstance(values, Sequence):
        raise AutomationRecipeValidationError(f"{field_name} must be a sequence of strings")
    result = tuple(str(value) for value in values)
    _require_string_tuple(result, field_name)
    return result


def _require_mapping(value: Any, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise AutomationRecipeValidationError(f"{field_name} must be a mapping")
    return value


def _string_mapping(value: Any, field_name: str) -> Mapping[str, str]:
    mapping = _require_mapping(value, field_name)
    result = {str(key): str(item) for key, item in mapping.items()}
    if not result.get("source", "").strip():
        raise AutomationRecipeValidationError(f"{field_name}.source must be non-empty")
    return result


def _require_utc(value: str, field_name: str) -> None:
    _parse_utc(value, field_name)


def _parse_utc(value: str, field_name: str = "datetime") -> datetime:
    _require_text(str(value), field_name)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise AutomationRecipeValidationError(f"{field_name} must be ISO-8601") from exc
    if parsed.tzinfo is None:
        raise AutomationRecipeValidationError(f"{field_name} must include timezone")
    return parsed.astimezone(timezone.utc)


def _version_key(value: str) -> tuple[int, ...]:
    parts = value.split(".")
    try:
        return tuple(int(part) for part in parts)
    except ValueError as exc:
        raise AutomationRecipeValidationError("recipe_version must use numeric dot-separated segments") from exc


__all__ = [
    "AuthorityLevel",
    "AutomationRecipeAsset",
    "AutomationRecipeValidationError",
    "RecipeReplayEvidence",
    "RecipeReplayPolicy",
    "RecipeReplayVerdict",
    "build_automation_recipe_asset",
    "replay_automation_recipe",
    "validate_recipe_upgrade",
]
