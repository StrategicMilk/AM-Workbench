"""Embedding helpers for the memory subsystem.

Routes dense-vector generation through the supervised AM Engine client, and
provides binary pack/unpack utilities for storing those vectors in SQLite
BLOB columns.

sqlite-vec extension management is handled by the caller (``unified.py``)
which owns the connection lifecycle and optional KNN virtual table setup.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from importlib import import_module
from importlib.util import find_spec

from vetinari.memory.memory_storage import (
    _pack_embedding as pack_embedding,
)
from vetinari.memory.memory_storage import (
    _unpack_embedding as unpack_embedding,
)

logger = logging.getLogger(__name__)

# Public embedding API re-exported from this module. `unpack_embedding` has no
# internal caller here (unlike `pack_embedding`), so it must be named in
# __all__ or lint autoflake strips the re-export and breaks unified.py,
# _store_search.py, and _store_episode.py, which import it from here.
__all__ = [
    "embed_all_missing",
    "embed_via_local_inference",
    "load_sqlite_vec",
    "pack_embedding",
    "sqlite_vec_available",
    "unpack_embedding",
]


# ---------------------------------------------------------------------------
# Constants (mirrors unified.py module-level env reads)
# ---------------------------------------------------------------------------

EMBEDDING_API_URL = ""  # Compatibility argument only; AM Engine owns endpoint discovery.
EMBEDDING_MODEL = os.environ.get("VETINARI_EMBEDDING_MODEL", "text-embedding-nomic-embed-text-v1.5")
# Expected output dimension from the default embedding model.
EMBED_DIM = 768  # nomic-embed-text-v1.5 produces 768-dim vectors


# ---------------------------------------------------------------------------
# Local inference endpoint
# ---------------------------------------------------------------------------


def embed_via_local_inference(
    text: str,
    api_url: str = EMBEDDING_API_URL,
    model: str = EMBEDDING_MODEL,
) -> list[float] | None:
    """Get an embedding through the supervised AM Engine typed client.

    Configured via ``VETINARI_EMBEDDING_API_URL`` and
    ``VETINARI_EMBEDDING_MODEL`` environment variables.  Returns ``None``
    when the endpoint is unreachable so callers can fall back gracefully
    to FTS5 text search.

    Args:
        text: Text to embed.
        api_url: Retained compatibility argument; endpoint discovery is supervisor-owned.
        model: Embedding model identifier (default from env).

    Returns:
        Float list embedding vector, or None if the endpoint is unreachable.
    """
    if not text.strip():
        # Empty text produces an all-zeros vector from most endpoints, which
        # makes cosine_similarity return 0.0 for every comparison - undefined
        # similarity. Return a unit vector so comparisons are at least defined.
        return [1.0] + [0.0] * (EMBED_DIM - 1)

    try:
        from vetinari.engine.client import EmbeddingsRequest, get_engine_client

        response = get_engine_client().embeddings(EmbeddingsRequest((text,), model_id=model))
        return [float(value) for value in response.vectors[0]]
    except Exception as exc:
        logger.warning(
            "AM Engine embedding unavailable; memory search will use text fallback",
            extra={"fallback_type": "fts5", "exc_class": type(exc).__name__},
        )
        return None


def embed_batch_via_local_inference(
    texts: list[str],
    api_url: str = EMBEDDING_API_URL,
    model: str = EMBEDDING_MODEL,
) -> list[list[float] | None]:
    """Get embeddings for multiple texts using one OpenAI-compatible request.

    Args:
        texts: Texts value consumed by embed_batch_via_local_inference().
        api_url: Api url value consumed by embed_batch_via_local_inference().
        model: Model value consumed by embed_batch_via_local_inference().

    Returns:
        Value produced for the caller.
    """
    if not texts:
        return []

    results: list[list[float] | None] = [None] * len(texts)
    non_empty: list[tuple[int, str]] = []
    for index, text in enumerate(texts):
        if not text.strip():
            results[index] = [1.0] + [0.0] * (EMBED_DIM - 1)
        else:
            non_empty.append((index, text))
    if not non_empty:
        return results

    try:
        from vetinari.engine.client import EmbeddingsRequest, get_engine_client

        response = get_engine_client().embeddings(
            EmbeddingsRequest(tuple(text for _, text in non_empty), model_id=model),
        )
        for (index, _), vector in zip(non_empty, response.vectors, strict=True):
            results[index] = [float(value) for value in vector]
    except Exception as exc:
        logger.warning(
            "AM Engine batch embedding unavailable; memory rows remain text-searchable",
            extra={"fallback_type": "fts5", "exc_class": type(exc).__name__},
        )
    return results


# ---------------------------------------------------------------------------
# sqlite-vec extension helpers
# ---------------------------------------------------------------------------


def sqlite_vec_available() -> bool:
    """Return True if the sqlite-vec Python package can be imported.

    Only checks importability -- does not attempt to load the extension into
    a connection.  Use :func:`load_sqlite_vec` to actually enable KNN search.

    Returns:
        True when ``sqlite_vec`` is importable.
    """
    if find_spec("sqlite_vec") is None:
        logger.warning("sqlite_vec not installed - KNN vector search unavailable")
        return False
    return True


def load_sqlite_vec(conn: sqlite3.Connection | None) -> bool:
    """Load the sqlite-vec extension into *conn*, enabling KNN vector search.

    Must be called before creating or querying the ``memory_vec`` virtual
    table.  Silently returns False when the extension is unavailable or when
    *conn* is None so callers can treat it as optional.

    Args:
        conn: Active SQLite connection to load the extension into, or None.

    Returns:
        True if the extension was loaded successfully, False otherwise.
    """
    if conn is None:
        return False
    if not sqlite_vec_available():
        return False
    sqlite_vec = import_module("sqlite_vec")
    try:
        conn.load_extension(sqlite_vec.loadable_path())
        return True
    except (AttributeError, sqlite3.OperationalError) as exc:
        logger.warning("sqlite-vec extension not available: %s - falling back to linear scan for memory search", exc)
        return False


def embed_all_missing(
    conn: sqlite3.Connection,
    api_url: str,
    model: str,
    has_vec: bool,
) -> int:
    """Generate embeddings for all memory entries that currently lack them.

    Skips forgotten entries.  Stores results in the ``embeddings`` table and,
    when ``has_vec`` is True, also in the sqlite-vec ``memory_vec`` table.

    Args:
        conn: Active SQLite connection.
        api_url: Embedding endpoint base URL.
        model: Embedding model identifier.
        has_vec: Whether the sqlite-vec extension is loaded.

    Returns:
        Number of embeddings successfully generated and stored.
    """
    from datetime import datetime, timezone

    rows = conn.execute(
        """SELECT m.id, m.content FROM memories m
           LEFT JOIN embeddings e ON m.id = e.memory_id
           WHERE e.memory_id IS NULL AND m.forgotten = 0""",
    ).fetchall()

    vectors = embed_batch_via_local_inference([row["content"] for row in rows], api_url, model)
    timestamp = datetime.now(timezone.utc).isoformat()
    embedding_rows: list[tuple[str, bytes, str, int, str]] = []
    vec_rows: list[tuple[str, bytes]] = []
    for row, vec in zip(rows, vectors, strict=True):
        if vec is None:
            continue
        blob = pack_embedding(vec)
        embedding_rows.append((row["id"], blob, model, len(vec), timestamp))
        if has_vec:
            vec_rows.append((row["id"], blob))

    count = 0
    if embedding_rows:
        try:
            with conn:
                conn.executemany(
                    "INSERT OR REPLACE INTO embeddings "
                    "(memory_id, embedding_blob, model, dimensions, created_at) VALUES (?, ?, ?, ?, ?)",
                    embedding_rows,
                )
                if vec_rows:
                    conn.executemany(
                        "INSERT OR REPLACE INTO memory_vec (memory_id, embedding) VALUES (?, ?)",
                        vec_rows,
                    )
            count = len(embedding_rows)
        except sqlite3.Error as exc:
            logger.warning("Embedding batch store failed: %s", exc)

    logger.info("Generated %d embeddings for %d memories", count, len(rows))
    return count
