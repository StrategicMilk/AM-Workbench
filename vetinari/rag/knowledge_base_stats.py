"""Statistics helper for the RAG knowledge base."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def build_knowledge_base_stats(kb: Any) -> dict[str, Any]:
    """Build the public KnowledgeBase statistics payload.

    Args:
        kb: KnowledgeBase-like object exposing connection and counters.

    Returns:
        Dict with document count, backend, database path, and embedding counters.
    """
    count = 0
    with kb._lock:
        try:
            cursor = kb._conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM documents")
            count = cursor.fetchone()[0]
        except Exception:
            logger.warning("Failed to get KB document count", exc_info=True)
    fallback_rate = round(kb._embedding_fallbacks / kb._embedding_attempts, 3) if kb._embedding_attempts > 0 else 0.0
    return {
        "document_count": count,
        "backend": "sqlite_vec" if kb._has_vec else "sqlite_fts5",
        "db_path": kb._db_path if kb._db_path is not None else "unified",
        "embedding_attempts": kb._embedding_attempts,
        "embedding_fallbacks": kb._embedding_fallbacks,
        "embedding_fallback_rate": fallback_rate,
    }


__all__ = ["build_knowledge_base_stats"]
