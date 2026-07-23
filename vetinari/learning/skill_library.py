"""SAGE-inspired skill library — reusable task templates from successful episodes.

Extracts recurring successful execution patterns into typed Skill objects
that the Foreman can consult before planning from scratch.  Inspired by
the SAGE paper's finding of 59% token reduction when reusing task templates.

This is part of the learning pipeline: Episodes → Promotion → **Skills** → Foreman lookup.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

from vetinari.memory.interfaces import MemoryEntry, MemoryType
from vetinari.utils import privacy_receipt

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MIN_EPISODES_FOR_SKILL = 5  # minimum successful episodes to extract a skill
SKILL_MATCH_QUALITY_FLOOR = 0.6  # minimum avg quality to create a skill


# ---------------------------------------------------------------------------
# Skill dataclass
# ---------------------------------------------------------------------------


@dataclass
class Skill:
    """Reusable task template extracted from successful episode patterns.

    Attributes:
        id: Unique skill identifier (``skill_<hex>``).
        name: Human-readable skill name derived from the task type.
        task_type: Category of task this skill covers (e.g. ``"coding"``).
        template: Generalised step-by-step template distilled from episodes.
        source_episodes: Episode IDs that contributed to this skill.
        success_count: Number of successful applications of this pattern.
        avg_quality: Average quality score across source episodes.
    """

    id: str
    name: str
    task_type: str
    template: str
    source_episodes: list[str] = field(default_factory=list)
    success_count: int = 0
    avg_quality: float = 0.0

    def __repr__(self) -> str:
        return f"Skill(id={self.id!r}, name={self.name!r}, task_type={self.task_type!r})"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Skill:
        """Reconstruct a Skill from its dict representation.

        Args:
            data: Dictionary with Skill field values.

        Returns:
            Populated Skill instance.
        """
        return cls(
            id=data["id"],
            name=data["name"],
            task_type=data["task_type"],
            template=data["template"],
            source_episodes=data.get("source_episodes", []),
            success_count=data.get("success_count", 0),
            avg_quality=data.get("avg_quality", 0.0),
        )


# ---------------------------------------------------------------------------
# Skill extraction
# ---------------------------------------------------------------------------


def extract_skill(episodes: list[dict[str, Any]]) -> Skill | None:
    """Analyse successful episodes and distil a reusable Skill template.

    Only creates a skill when there are enough high-quality episodes
    (at least ``MIN_EPISODES_FOR_SKILL`` with avg quality above
    ``SKILL_MATCH_QUALITY_FLOOR``).

    Args:
        episodes: List of episode dicts (from ``row_to_episode_dict``).
            Each must have ``task_summary``, ``output_summary``,
            ``quality_score``, ``success``, ``task_type``, ``episode_id``.

    Returns:
        A new Skill, or None if the episodes are insufficient.
    """
    successful = [e for e in episodes if e.get("success")]
    if len(successful) < MIN_EPISODES_FOR_SKILL:
        logger.debug(
            "Only %d successful episodes (need %d) — skipping skill extraction",
            len(successful),
            MIN_EPISODES_FOR_SKILL,
        )
        return None

    avg_quality = sum(e.get("quality_score", 0.0) for e in successful) / len(successful)
    if avg_quality < SKILL_MATCH_QUALITY_FLOOR:
        logger.debug(
            "Average quality %.2f below floor %.2f — skipping skill extraction",
            avg_quality,
            SKILL_MATCH_QUALITY_FLOOR,
        )
        return None

    # Sort by quality, take the best examples to build the template
    top = sorted(successful, key=lambda e: e.get("quality_score", 0.0), reverse=True)[:5]
    task_type = top[0].get("task_type", "general")

    # Build a generalised template from the best-performing episodes
    steps: list[str] = []
    for i, ep in enumerate(top, 1):
        steps.append(f"{i}. {ep.get('task_summary', 'N/A')} → {ep.get('output_summary', 'N/A')}")

    template = (
        f"Skill template for '{task_type}' tasks "
        f"(distilled from {len(successful)} successful executions):\n" + "\n".join(steps)
    )

    skill = Skill(
        id=f"skill_{uuid.uuid4().hex[:8]}",
        name=f"{task_type} skill",
        task_type=task_type,
        template=template,
        source_episodes=[e["episode_id"] for e in successful],
        success_count=len(successful),
        avg_quality=round(avg_quality, 3),
    )
    logger.info(
        "Extracted skill %s for task_type=%s from %d episodes (avg quality %.2f)",
        skill.id,
        task_type,
        len(successful),
        avg_quality,
    )
    return skill


# ---------------------------------------------------------------------------
# Skill persistence (via unified memory store)
# ---------------------------------------------------------------------------


def _skill_to_entry(skill: Skill) -> MemoryEntry:
    """Convert a Skill to a MemoryEntry for storage in the unified store."""
    metadata = skill.to_dict()
    metadata["privacy_receipt"] = privacy_receipt(
        privacy_class="operational",
        retention_days=30,
        source="skill_library.skill_metadata",
        redaction_applied=True,
    )
    return MemoryEntry(
        id=skill.id,
        agent="system",
        entry_type=MemoryType.SKILL,
        content=skill.template,
        summary=f"Skill: {skill.name} ({skill.success_count} episodes, quality {skill.avg_quality:.2f})",
        provenance="skill_extraction",
        metadata=metadata,
    )


def _entry_to_skill(entry: MemoryEntry) -> Skill | None:
    """Reconstruct a Skill from its MemoryEntry metadata."""
    if not entry.metadata or not isinstance(entry.metadata, dict):
        return None
    try:
        return Skill.from_dict(entry.metadata)
    except (KeyError, TypeError):
        logger.warning("Could not reconstruct skill from entry %s — metadata malformed", entry.id)
        return None


def store_skill(skill: Skill) -> str:
    """Persist a Skill as a MemoryEntry in the unified store.

    Args:
        skill: The Skill to persist.

    Returns:
        The stored entry ID.
    """
    from vetinari.memory.unified import get_unified_store

    store = get_unified_store()
    entry = _skill_to_entry(skill)
    return store.remember(entry)


def get_skill(skill_id: str) -> Skill | None:
    """Retrieve a specific skill by its ID.

    Args:
        skill_id: The skill ID (e.g. ``skill_abc12345``).

    Returns:
        The Skill if found, None otherwise.
    """
    from vetinari.memory.unified import get_unified_store

    store = get_unified_store()
    entry = store.get_entry(skill_id)
    if entry is None:
        return None
    return _entry_to_skill(entry)


def find_matching_skill(task_description: str, limit: int = 3, task_type: str | None = None) -> Skill | None:
    """Find the best matching skill for a task description using semantic search.

    Searches the memory store for SKILL-type entries that semantically
    match the given task description, returning the highest-quality match.

    Args:
        task_description: Natural language description of the task to match.
        limit: Maximum candidates to consider before picking the best.
        task_type: Optional task category. When supplied, candidates for other
            task types are ignored before quality ranking.

    Returns:
        The best-matching Skill, or None when nothing relevant is found.
    """
    from vetinari.memory.unified import get_unified_store

    store = get_unified_store()
    results = store.search(
        task_description,
        entry_types=[MemoryType.SKILL.value],
        limit=limit,
        use_semantic=True,
    )
    if not results:
        return None

    # Pick the result with the highest avg_quality among matches
    best: Skill | None = None
    for entry in results:
        skill = _entry_to_skill(entry)
        if skill is None:
            continue
        if task_type is not None and skill.task_type != task_type:
            continue
        if best is None or skill.avg_quality > best.avg_quality:
            best = skill

    if best is not None:
        logger.debug("Matched skill %s (quality %.2f) for task: %s", best.id, best.avg_quality, task_description[:80])
    return best
