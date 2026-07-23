"""Search and query-routing methods for the unified memory store.

This mixin keeps natural-language intent dispatch, timeline lookup, and
long-term memory search separate from the store connection lifecycle.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from vetinari.types import MemoryType

from ._store_ops import get_superseded_ids
from ._store_search import build_timeline, fts_search, like_search
from .episode_recorder import RecordedEpisode
from .intent_parser import IntentParser, QueryIntent
from .interfaces import MemoryEntry


class _SearchMixin:
    """Timeline, FTS, semantic, and intent-routed search methods."""

    if TYPE_CHECKING:
        _conn: Any
        _lock: Any
        _semantic_search: Any
        _touch_accessed: Any
        recall_episodes: Any

    def search(
        self,
        query: str,
        agent: str | None = None,
        entry_types: list[str] | None = None,
        limit: int = 10,
        use_semantic: bool = False,
        include_superseded: bool = False,
    ) -> list[MemoryEntry]:
        """FTS5 or embedding similarity search over long-term memories.

        By default, entries that have been superseded by a newer entry in
        the fact-graph chain are excluded from results.

        Args:
            query: Free-text or semantic search query.
            agent: Optional agent-name filter.
            entry_types: Optional entry-type filter list.
            limit: Maximum results.
            use_semantic: Use embedding similarity instead of FTS5.
            include_superseded: When True, include entries superseded by
                newer entries in the fact-graph chain.

        Returns:
            Up to *limit* MemoryEntries ranked by relevance to *query*.
        """
        fetch_limit = limit if include_superseded else limit * 2
        if use_semantic:
            results = self._semantic_search(query, agent, entry_types, fetch_limit)
        else:
            with self._lock:
                results = fts_search(self._conn, query, agent, entry_types, fetch_limit, like_fallback=like_search)
        if not include_superseded:
            with self._lock:
                superseded = get_superseded_ids(self._conn)
            results = [entry for entry in results if entry.id not in superseded]
        final = results[:limit]
        self._touch_accessed([entry.id for entry in final if entry.id])
        return final

    def timeline(
        self, agent: str | None = None, start_time: int | None = None, end_time: int | None = None, limit: int = 100
    ) -> list[MemoryEntry]:
        """Return memories in reverse chronological order with optional filters.

        Args:
            agent: Restrict results to entries recorded by this agent name. Pass
                ``None`` to include all agents.
            start_time: Inclusive lower bound as a Unix millisecond timestamp.
                Pass ``None`` for no lower bound.
            end_time: Inclusive upper bound as a Unix millisecond timestamp.
                Pass ``None`` for no upper bound.
            limit: Maximum number of entries to return (default 100).

        Returns:
            Up to *limit* MemoryEntries ordered newest-first within the requested time window.
        """
        with self._lock:
            return build_timeline(self._conn, agent, start_time, end_time, limit)

    def query(self, question: str, agent: str | None = None) -> list[MemoryEntry]:
        """Dispatch a natural language question to the best retrieval backend.

        Uses :class:`IntentParser` to classify the question, then routes to
        ``recall_episodes`` (episode queries), ``timeline`` (time-range queries),
        or ``search`` (knowledge-base and general semantic queries).

        Args:
            question: Natural language question from an agent or user.
            agent: Optional agent-name filter passed through to timeline/search.

        Returns:
            Matching MemoryEntries (or Episodes converted to MemoryEntries).
        """
        parsed = IntentParser().parse(question)

        if parsed.intent == QueryIntent.EPISODE_RECALL:
            episodes = self.recall_episodes(
                query=question,
                task_type=parsed.task_type,
                successful_only=bool(parsed.success_filter),
            )
            return [self._episode_to_entry(episode) for episode in episodes]

        if parsed.intent == QueryIntent.TIMELINE:
            start, end = parsed.time_range
            return self.timeline(agent=agent, start_time=start, end_time=end)

        if parsed.intent == QueryIntent.KNOWLEDGE_BASE:
            return self.search(
                question,
                entry_types=["discovery", "rule", "pattern"],
                use_semantic=True,
            )

        return self.search(question, use_semantic=True)

    @staticmethod
    def _episode_to_entry(episode: RecordedEpisode) -> MemoryEntry:
        """Convert an Episode record to a MemoryEntry for uniform return types."""
        return MemoryEntry(
            id=episode.episode_id,
            agent=episode.agent_type,
            entry_type=MemoryType.SUCCESS if episode.success else MemoryType.PROBLEM,
            content=episode.task_summary,
            summary=episode.output_summary,
            metadata={"task_type": episode.task_type},
        )
