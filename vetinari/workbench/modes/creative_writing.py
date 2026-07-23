"""Creative writing mode contract for AM Workbench."""

from __future__ import annotations

from dataclasses import dataclass

CREATIVE_WRITING_TEMPLATE_ID = "creative_writing"
CREATIVE_WRITING_REQUIRED_ARTIFACTS = (
    "story_bible",
    "style_bible",
    "world_bible",
    "continuity_ledger",
    "scene_beats",
    "voice_conformance_report",
    "draft_branches",
    "export_manifest",
)


class CreativeWritingModeRejected(ValueError):
    """Raised when creative writing state lacks continuity proof."""


@dataclass(frozen=True, slots=True)
class CharacterContinuity:
    """Character continuity facts that must remain stable across drafts."""

    character_id: str
    canonical_traits: tuple[str, ...]
    open_questions: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_non_empty(self.character_id, "character_id")
        _require_non_empty_tuple(self.canonical_traits, "canonical_traits")


@dataclass(frozen=True, slots=True)
class TimelineEvent:
    """Ordered world/story timeline event."""

    event_id: str
    sequence: int
    summary: str

    def __post_init__(self) -> None:
        _require_non_empty(self.event_id, "event_id")
        _require_non_empty(self.summary, "summary")
        if self.sequence < 0:
            raise CreativeWritingModeRejected("timeline sequence must be non-negative")


@dataclass(frozen=True, slots=True)
class SceneBeat:
    """Scene beat with point-of-view and continuity refs."""

    beat_id: str
    pov_character_id: str
    timeline_event_ids: tuple[str, ...]
    purpose: str

    def __post_init__(self) -> None:
        _require_non_empty(self.beat_id, "beat_id")
        _require_non_empty(self.pov_character_id, "pov_character_id")
        _require_non_empty_tuple(self.timeline_event_ids, "timeline_event_ids")
        _require_non_empty(self.purpose, "purpose")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"SceneBeat(beat_id={self.beat_id!r}, pov_character_id={self.pov_character_id!r}, timeline_event_ids={self.timeline_event_ids!r})"


@dataclass(frozen=True, slots=True)
class CreativeWritingModeState:
    """Promotion-ready creative writing workspace state."""

    story_bible_ref: str
    style_bible_ref: str
    world_bible_ref: str
    characters: tuple[CharacterContinuity, ...]
    timeline: tuple[TimelineEvent, ...]
    scene_beats: tuple[SceneBeat, ...]
    voice_conformance_score: float
    draft_branch_ids: tuple[str, ...]
    export_targets: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_non_empty(self.story_bible_ref, "story_bible_ref")
        _require_non_empty(self.style_bible_ref, "style_bible_ref")
        _require_non_empty(self.world_bible_ref, "world_bible_ref")
        if not self.characters or not self.timeline or not self.scene_beats:
            raise CreativeWritingModeRejected("creative writing requires characters, timeline, and scene beats")
        if not 0 <= self.voice_conformance_score <= 1:
            raise CreativeWritingModeRejected("voice conformance score must be between 0 and 1")
        _require_non_empty_tuple(self.draft_branch_ids, "draft_branch_ids")
        _require_non_empty_tuple(self.export_targets, "export_targets")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"CreativeWritingModeState(story_bible_ref={self.story_bible_ref!r}, style_bible_ref={self.style_bible_ref!r}, world_bible_ref={self.world_bible_ref!r})"


def require_creative_writing_ready(state: CreativeWritingModeState) -> None:
    """Reject creative writing output with broken continuity or weak voice fit.

    Raises:
        Exception: Propagates validation or runtime failures from the underlying operation.
    """
    character_ids = {character.character_id for character in state.characters}
    timeline_ids = {event.event_id for event in state.timeline}
    for beat in state.scene_beats:
        if beat.pov_character_id not in character_ids:
            raise CreativeWritingModeRejected(f"scene beat {beat.beat_id!r} references unknown character")
        missing_events = set(beat.timeline_event_ids) - timeline_ids
        if missing_events:
            raise CreativeWritingModeRejected(f"scene beat {beat.beat_id!r} references unknown timeline events")
    if state.voice_conformance_score < 0.8:
        raise CreativeWritingModeRejected("voice conformance score below promotion threshold")


def _require_non_empty(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise CreativeWritingModeRejected(f"{field_name} must be non-empty")


def _require_non_empty_tuple(values: tuple[str, ...], field_name: str) -> None:
    if (
        not isinstance(values, tuple)
        or not values
        or not all(isinstance(value, str) and value.strip() for value in values)
    ):
        raise CreativeWritingModeRejected(f"{field_name} must contain non-empty strings")


__all__ = [
    "CREATIVE_WRITING_REQUIRED_ARTIFACTS",
    "CREATIVE_WRITING_TEMPLATE_ID",
    "CharacterContinuity",
    "CreativeWritingModeRejected",
    "CreativeWritingModeState",
    "SceneBeat",
    "TimelineEvent",
    "require_creative_writing_ready",
]
