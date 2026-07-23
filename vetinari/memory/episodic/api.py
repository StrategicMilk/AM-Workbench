"""Canonical API over the unified episodic memory store."""

from __future__ import annotations

from typing import Any

from vetinari.memory.unified import UnifiedMemoryStore, get_unified_memory_store

from .schema import CanonicalEpisode, EpisodeFeedback


class UnifiedEpisodicMemory:
    """Thin adapter over ``UnifiedMemoryStore`` episodic storage."""

    def __init__(self, store: UnifiedMemoryStore | None = None) -> None:
        self._store = store if store is not None else get_unified_memory_store()

    def record(self, episode: CanonicalEpisode | None = None, **kwargs: Any) -> str:
        """Record a canonical episode in ``memory_episodes`` and return its id.

        Returns:
            str value produced by record().
        """
        canonical = episode if episode is not None else CanonicalEpisode(**kwargs)
        return self._store.record_episode(
            task_description=canonical.task_summary,
            agent_type=canonical.agent_type,
            task_type=canonical.task_type,
            output_summary=canonical.output_summary,
            quality_score=canonical.quality_score,
            success=canonical.success,
            model_id=canonical.model_id,
            metadata=canonical.metadata_for_storage(),
        )

    def recall(
        self,
        query: str,
        k: int = 5,
        min_score: float = 0.0,
        task_type: str | None = None,
        successful_only: bool = False,
    ) -> list[CanonicalEpisode]:
        """Recall canonical episodes from the existing unified store."""
        return [
            CanonicalEpisode.from_recorded(episode)
            for episode in self._store.recall_episodes(
                query=query,
                k=k,
                min_score=min_score,
                task_type=task_type,
                successful_only=successful_only,
            )
        ]

    def get(self, episode_id: str) -> CanonicalEpisode | None:
        """Return one canonical episode by id, if present.

        Returns:
            CanonicalEpisode | None value produced by get().
        """
        episode = self._store.get_episode(episode_id)
        return CanonicalEpisode.from_recorded(episode) if episode is not None else None

    def add_feedback(self, episode_id: str, feedback: EpisodeFeedback) -> bool:
        """Attach candidate-only feedback metadata to an existing episode."""
        return self._store.append_episode_feedback(episode_id, feedback.to_dict())

    def get_failure_patterns(self, agent_type: str, task_type: str) -> list[str]:
        """Return recent failure summaries for a legacy-compatible caller."""
        return self._store.get_failure_patterns(agent_type, task_type)

    def get_stats(self) -> dict[str, Any]:
        """Return aggregate episode stats from canonical storage."""
        return self._store.get_episode_stats()
