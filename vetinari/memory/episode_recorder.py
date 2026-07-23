"""Episode recorder — execution history logging for agents.

Extracted from ``unified.py`` (P0 split).  Persists agent execution
episodes to the ``memory_episodes`` table for retrospective analysis
and learning.  Applies temporal decay and eviction to keep the table
from growing unboundedly.

Decision: scope-aware episode storage (ADR-0077).
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from json import JSONDecodeError
from typing import Any

from vetinari.boundary_guards import account_evidence_drop
from vetinari.database import get_connection

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_EPISODES = 5000  # evict oldest when over this limit
# Failure importance penalty: multiply failure episodes' importance by this
_FAILURE_PENALTY = 0.75  # 25% reduction (was 50% — reduced per item 3.9)


# ---------------------------------------------------------------------------
# Episode dataclass
# ---------------------------------------------------------------------------


class RecordedEpisode:
    """An agent execution episode (immutable record of a completed task)."""

    __slots__ = (
        "agent_type",
        "episode_id",
        "importance",
        "metadata",
        "model_id",
        "output_summary",
        "quality_score",
        "scope",
        "success",
        "task_summary",
        "task_type",
        "timestamp",
    )

    def __init__(
        self,
        *,
        episode_id: str = "",
        timestamp: str = "",
        task_summary: str = "",
        agent_type: str = "",
        task_type: str = "",
        output_summary: str = "",
        quality_score: float = 0.0,
        success: bool = False,
        model_id: str = "",
        importance: float = 0.5,
        metadata: dict[str, Any] | None = None,
        scope: str = "global",
    ) -> None:
        self.episode_id = episode_id or f"ep_{uuid.uuid4().hex[:8]}"
        self.timestamp = timestamp or datetime.now(timezone.utc).isoformat()
        self.task_summary = task_summary
        self.agent_type = agent_type
        self.task_type = task_type
        self.output_summary = output_summary
        self.quality_score = quality_score
        self.success = success
        self.model_id = model_id
        self.importance = importance
        self.metadata = metadata or {}
        self.scope = scope

    def __repr__(self) -> str:
        return (
            f"RecordedEpisode(episode_id={self.episode_id!r}, agent_type={self.agent_type!r}, "
            f"task_type={self.task_type!r}, success={self.success!r})"
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dictionary."""
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
            "importance": self.importance,
            "metadata": self.metadata,
            "scope": self.scope,
        }


# ---------------------------------------------------------------------------
# EpisodeRecorder
# ---------------------------------------------------------------------------


class EpisodeRecorder:
    """Records and recalls agent execution episodes.

    Persists episodes to ``memory_episodes`` via the unified SQLite
    connection.  Evicts oldest episodes when the table exceeds
    ``MAX_EPISODES`` rows.
    """

    @staticmethod
    def _store_episode_embedding(conn: Any, episode_id: str, task_summary: str, output_summary: str) -> None:
        """Store best-effort embedding for episode similarity search."""
        try:
            from vetinari.embeddings import get_embedder
            from vetinari.memory.memory_storage import _pack_embedding

            vec = get_embedder().embed(f"{task_summary} {output_summary}")
            conn.execute(
                "INSERT OR REPLACE INTO episode_embeddings (episode_id, embedding_blob) VALUES (?, ?)",
                (episode_id, _pack_embedding(vec)),
            )
            conn.commit()
        except Exception as exc:
            logger.warning(
                "Episode embedding storage failed for episode %s: %s - episode recorded without embedding",
                episode_id,
                exc,
            )

    def record(
        self,
        *,
        task_summary: str,
        agent_type: str,
        task_type: str,
        output_summary: str,
        quality_score: float = 0.5,
        success: bool = True,
        model_id: str = "",
        scope: str = "global",
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Persist a new episode record.

        Importance is derived from the quality score and success flag.
        Failed episodes receive a 25% importance penalty.

        Args:
            task_summary: Human-readable task description.
            agent_type: The agent type that executed the task.
            task_type: Category of task (e.g. ``"coding"``, ``"review"``).
            output_summary: Brief summary of the output produced.
            quality_score: Quality score in [0.0, 1.0].
            success: Whether the task completed without errors.
            model_id: Identifier of the model used.
            scope: Memory scope for inheritance-aware recall.
            metadata: Additional key-value pairs to store.

        Returns:
            The ``episode_id`` of the newly created record.
        """
        episode_id = f"ep_{uuid.uuid4().hex[:8]}"
        ts = datetime.now(timezone.utc).isoformat()
        importance = quality_score * (1.0 if success else _FAILURE_PENALTY)

        import json

        conn = get_connection()
        conn.execute(
            """INSERT INTO memory_episodes
               (episode_id, timestamp, task_summary, agent_type, task_type,
                output_summary, quality_score, success, model_id, importance,
                metadata_json, scope)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                episode_id,
                ts,
                task_summary,
                agent_type,
                task_type,
                output_summary,
                quality_score,
                int(success),
                model_id,
                importance,
                json.dumps(metadata or {}),
                scope,
            ),
        )
        conn.commit()

        self._store_episode_embedding(conn, episode_id, task_summary, output_summary)
        self._maybe_evict()
        return episode_id

    def recall(
        self,
        *,
        task_type: str | None = None,
        agent_type: str | None = None,
        scope: str = "global",
        limit: int = 20,
        min_quality: float = 0.0,
    ) -> list[RecordedEpisode]:
        """Retrieve recent episodes, optionally filtered by type and scope.

        Scope inheritance: a non-global scope also returns global episodes.

        Args:
            task_type: Filter to a specific task category.
            agent_type: Filter to a specific agent type.
            scope: Scope filter (with global inheritance).
            limit: Maximum number of episodes to return.
            min_quality: Minimum quality score threshold.

        Returns:
            List of :class:`Episode` objects ordered by recency.
        """
        conn = get_connection()
        clauses: list[str] = ["quality_score >= ?"]
        params: list[Any] = [min_quality]

        if scope and scope != "global":
            clauses.append("scope IN (?, 'global')")
            params.append(scope)
        if task_type:
            clauses.append("task_type = ?")
            params.append(task_type)
        if agent_type:
            clauses.append("agent_type = ?")
            params.append(agent_type)

        where = " AND ".join(clauses)
        params.append(limit)
        rows = conn.execute(
            f"SELECT * FROM memory_episodes WHERE {where} ORDER BY created_at DESC LIMIT ?",
            params,
        ).fetchall()
        return [self._row_to_episode(row) for row in rows]

    def get_failure_patterns(self, agent_type: str, task_type: str) -> list[str]:
        """Return output summaries from failed episodes for error pattern analysis.

        Args:
            agent_type: Agent type to filter.
            task_type: Task type to filter.

        Returns:
            List of output summary strings from failed episodes.
        """
        conn = get_connection()
        rows = conn.execute(
            """SELECT output_summary FROM memory_episodes
               WHERE agent_type = ? AND task_type = ? AND success = 0
               ORDER BY created_at DESC LIMIT 50""",
            (agent_type, task_type),
        ).fetchall()
        return [row[0] for row in rows]

    @staticmethod
    def _maybe_evict() -> None:
        """Evict oldest low-importance episodes when the table is too large."""
        conn = get_connection()
        count = conn.execute("SELECT COUNT(*) FROM memory_episodes").fetchone()[0]
        if count <= MAX_EPISODES:
            return
        # Remove the bottom 10% by importance
        evict_count = max(1, count // 10)
        conn.execute(
            """DELETE FROM memory_episodes WHERE episode_id IN (
               SELECT episode_id FROM memory_episodes
               ORDER BY importance ASC, created_at ASC LIMIT ?)""",
            (evict_count,),
        )
        conn.commit()
        logger.debug("Episode eviction: removed %d low-importance records", evict_count)

    @staticmethod
    def _row_to_episode(row: Any) -> RecordedEpisode:
        """Convert a SQLite row to an Episode.

        Args:
            row: sqlite3.Row from memory_episodes.

        Returns:
            Populated :class:`Episode`.
        """
        import json

        meta: dict[str, Any] = {}
        if row["metadata_json"]:
            try:
                meta = json.loads(row["metadata_json"])
            except JSONDecodeError as exc:
                logger.warning(
                    "Episode metadata JSON decode failed for episode %s: %s",
                    row["episode_id"],
                    exc,
                )
                account_evidence_drop(
                    logger=logger,
                    evidence_ref=f"episode:{row['episode_id']}:metadata_json",
                    reason="episode_metadata_decode_error",
                )
                meta = {}
        try:
            scope = row["scope"] or "global"
        except (IndexError, KeyError, TypeError):
            scope = "global"
        return RecordedEpisode(
            episode_id=row["episode_id"],
            timestamp=row["timestamp"],
            task_summary=row["task_summary"],
            agent_type=row["agent_type"],
            task_type=row["task_type"],
            output_summary=row["output_summary"],
            quality_score=float(row["quality_score"]),
            success=bool(row["success"]),
            model_id=row["model_id"] or "",
            importance=float(row["importance"]),
            metadata=meta,
            scope=scope,
        )
