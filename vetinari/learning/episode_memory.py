"""Legacy episodic-memory compatibility over canonical storage."""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from vetinari.database import get_connection
from vetinari.memory.episodic import CanonicalEpisode, UnifiedEpisodicMemory

logger = logging.getLogger(__name__)


def _simple_embedding(text: str, dim: int = 256) -> list[float]:
    """Return the canonical lightweight trigram embedding used by learning prototypes."""
    import hashlib

    vec = [0.0] * dim
    value = text
    for index in range(max(0, len(value) - 2)):
        gram = value[index : index + 3].lower()
        digest = hashlib.md5(gram.encode(), usedforsecurity=False).hexdigest()
        vec[int(digest, 16) % dim] += 1.0
    norm = (sum(item * item for item in vec) ** 0.5) or 1.0
    return [item / norm for item in vec]


@dataclass
class MemoryRecordedEpisode:
    """Legacy return shape for a single past execution record."""

    episode_id: str
    timestamp: str
    task_summary: str
    agent_type: str
    task_type: str
    output_summary: str
    quality_score: float
    success: bool
    model_id: str
    embedding: tuple[float, ...] = field(default_factory=tuple)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __repr__(self) -> str:
        return (
            f"Episode(episode_id={self.episode_id!r}, agent_type={self.agent_type!r}, "
            f"task_type={self.task_type!r}, quality_score={self.quality_score!r}, "
            f"success={self.success!r})"
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize this episode to a plain dictionary."""
        return {
            "episode_id": self.episode_id,
            "timestamp": self.timestamp,
            "task_summary": self.task_summary,
            "agent_type": self.agent_type,
            "task_type": self.task_type,
            "output_summary": self.output_summary,
            "quality_score": self.quality_score,
            "success": self.success,
            "model_id": self.model_id,
            "metadata": self.metadata,
        }


class EpisodeMemory:
    """Compatibility facade that delegates current writes to canonical memory."""

    _instance: EpisodeMemory | None = None
    _cls_lock = threading.Lock()

    def __init__(self, episodic_memory: UnifiedEpisodicMemory | None = None) -> None:
        self._lock = threading.RLock()
        self._episodic = episodic_memory if episodic_memory is not None else UnifiedEpisodicMemory()
        self._legacy_rows_migrated = False
        self._migrate_legacy_rows()

    @classmethod
    def get_instance(cls) -> EpisodeMemory:
        """Return the process singleton.

        Returns:
            Resolved instance value.
        """
        with cls._cls_lock:
            if cls._instance is None:
                cls._instance = cls()
        return cls._instance

    def record(
        self,
        task_description: str,
        agent_type: str,
        task_type: str,
        output_summary: str,
        quality_score: float,
        success: bool,
        model_id: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Record a new episode through canonical ``memory_episodes`` storage.

        Args:
            task_description: Task description value consumed by record().
            agent_type: Agent type value consumed by record().
            task_type: Task type value consumed by record().
            output_summary: Output summary value consumed by record().
            quality_score: Score value evaluated by the operation.
            success: Success value consumed by record().
            model_id: Model identifier used for routing or lookup.
            metadata: Structured data consumed by the operation.

        Returns:
            str value produced by record().
        """
        canonical = CanonicalEpisode(
            task_summary=task_description[:300],
            agent_type=agent_type,
            task_type=task_type,
            output_summary=output_summary[:500],
            quality_score=quality_score,
            success=success,
            model_id=model_id,
            importance=round(quality_score * (1.0 if success else 0.5), 3),
            provenance="legacy_episode_memory",
            metadata=metadata or {},
        )
        episode_id = self._episodic.record(canonical)
        self._mirror_legacy_row(episode_id, canonical)
        return episode_id

    def recall(
        self,
        query: str,
        k: int = 5,
        min_score: float = 0.0,
        task_type: str | None = None,
        successful_only: bool = False,
    ) -> list[MemoryRecordedEpisode]:
        """Return legacy-shaped episodes from canonical recall.

        Args:
            query: Query value consumed by recall().
            k: K value consumed by recall().
            min_score: Score value evaluated by the operation.
            task_type: Task type value consumed by recall().
            successful_only: Successful only value consumed by recall().

        Returns:
            list[MemoryRecordedEpisode] value produced by recall().
        """
        if not self._legacy_rows_migrated:
            self._migrate_legacy_rows()
        episodes = self._episodic.recall(
            query=query,
            k=k,
            min_score=min_score,
            task_type=task_type,
            successful_only=successful_only,
        )
        return [self._to_legacy(episode) for episode in episodes]

    def get_failure_patterns(self, agent_type: str, task_type: str) -> list[str]:
        """Return recent failed output summaries from canonical storage."""
        return self._episodic.get_failure_patterns(agent_type, task_type)

    def get_stats(self) -> dict[str, Any]:
        """Return legacy stat keys backed by canonical storage.

        Returns:
            Resolved stats value.
        """
        stats = self._episodic.get_stats()
        return {
            "total_episodes": stats.get("total_episodes", 0),
            "successful": stats.get("successful", 0),
            "avg_quality_score": stats.get("avg_quality_score", 0.0),
            "index_size": stats.get("total_episodes", 0),
        }

    def _migrate_legacy_rows(self) -> None:
        """Move old rows into canonical storage when the old table already exists."""
        with self._lock:
            if self._legacy_rows_migrated:
                return
            try:
                conn = get_connection()
                if not self._legacy_table_exists(conn):
                    self._legacy_rows_migrated = True
                    return
                rows = conn.execute(
                    "SELECT episode_id, timestamp, task_summary, agent_type, task_type, "
                    "output_summary, quality_score, success, model_id, metadata, created_at, importance "
                    "FROM episode_memory_store"
                ).fetchall()
                for row in rows:
                    self._migrate_legacy_row(conn, row)
                conn.commit()
                self._legacy_rows_migrated = True
            except sqlite3.Error as exc:
                logger.warning("[EpisodeMemory] Legacy row adaptation failed: %s", exc)

    @staticmethod
    def _legacy_table_exists(conn: sqlite3.Connection) -> bool:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
            ("episode_memory_store",),
        ).fetchone()
        return row is not None

    @staticmethod
    def _migrate_legacy_row(conn: sqlite3.Connection, row: sqlite3.Row) -> None:
        existing = conn.execute(
            "SELECT episode_id FROM memory_episodes WHERE episode_id = ?",
            (row["episode_id"],),
        ).fetchone()
        if existing is not None:
            return
        metadata = _safe_json_loads(row["metadata"])
        metadata.setdefault("provenance", "legacy_episode_memory_store")
        metadata["legacy_episode_memory_store"] = True
        metadata["candidate_status"] = "candidate"
        metadata["candidate_only"] = True
        metadata["requires_memory_firewall"] = True
        conn.execute(
            """INSERT INTO memory_episodes
               (episode_id, timestamp, task_summary, agent_type, task_type,
                output_summary, quality_score, success, model_id, importance, metadata_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                row["episode_id"],
                row["timestamp"] or datetime.now(timezone.utc).isoformat(),
                row["task_summary"],
                row["agent_type"],
                row["task_type"],
                row["output_summary"],
                float(row["quality_score"] or 0.0),
                int(row["success"] or 0),
                row["model_id"] or "",
                float(row["importance"] or 0.5),
                json.dumps(metadata),
            ),
        )

    def _mirror_legacy_row(self, episode_id: str, episode: CanonicalEpisode) -> None:
        with self._lock:
            try:
                conn = get_connection()
                if not self._legacy_table_exists(conn):
                    return
                existing = conn.execute(
                    "SELECT episode_id FROM episode_memory_store WHERE episode_id = ?",
                    (episode_id,),
                ).fetchone()
                if existing is not None:
                    return
                conn.execute(
                    """INSERT INTO episode_memory_store
                       (episode_id, timestamp, task_summary, agent_type, task_type,
                        output_summary, quality_score, success, model_id, metadata)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        episode_id,
                        episode.timestamp,
                        episode.task_summary,
                        episode.agent_type,
                        episode.task_type,
                        episode.output_summary,
                        float(episode.quality_score),
                        int(episode.success),
                        episode.model_id,
                        json.dumps(episode.metadata_for_storage()),
                    ),
                )
                conn.commit()
            except sqlite3.Error as exc:
                logger.warning("[EpisodeMemory] Legacy row mirror failed for %s: %s", episode_id, exc)

    @staticmethod
    def _to_legacy(episode: CanonicalEpisode) -> MemoryRecordedEpisode:
        return MemoryRecordedEpisode(
            episode_id=episode.episode_id,
            timestamp=episode.timestamp,
            task_summary=episode.task_summary,
            agent_type=episode.agent_type,
            task_type=episode.task_type,
            output_summary=episode.output_summary,
            quality_score=episode.quality_score,
            success=episode.success,
            model_id=episode.model_id,
            embedding=(),
            metadata=dict(episode.metadata),
        )


def _safe_json_loads(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        loaded = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        logger.warning("Handled recoverable failure before fallback.", exc_info=True)
        return {}
    return loaded if isinstance(loaded, dict) else {}


_episode_memory: EpisodeMemory | None = None
_mem_lock = threading.Lock()


def get_episode_memory() -> EpisodeMemory:
    """Return the global legacy-compatible EpisodeMemory singleton.

    Returns:
        Resolved episode memory value.
    """
    global _episode_memory
    if _episode_memory is None:
        with _mem_lock:
            if _episode_memory is None:
                _episode_memory = EpisodeMemory.get_instance()
    return _episode_memory
