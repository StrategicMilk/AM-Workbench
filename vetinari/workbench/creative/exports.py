"""Creative roleplay studio export plan factory."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from vetinari.workbench.creative.world import CreativeWorldRejected, CreativeWorldState


class CreativeExportTarget(StrEnum):
    """Runtime contract for CreativeExportTarget."""

    STORY = "story"
    SCRIPT = "script"
    GAME_DESIGN = "game_design"
    MEDIA_PLAN = "media_plan"


@dataclass(frozen=True, slots=True)
class CreativeExportPlan:
    """Runtime contract for CreativeExportPlan."""

    world_id: str
    target: CreativeExportTarget
    title: str
    artifact_refs: tuple[str, ...]
    authority_ref: str
    evidence_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_text(self.world_id, "world_id")
        if not isinstance(self.target, CreativeExportTarget):
            raise CreativeWorldRejected("target must be CreativeExportTarget")
        _require_text(self.title, "title")
        _require_text_tuple(self.artifact_refs, "artifact_refs")
        _require_text(self.authority_ref, "authority_ref")
        _require_text_tuple(self.evidence_refs, "evidence_refs")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"CreativeExportPlan(world_id={self.world_id!r}, target={self.target!r}, title={self.title!r})"


def build_creative_export_plan(
    world_state: CreativeWorldState,
    target: CreativeExportTarget | str,
    *,
    authority_ref: str,
    evidence_refs: tuple[str, ...],
) -> CreativeExportPlan:
    """Build a typed export plan without writing artifacts.

    Args:
        world_state: World state value consumed by build_creative_export_plan().
        target: Target object or path updated by the operation.
        authority_ref: Authority ref value consumed by build_creative_export_plan().
        evidence_refs: Evidence refs value consumed by build_creative_export_plan().

    Returns:
        Newly constructed creative export plan value.

    Raises:
        Exception: Propagates validation or runtime failures from the underlying operation.
    """
    if not isinstance(world_state, CreativeWorldState):
        raise CreativeWorldRejected("world_state must be CreativeWorldState")
    _require_text(authority_ref, "authority_ref")
    _require_text_tuple(evidence_refs, "evidence_refs")
    try:
        export_target = target if isinstance(target, CreativeExportTarget) else CreativeExportTarget(target)
    except ValueError as exc:
        raise CreativeWorldRejected("unsupported creative export target") from exc
    return CreativeExportPlan(
        world_id=world_state.world_id,
        target=export_target,
        title=f"{world_state.title} {export_target.value.replace('_', ' ').title()} Export",
        artifact_refs=(
            f"world:{world_state.world_id}",
            f"style:{world_state.tone_style_guide.guide_id}",
            f"scenes:{len(world_state.scene_history)}",
        ),
        authority_ref=authority_ref,
        evidence_refs=evidence_refs,
    )


def _require_text(value: object, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise CreativeWorldRejected(f"{field_name} must be non-empty")


def _require_text_tuple(values: tuple[str, ...], field_name: str) -> None:
    if not isinstance(values, tuple) or not values:
        raise CreativeWorldRejected(f"{field_name} must contain non-empty strings")
    if any(not isinstance(value, str) or not value.strip() for value in values):
        raise CreativeWorldRejected(f"{field_name} must contain non-empty strings")


__all__ = [
    "CreativeExportPlan",
    "CreativeExportTarget",
    "build_creative_export_plan",
]
