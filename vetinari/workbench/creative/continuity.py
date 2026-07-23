"""Continuity checks for creative world state."""

from __future__ import annotations

from dataclasses import dataclass

from vetinari.workbench.creative.world import CreativeWorldState, SceneHistoryEntry


class ContinuityCheckRejected(ValueError):
    """Raised when creative continuity has fail-closed violations."""

    def __init__(self, violations: tuple[ContinuityViolation, ...]) -> None:
        self.violations = violations
        kinds = ", ".join(violation.kind for violation in violations)
        super().__init__(f"creative continuity rejected: {kinds}")


@dataclass(frozen=True, slots=True)
class ContinuityViolation:
    """Runtime contract for ContinuityViolation."""

    violation_id: str
    kind: str
    subject_ref: str
    evidence_refs: tuple[str, ...]
    description: str

    def __post_init__(self) -> None:
        for field_name in ("violation_id", "kind", "subject_ref", "description"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} must be non-empty")
        if not isinstance(self.evidence_refs, tuple) or not self.evidence_refs:
            raise ValueError("evidence_refs must contain non-empty strings")
        if any(not isinstance(ref, str) or not ref.strip() for ref in self.evidence_refs):
            raise ValueError("evidence_refs must contain non-empty strings")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"ContinuityViolation(violation_id={self.violation_id!r}, kind={self.kind!r}, subject_ref={self.subject_ref!r})"


def check_world_continuity(world_state: CreativeWorldState) -> tuple[ContinuityViolation, ...]:
    """Return deterministic continuity violations for a world state.

    Returns:
        Validation outcome for world continuity.
    """
    violations: list[ContinuityViolation] = []
    character_ids = {character.character_id for character in world_state.characters}
    for scene in world_state.scene_history:
        violations.extend(check_scene_against_world(world_state, scene))
    violations.extend(_trait_contradictions(world_state))
    violations.extend(_relationship_contradictions(world_state, character_ids))
    violations.extend(
        ContinuityViolation(
            violation_id=f"relationship-unknown-character:{relationship.relationship_id}",
            kind="relationship-unknown-character",
            subject_ref=relationship.relationship_id,
            evidence_refs=relationship.evidence_refs,
            description="Relationship references a character outside the world cast.",
        )
        for relationship in world_state.relationships
        if (
            relationship.source_character_id not in character_ids
            or relationship.target_character_id not in character_ids
        )
    )
    return tuple(violations)


def check_scene_against_world(
    world_state: CreativeWorldState,
    scene_history_entry: SceneHistoryEntry,
) -> tuple[ContinuityViolation, ...]:
    """Return continuity violations for one scene against the world.

    Args:
        world_state: World state value consumed by check_scene_against_world().
        scene_history_entry: Scene history entry value consumed by check_scene_against_world().

    Returns:
        Validation outcome for scene against world.
    """
    character_ids = {character.character_id for character in world_state.characters}
    timeline_ids = {event.event_id for event in world_state.timeline}
    violations: list[ContinuityViolation] = []
    if scene_history_entry.pov_character_id not in character_ids:
        violations.append(
            ContinuityViolation(
                violation_id=f"pov-not-in-cast:{scene_history_entry.entry_id}",
                kind="pov-not-in-cast",
                subject_ref=scene_history_entry.pov_character_id,
                evidence_refs=scene_history_entry.evidence_refs,
                description="Scene POV character is not present in character cards.",
            )
        )
    missing_events = tuple(
        event_id for event_id in scene_history_entry.timeline_event_ids if event_id not in timeline_ids
    )
    if missing_events:
        violations.append(
            ContinuityViolation(
                violation_id=f"timeline-event-unknown:{scene_history_entry.entry_id}",
                kind="timeline-event-unknown",
                subject_ref=scene_history_entry.entry_id,
                evidence_refs=scene_history_entry.evidence_refs,
                description=f"Scene references unknown timeline events: {', '.join(missing_events)}.",
            )
        )
    return tuple(violations)


def require_continuity_clean(world_state: CreativeWorldState) -> None:
    """Raise when any continuity violation exists.

    Raises:
        Exception: Propagates validation or runtime failures from the underlying operation.
    """
    violations = check_world_continuity(world_state)
    if violations:
        raise ContinuityCheckRejected(violations)


def _trait_contradictions(world_state: CreativeWorldState) -> tuple[ContinuityViolation, ...]:
    by_character: dict[str, set[str]] = {}
    evidence: dict[str, list[str]] = {}
    for character in world_state.characters:
        by_character.setdefault(character.character_id, set()).update(character.canonical_traits)
        evidence.setdefault(character.character_id, []).extend((character.authority_ref, character.provenance_ref))

    violations: list[ContinuityViolation] = []
    for character_id, traits in by_character.items():
        normalized = {trait.casefold() for trait in traits}
        for trait in sorted(normalized):
            if trait.startswith(("not:", "no:")):
                positive = trait.split(":", 1)[1]
                if positive in normalized:
                    violations.append(
                        ContinuityViolation(
                            violation_id=f"trait-contradiction:{character_id}:{positive}",
                            kind="trait-contradiction",
                            subject_ref=character_id,
                            evidence_refs=tuple(dict.fromkeys(evidence[character_id])),
                            description=f"Character has both {positive!r} and {trait!r} traits.",
                        )
                    )
    return tuple(violations)


def _relationship_contradictions(
    world_state: CreativeWorldState,
    character_ids: set[str],
) -> tuple[ContinuityViolation, ...]:
    exclusive_pairs = {
        frozenset(("ally", "enemy")),
        frozenset(("trusts", "distrusts")),
        frozenset(("mentor", "rival")),
        frozenset(("protects", "hunts")),
        frozenset(("friend", "enemy")),
    }
    by_pair: dict[tuple[str, str], list[tuple[str, str, tuple[str, ...]]]] = {}
    for relationship in world_state.relationships:
        if (
            relationship.source_character_id not in character_ids
            or relationship.target_character_id not in character_ids
        ):
            continue
        pair = tuple(sorted((relationship.source_character_id, relationship.target_character_id)))
        by_pair.setdefault(pair, []).append((
            relationship.relationship_id,
            relationship.relationship_type.casefold(),
            relationship.evidence_refs,
        ))
    violations: list[ContinuityViolation] = []
    for pair, entries in by_pair.items():
        types = {entry[1] for entry in entries}
        if any(exclusive <= types for exclusive in exclusive_pairs):
            evidence_refs: list[str] = []
            relationship_ids: list[str] = []
            for relationship_id, _relationship_type, refs in entries:
                relationship_ids.append(relationship_id)
                evidence_refs.extend(refs)
            violations.append(
                ContinuityViolation(
                    violation_id=f"relationship-contradiction:{pair[0]}:{pair[1]}",
                    kind="relationship-contradiction",
                    subject_ref=f"{pair[0]}|{pair[1]}",
                    evidence_refs=tuple(dict.fromkeys(evidence_refs)),
                    description="Relationship map contains mutually exclusive relationship types.",
                )
            )
    return tuple(violations)


__all__ = [
    "ContinuityCheckRejected",
    "ContinuityViolation",
    "check_scene_against_world",
    "check_world_continuity",
    "require_continuity_clean",
]
