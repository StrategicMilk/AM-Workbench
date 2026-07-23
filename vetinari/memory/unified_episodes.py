"""Episode API methods for the unified long-term memory store.

This mixin owns the public episode recording, recall, metadata, feedback, and
statistics methods while table-level SQL helpers stay in ``_store_episode``.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from typing import TYPE_CHECKING, Any

from ._store_episode import get_episode_stats, get_failure_patterns, recall_episodes_from_db, row_to_episode_dict
from ._store_episode import record_episode_full as _record_episode_full
from .episode_recorder import RecordedEpisode
from .unified_embeddings import _embed_for_store

logger = logging.getLogger(__name__)


class _EpisodeMixin:
    """Episode recording, recall, feedback, and stats methods."""

    if TYPE_CHECKING:
        _conn: Any
        _embedding_api_url: Any
        _embedding_model: Any
        _lock: Any
        _max_entries: Any

    def record_episode(
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
        """Persist an agent execution episode and store its embedding for recall.

        Args:
            task_description: Plain-text description of the task that was executed.
            agent_type: Identifier for the agent that ran the task.
            task_type: Category of work, such as ``"code_generation"`` or ``"review"``.
            output_summary: Short summary of what the agent produced.
            quality_score: Numeric quality rating in ``[0.0, 1.0]`` assigned by the inspector.
            success: Whether the episode ended in a successful outcome.
            model_id: Identifier of the model used for inference; empty string if unknown.
            metadata: Optional free-form key/value pairs stored alongside the episode record.

        Returns:
            The unique episode ID assigned to the new record.
        """
        with self._lock:
            return _record_episode_full(
                self._conn,
                task_description=task_description,
                agent_type=agent_type,
                task_type=task_type,
                output_summary=output_summary,
                quality_score=quality_score,
                success=success,
                model_id=model_id,
                metadata=metadata or {},
                max_entries=self._max_entries,
                api_url=self._embedding_api_url,
                model=self._embedding_model,
            )

    def recall_episodes(
        self,
        query: str,
        k: int = 5,
        min_score: float = 0.0,
        task_type: str | None = None,
        successful_only: bool = False,
    ) -> list[RecordedEpisode]:
        """Return the *k* most relevant past episodes for *query*.

        Args:
            query: Natural language description of the task to find similar episodes for.
            k: Maximum number of episodes to return (default 5).
            min_score: Minimum cosine-similarity score; episodes below this threshold
                are excluded (default 0.0, meaning no filtering).
            task_type: Optional task category filter applied before similarity ranking.
            successful_only: When ``True``, restrict results to episodes that succeeded.

        Returns:
            Up to *k* Episodes ranked by similarity to the query, filtered by *min_score*.
        """
        query_vec = _embed_for_store(self, f"{task_type or ''}: {query}")
        with self._lock:
            return recall_episodes_from_db(
                self._conn,
                query_vec=query_vec,
                query_text=query,
                k=k,
                min_score=min_score,
                task_type=task_type,
                successful_only=successful_only,
                row_to_episode_fn=self._row_to_episode,
                embedding_model=self._embedding_model,
            )

    def get_episode(self, episode_id: str) -> RecordedEpisode | None:
        """Return one recorded episode by ID from canonical storage.

        Returns:
            The matching episode, or None when the ID is absent.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM memory_episodes WHERE episode_id = ?",
                (episode_id,),
            ).fetchone()
        return self._row_to_episode(row) if row is not None else None

    def get_episode_metadata(self, episode_id: str) -> dict[str, Any]:
        """Return parsed metadata for one canonical episode.

        Returns:
            Episode metadata as a JSON-compatible dictionary. Missing or damaged
            metadata returns an empty dictionary.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT metadata_json FROM memory_episodes WHERE episode_id = ?",
                (episode_id,),
            ).fetchone()
        if row is None or not row["metadata_json"]:
            return {}
        try:
            metadata = json.loads(row["metadata_json"])
        except (json.JSONDecodeError, TypeError):
            logger.warning(
                "Could not parse metadata for episode %s - returning empty metadata",
                episode_id,
                exc_info=True,
            )
            return {}
        if not isinstance(metadata, dict):
            logger.warning(
                "Episode metadata for %s is not a JSON object - returning empty metadata",
                episode_id,
            )
            return {}
        return metadata

    def update_episode_metadata(self, episode_id: str, metadata: dict[str, Any]) -> bool:
        """Replace metadata for one canonical episode.

        Args:
            episode_id: Episode identifier to update.
            metadata: Replacement JSON-compatible metadata dictionary.

        Returns:
            True when an episode row was updated, False when the ID was absent.
        """
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE memory_episodes SET metadata_json = ? WHERE episode_id = ?",
                (json.dumps(metadata), episode_id),
            )
            self._conn.commit()
        return cursor.rowcount > 0

    def append_episode_feedback(self, episode_id: str, feedback: dict[str, Any]) -> bool:
        """Append candidate-only feedback metadata without promoting authority.

        Args:
            episode_id: Episode identifier that receives the feedback entry.
            feedback: JSON-compatible feedback payload from a reviewer or evaluator.

        Returns:
            True when feedback was appended, False when the episode ID was absent.
        """
        metadata = self.get_episode_metadata(episode_id)
        if self.get_episode(episode_id) is None:
            return False
        feedback_entry = dict(feedback)
        feedback_entry["candidate_only"] = True
        feedback_entry["requires_memory_firewall"] = True
        values = list(metadata.get("episode_feedback") or [])
        values.append(feedback_entry)
        metadata["episode_feedback"] = values
        metadata["candidate_status"] = "candidate"
        metadata["candidate_only"] = True
        metadata["requires_memory_firewall"] = True
        return self.update_episode_metadata(episode_id, metadata)

    def get_failure_patterns(self, agent_type: str, task_type: str) -> list[str]:
        """Return recent failure output summaries for an agent/task combination.

        Args:
            agent_type: Agent identifier to filter by.
            task_type: Task category to filter by.

        Returns:
            Output summary strings from failed episodes, most recent first.
        """
        with self._lock:
            return get_failure_patterns(self._conn, agent_type, task_type)

    def get_episode_stats(self) -> dict[str, Any]:
        """Return total, successful, and avg_quality_score for stored episodes.

        Returns:
            Dict with total, successful, failed, and avg_quality_score keys.
        """
        with self._lock:
            return get_episode_stats(self._conn)

    def _row_to_episode(self, row: sqlite3.Row) -> RecordedEpisode:
        """Convert a memory_episodes row to a RecordedEpisode."""
        return RecordedEpisode(**row_to_episode_dict(row))
