"""Creative roleplay studio world contract."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, ValidationError

from vetinari.workbench.modes.creative_writing import CharacterContinuity, SceneBeat, TimelineEvent

SCHEMA_PATH = Path(__file__).resolve().parents[3] / "schemas" / "workbench_creative_world.schema.json"


class CreativeWorldRejected(ValueError):
    """Raised when creative world state is missing required proof."""


@dataclass(frozen=True, slots=True)
class CharacterCard:
    """Runtime contract for CharacterCard."""

    character_id: str
    display_name: str
    summary: str
    continuity: CharacterContinuity
    authority_ref: str
    provenance_ref: str
    portrait_ref: str | None = None
    branch_ref: str = "canon"

    def __post_init__(self) -> None:
        _require_text(self.character_id, "character_id")
        _require_text(self.display_name, "display_name")
        _require_text(self.summary, "summary")
        _require_text(self.authority_ref, "authority_ref")
        _require_text(self.provenance_ref, "provenance_ref")
        _require_text(self.branch_ref, "branch_ref")
        if self.character_id != self.continuity.character_id:
            raise CreativeWorldRejected("character_id must match continuity.character_id")
        if self.portrait_ref is not None:
            _require_text(self.portrait_ref, "portrait_ref")

    @property
    def canonical_traits(self) -> tuple[str, ...]:
        return self.continuity.canonical_traits

    @property
    def open_questions(self) -> tuple[str, ...]:
        return self.continuity.open_questions

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"CharacterCard(character_id={self.character_id!r}, display_name={self.display_name!r}, summary={self.summary!r})"


@dataclass(frozen=True, slots=True)
class WorldBible:
    """Runtime contract for WorldBible."""

    world_id: str
    title: str
    summary: str
    authority_ref: str
    provenance_ref: str

    def __post_init__(self) -> None:
        _require_text(self.world_id, "world_id")
        _require_text(self.title, "title")
        _require_text(self.summary, "summary")
        _require_text(self.authority_ref, "authority_ref")
        _require_text(self.provenance_ref, "provenance_ref")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"WorldBible(world_id={self.world_id!r}, title={self.title!r}, summary={self.summary!r})"


@dataclass(frozen=True, slots=True)
class RelationshipMap:
    """Runtime contract for RelationshipMap."""

    relationship_id: str
    source_character_id: str
    target_character_id: str
    relationship_type: str
    status: str
    evidence_refs: tuple[str, ...]
    authority_ref: str
    provenance_ref: str

    def __post_init__(self) -> None:
        _require_text(self.relationship_id, "relationship_id")
        _require_text(self.source_character_id, "source_character_id")
        _require_text(self.target_character_id, "target_character_id")
        if self.source_character_id == self.target_character_id:
            raise CreativeWorldRejected("relationship endpoints must be distinct")
        _require_text(self.relationship_type, "relationship_type")
        _require_text(self.status, "status")
        _require_text_tuple(self.evidence_refs, "evidence_refs")
        _require_text(self.authority_ref, "authority_ref")
        _require_text(self.provenance_ref, "provenance_ref")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"RelationshipMap(relationship_id={self.relationship_id!r}, source_character_id={self.source_character_id!r}, target_character_id={self.target_character_id!r})"


@dataclass(frozen=True, slots=True)
class SceneHistoryEntry:
    """Runtime contract for SceneHistoryEntry."""

    entry_id: str
    scene: SceneBeat
    summary: str
    outcome: str
    evidence_refs: tuple[str, ...]
    authority_ref: str
    provenance_ref: str

    def __post_init__(self) -> None:
        _require_text(self.entry_id, "entry_id")
        _require_text(self.summary, "summary")
        _require_text(self.outcome, "outcome")
        _require_text_tuple(self.evidence_refs, "evidence_refs")
        _require_text(self.authority_ref, "authority_ref")
        _require_text(self.provenance_ref, "provenance_ref")

    @property
    def beat_id(self) -> str:
        return self.scene.beat_id

    @property
    def pov_character_id(self) -> str:
        return self.scene.pov_character_id

    @property
    def timeline_event_ids(self) -> tuple[str, ...]:
        return self.scene.timeline_event_ids

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"SceneHistoryEntry(entry_id={self.entry_id!r}, scene={self.scene!r}, summary={self.summary!r})"


@dataclass(frozen=True, slots=True)
class ToneStyleGuide:
    """Runtime contract for ToneStyleGuide."""

    guide_id: str
    voice_summary: str
    style_rules: tuple[str, ...]
    forbidden_tones: tuple[str, ...]
    authority_ref: str
    provenance_ref: str

    def __post_init__(self) -> None:
        _require_text(self.guide_id, "guide_id")
        _require_text(self.voice_summary, "voice_summary")
        _require_text_tuple(self.style_rules, "style_rules")
        _require_text_tuple(self.forbidden_tones, "forbidden_tones", allow_empty=True)
        _require_text(self.authority_ref, "authority_ref")
        _require_text(self.provenance_ref, "provenance_ref")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"ToneStyleGuide(guide_id={self.guide_id!r}, voice_summary={self.voice_summary!r}, style_rules={self.style_rules!r})"


@dataclass(frozen=True, slots=True)
class CreativeWorldState:
    """Runtime contract for CreativeWorldState."""

    world_bible: WorldBible
    tone_style_guide: ToneStyleGuide
    characters: tuple[CharacterCard, ...]
    relationships: tuple[RelationshipMap, ...]
    timeline: tuple[TimelineEvent, ...]
    scene_history: tuple[SceneHistoryEntry, ...]
    authority_ref: str
    provenance_ref: str

    def __post_init__(self) -> None:
        if not self.characters:
            raise CreativeWorldRejected("creative world requires characters")
        if not self.timeline:
            raise CreativeWorldRejected("creative world requires timeline events")
        if not self.scene_history:
            raise CreativeWorldRejected("creative world requires scene history")
        _require_text(self.authority_ref, "authority_ref")
        _require_text(self.provenance_ref, "provenance_ref")
        if self.world_bible.authority_ref != self.authority_ref:
            raise CreativeWorldRejected("world authority must match aggregate authority")
        if self.world_bible.provenance_ref != self.provenance_ref:
            raise CreativeWorldRejected("world provenance must match aggregate provenance")
        authoritative_children = (
            (self.tone_style_guide.authority_ref, self.tone_style_guide.provenance_ref, "tone style guide"),
            *((item.authority_ref, item.provenance_ref, f"character {item.character_id}") for item in self.characters),
            *(
                (item.authority_ref, item.provenance_ref, f"relationship {item.relationship_id}")
                for item in self.relationships
            ),
            *((item.authority_ref, item.provenance_ref, f"scene {item.entry_id}") for item in self.scene_history),
        )
        for authority_ref, provenance_ref, label in authoritative_children:
            if authority_ref != self.authority_ref:
                raise CreativeWorldRejected(f"{label} authority must match aggregate authority")
            if provenance_ref != self.provenance_ref:
                raise CreativeWorldRejected(f"{label} provenance must match aggregate provenance")

    @property
    def world_id(self) -> str:
        return self.world_bible.world_id

    @property
    def title(self) -> str:
        return self.world_bible.title

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"CreativeWorldState(world_bible={self.world_bible!r}, tone_style_guide={self.tone_style_guide!r}, characters={self.characters!r})"


def load_creative_world(path: str | Path) -> CreativeWorldState:
    """Load and validate a creative world payload.

    Returns:
        Resolved creative world value.

    Raises:
        Exception: Propagates validation or runtime failures from the underlying operation.
    """
    payload_path = Path(path)
    try:
        payload = json.loads(payload_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CreativeWorldRejected(f"creative world payload unreadable: {exc}") from exc

    schema = _load_schema()
    try:
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema).validate(payload)
    except ValidationError as exc:
        path_text = ".".join(str(part) for part in exc.absolute_path) or "<root>"
        raise CreativeWorldRejected(f"creative world schema rejected {path_text}: {exc.message}") from exc

    return _world_from_payload(payload)


def _world_from_payload(payload: dict[str, Any]) -> CreativeWorldState:
    authority_ref = payload["authority_ref"]
    provenance_ref = payload["provenance_ref"]
    world = WorldBible(
        world_id=payload["world_id"],
        title=payload["title"],
        summary=payload["summary"],
        authority_ref=authority_ref,
        provenance_ref=provenance_ref,
    )
    guide_payload = payload["tone_style_guide"]
    guide = ToneStyleGuide(
        guide_id=guide_payload["guide_id"],
        voice_summary=guide_payload["voice_summary"],
        style_rules=tuple(guide_payload["style_rules"]),
        forbidden_tones=tuple(guide_payload.get("forbidden_tones", ())),
        authority_ref=guide_payload["authority_ref"],
        provenance_ref=guide_payload["provenance_ref"],
    )
    characters = tuple(_character_from_payload(item) for item in payload["characters"])
    relationships = tuple(_relationship_from_payload(item) for item in payload["relationships"])
    timeline = tuple(
        TimelineEvent(event_id=item["event_id"], sequence=item["sequence"], summary=item["summary"])
        for item in payload["timeline"]
    )
    scene_history = tuple(_scene_from_payload(item) for item in payload["scene_history"])
    return CreativeWorldState(
        world_bible=world,
        tone_style_guide=guide,
        characters=characters,
        relationships=relationships,
        timeline=timeline,
        scene_history=scene_history,
        authority_ref=authority_ref,
        provenance_ref=provenance_ref,
    )


def _character_from_payload(payload: dict[str, Any]) -> CharacterCard:
    continuity = CharacterContinuity(
        character_id=payload["character_id"],
        canonical_traits=tuple(payload["canonical_traits"]),
        open_questions=tuple(payload.get("open_questions", ())),
    )
    return CharacterCard(
        character_id=payload["character_id"],
        display_name=payload["display_name"],
        summary=payload["summary"],
        continuity=continuity,
        authority_ref=payload["authority_ref"],
        provenance_ref=payload["provenance_ref"],
        portrait_ref=payload.get("portrait_ref"),
        branch_ref=payload.get("branch_ref", "canon"),
    )


def _relationship_from_payload(payload: dict[str, Any]) -> RelationshipMap:
    return RelationshipMap(
        relationship_id=payload["relationship_id"],
        source_character_id=payload["source_character_id"],
        target_character_id=payload["target_character_id"],
        relationship_type=payload["relationship_type"],
        status=payload["status"],
        evidence_refs=tuple(payload["evidence_refs"]),
        authority_ref=payload["authority_ref"],
        provenance_ref=payload["provenance_ref"],
    )


def _scene_from_payload(payload: dict[str, Any]) -> SceneHistoryEntry:
    scene = SceneBeat(
        beat_id=payload["beat_id"],
        pov_character_id=payload["pov_character_id"],
        timeline_event_ids=tuple(payload["timeline_event_ids"]),
        purpose=payload["purpose"],
    )
    return SceneHistoryEntry(
        entry_id=payload["entry_id"],
        scene=scene,
        summary=payload["summary"],
        outcome=payload["outcome"],
        evidence_refs=tuple(payload["evidence_refs"]),
        authority_ref=payload["authority_ref"],
        provenance_ref=payload["provenance_ref"],
    )


def _load_schema() -> dict[str, Any]:
    try:
        return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CreativeWorldRejected(f"creative world schema unavailable: {exc}") from exc


def _require_text(value: object, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise CreativeWorldRejected(f"{field_name} must be non-empty")


def _require_text_tuple(values: tuple[str, ...], field_name: str, *, allow_empty: bool = False) -> None:
    if not isinstance(values, tuple):
        raise CreativeWorldRejected(f"{field_name} must be a tuple")
    if not allow_empty and not values:
        raise CreativeWorldRejected(f"{field_name} must contain non-empty strings")
    if any(not isinstance(value, str) or not value.strip() for value in values):
        raise CreativeWorldRejected(f"{field_name} must contain non-empty strings")


__all__ = [
    "CharacterCard",
    "CreativeWorldRejected",
    "CreativeWorldState",
    "RelationshipMap",
    "SceneHistoryEntry",
    "ToneStyleGuide",
    "WorldBible",
    "load_creative_world",
]
