"""Embedding helpers for the unified long-term memory store.

This module owns semantic-search plumbing and embedding persistence while the
public compatibility symbols remain available from ``vetinari.memory.unified``.
"""

from __future__ import annotations

import logging
import sqlite3
import sys
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from ._store_search import fts_search, is_semantic_duplicate, like_search, manual_cosine_search, vec_knn_search
from .interfaces import MemoryEntry
from .memory_embeddings import embed_all_missing
from .memory_embeddings import embed_via_local_inference as _embed_via_local_inference
from .memory_embeddings import pack_embedding as _pack_embedding
from .unified_config import EMBEDDING_DIMENSIONS

logger = logging.getLogger(__name__)

_UNIFIED_MODULE_NAME = "vetinari.memory.unified"  # Public patch target retained for compatibility.
_DEFAULT_EMBEDDER = _embed_via_local_inference  # Original embedder, used to detect local test patches.


def _embed_for_store(store: Any, text: str) -> list[float] | None:
    """Embed text using store configuration and the public compatibility patch point."""
    embedder = _embed_via_local_inference
    if embedder is _DEFAULT_EMBEDDER:
        unified_module = sys.modules.get(_UNIFIED_MODULE_NAME)
        public_embedder = getattr(unified_module, "_embed_via_local_inference", None)
        if callable(public_embedder):
            embedder = public_embedder
    return embedder(text, store._embedding_api_url, store._embedding_model)


class _EmbeddingMixin:
    """Semantic search and embedding persistence methods for UnifiedMemoryStore."""

    if TYPE_CHECKING:
        _conn: Any
        _dedup_threshold: Any
        _embedding_api_url: Any
        _embedding_model: Any
        _has_vec: Any
        _lock: Any

    def embeddings_available(self) -> bool:
        """Return True if the embedding endpoint responds to a probe request."""
        return _embed_for_store(self, "ping") is not None

    def _store_embedding_for(self, memory_id: str, text: str) -> None:
        """Store an embedding for one memory row when the endpoint is available."""
        vec = _embed_for_store(self, text)
        if vec is None:
            return
        blob = _pack_embedding(vec)
        try:
            self._conn.execute(
                "INSERT OR REPLACE INTO embeddings "
                "(memory_id, embedding_blob, model, dimensions, created_at) VALUES (?, ?, ?, ?, ?)",
                (memory_id, blob, self._embedding_model, len(vec), datetime.now(timezone.utc).isoformat()),
            )
            if self._has_vec:
                self._conn.execute(
                    "INSERT OR REPLACE INTO memory_vec (memory_id, embedding) VALUES (?, ?)",
                    (memory_id, blob),
                )
            self._conn.commit()
        except sqlite3.Error as exc:
            logger.warning("Failed to store embedding for %s - semantic search will degrade", memory_id)
            logger.debug("Embedding store error detail: %s", exc)

    def embed_all(self) -> int:
        """Generate embeddings for every memory that currently lacks one.

        Returns:
            Count of embeddings newly generated during this call.
        """
        with self._lock:
            return embed_all_missing(
                self._conn,
                api_url=self._embedding_api_url,
                model=self._embedding_model,
                has_vec=self._has_vec,
            )

    def _semantic_search(
        self, query: str, agent: str | None, entry_types: list[str] | None, limit: int
    ) -> list[MemoryEntry]:
        """Embedding similarity search, falling back to FTS5 when unavailable."""
        query_vec = _embed_for_store(self, query)
        if query_vec is None:
            with self._lock:
                return fts_search(self._conn, query, agent, entry_types, limit, like_fallback=like_search)

        def _fts(
            conn: sqlite3.Connection,
            query_text: str,
            agent_filter: str | None,
            type_filter: list[str] | None,
            result_limit: int,
        ) -> list[MemoryEntry]:
            return fts_search(conn, query_text, agent_filter, type_filter, result_limit, like_fallback=like_search)

        def _cosine(
            conn: sqlite3.Connection,
            query_vector: list[float],
            agent_filter: str | None,
            type_filter: list[str] | None,
            result_limit: int,
        ) -> list[MemoryEntry]:
            return manual_cosine_search(
                conn,
                query_vector,
                agent_filter,
                type_filter,
                result_limit,
                fallback_query=query,
                embedding_model=self._embedding_model,
                embedding_dimensions=EMBEDDING_DIMENSIONS,
                fts_fallback=_fts,
            )

        with self._lock:
            if self._has_vec:
                return vec_knn_search(
                    self._conn,
                    query_vec,
                    agent,
                    entry_types,
                    limit,
                    fallback_query=query,
                    embedding_model=self._embedding_model,
                    embedding_dimensions=EMBEDDING_DIMENSIONS,
                    fts_fallback=_fts,
                    manual_fallback=_cosine,
                )
            return _cosine(self._conn, query_vec, agent, entry_types, limit)

    def _check_semantic_duplicate(self, content: str) -> bool:
        """Return True when *content* cosine-similarity exceeds the dedup threshold."""
        query_vec = _embed_for_store(self, content)
        if query_vec is None:
            return False
        with self._lock:
            return is_semantic_duplicate(self._conn, query_vec, self._dedup_threshold)
