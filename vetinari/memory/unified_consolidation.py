"""Consolidation and episode-promotion helpers for UnifiedMemoryStore.

This mixin owns the transition from short-lived session context and episodic
records into durable semantic memory entries.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from typing import TYPE_CHECKING, Any

from vetinari.exceptions import StorageError
from vetinari.types import MemoryType

from ._store_episode import row_to_episode_dict
from ._store_ops import evict_low_importance_memories
from .interfaces import MemoryEntry
from .unified_config import CONSOLIDATION_QUALITY_THRESHOLD, EPISODE_PROMOTION_THRESHOLD

logger = logging.getLogger(__name__)

_PATTERN_SUMMARY_EPISODE_LIMIT = 5  # Keep promoted pattern content concise while preserving examples.
_MIN_PROMOTION_QUALITY = 0.80
_BLOCKING_GOVERNANCE_FLAGS = {
    "candidate_only",
    "requires_memory_firewall",
    "promotion_blocked",
    "privacy_blocked",
    "policy_blocked",
}


class _ConsolidationMixin:
    """Session consolidation and episodic-to-semantic promotion methods."""

    if TYPE_CHECKING:
        _check_semantic_duplicate: Any
        _conn: Any
        _lock: Any
        _max_entries: Any
        remember: Any
        session: Any

    def consolidate(self, quality_threshold: float = CONSOLIDATION_QUALITY_THRESHOLD) -> int:
        """Promote session entries (quality >= threshold) to long-term memory.

        Returns:
            Number of session entries successfully promoted to the long-term store.
        """
        entries = self.session.get_all()
        promoted = 0
        for session_entry in entries:
            if session_entry.quality_score < quality_threshold:
                continue
            value = session_entry.value
            if isinstance(value, dict) and "content" in value:
                mem_entry = MemoryEntry.from_dict(value)
            elif isinstance(value, str):
                mem_entry = MemoryEntry(content=value, provenance="session_consolidation")
            else:
                continue
            if self._check_semantic_duplicate(mem_entry.content):
                logger.debug("Skipping semantic duplicate during consolidation: %s", session_entry.key)
                continue
            try:
                self.remember(mem_entry)
                promoted += 1
            except (RuntimeError, StorageError):
                logger.warning("Consolidation failed for entry %s - skipping", session_entry.key)
        if promoted > 0:
            with self._lock:
                evict_low_importance_memories(self._conn, self._max_entries)
        logger.info("Consolidated %s of %s session entries to long-term", promoted, len(entries))
        return promoted

    def promote_episodes_to_semantic(
        self,
        threshold: int = EPISODE_PROMOTION_THRESHOLD,
    ) -> int:
        """Extract recurring patterns from episodic memory into semantic rules.

        Groups successful episodes by task_type. When a group reaches
        *threshold* members, a PATTERN memory is created that summarises
        the common approach, and source episodes are marked consolidated
        (``promoted=1`` in metadata) so they are not promoted again.

        Args:
            threshold: Minimum number of similar successful episodes required
                before a pattern is extracted.

        Returns:
            Number of new semantic pattern entries created.
        """
        with self._lock:
            rows = self._conn.execute("SELECT * FROM memory_episodes WHERE success = 1").fetchall()

        if not rows:
            return 0

        groups = _group_promotable_episodes(rows)
        promoted = 0
        for task_type, episodes in groups.items():
            if len(episodes) < threshold:
                continue

            entry = _build_pattern_entry(task_type, episodes)
            try:
                stored_id = self.remember(entry)
                if stored_id == entry.id:
                    promoted += 1
            except StorageError:
                logger.warning(
                    "Failed to store promoted pattern for task_type=%s - skipping",
                    task_type,
                )
                continue

            _mark_episodes_promoted(self, episodes)

        if promoted > 0:
            logger.info("Promoted %d episode groups to semantic patterns", promoted)
        return promoted


def _group_promotable_episodes(rows: list[sqlite3.Row]) -> dict[str, list[dict[str, Any]]]:
    """Group successful, unpromoted episodes by task type."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        episode = row_to_episode_dict(row)
        if not _episode_is_promotable(episode):
            continue
        key = episode.get("task_type", "unknown")
        groups.setdefault(key, []).append(episode)
    return groups


def _episode_is_promotable(episode: dict[str, Any]) -> bool:
    """Return whether an episode may become durable semantic memory."""
    metadata = episode.get("metadata") or {}
    if metadata.get("promoted"):
        return False
    if float(episode.get("quality_score") or 0.0) < _MIN_PROMOTION_QUALITY:
        return False
    if any(metadata.get(flag) for flag in _BLOCKING_GOVERNANCE_FLAGS):
        return False
    verdict = str(metadata.get("policy_verdict", metadata.get("governance_verdict", "allow"))).lower()
    return verdict not in {"deny", "denied", "block", "blocked", "reject", "rejected"}


def _build_pattern_entry(task_type: str, episodes: list[dict[str, Any]]) -> MemoryEntry:
    """Build a semantic PATTERN memory from a group of successful episodes."""
    sorted_episodes = sorted(episodes, key=lambda episode: episode["quality_score"], reverse=True)
    top_episodes = sorted_episodes[:_PATTERN_SUMMARY_EPISODE_LIMIT]
    summaries = [episode["task_summary"] for episode in top_episodes]
    avg_quality = sum(episode["quality_score"] for episode in episodes) / len(episodes)
    source_ids = [episode["episode_id"] for episode in episodes]

    pattern_content = (
        f"Recurring pattern for task_type={task_type} "
        f"(observed {len(episodes)} times, avg quality {avg_quality:.2f}):\n"
        + "\n".join(f"- {summary}" for summary in summaries)
    )

    return MemoryEntry(
        id=f"pattern_{uuid.uuid4().hex[:8]}",
        agent="system",
        entry_type=MemoryType.PATTERN,
        content=pattern_content,
        summary=f"Extracted pattern: {task_type} ({len(episodes)} episodes)",
        provenance="episode_promotion",
        metadata={
            "source_episode_ids": source_ids,
            "task_type": task_type,
            "episode_count": len(episodes),
            "avg_quality": round(avg_quality, 3),
            "promotion_quality_gate": _MIN_PROMOTION_QUALITY,
            "governance_gate": "deny/block/reject and candidate-only metadata excluded",
        },
    )


def _mark_episodes_promoted(store: Any, episodes: list[dict[str, Any]]) -> None:
    """Set the promoted metadata flag on source episodes after pattern storage."""
    metadata_updates = []
    for episode in episodes:
        metadata = dict(episode.get("metadata") or {})
        metadata["promoted"] = True
        metadata_updates.append((json.dumps(metadata), episode["episode_id"]))
    with store._lock:
        try:
            store._conn.executemany(
                "UPDATE memory_episodes SET metadata_json = ? WHERE episode_id = ?",
                metadata_updates,
            )
            store._conn.commit()
        except sqlite3.Error:
            logger.warning(
                "Could not update metadata for %d episodes during promotion - pattern was stored but metadata flags were not set",
                len(metadata_updates),
            )
