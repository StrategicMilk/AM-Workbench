"""Low-level embedding helpers and KBDocument for Vetinari's RAG knowledge base.

Extracted from ``knowledge_base.py`` to keep that module under the 550-line
file limit.  All names are re-exported from ``knowledge_base.py`` so existing
import paths remain valid.
"""

from __future__ import annotations

import logging
import math
import os
import sqlite3
import struct
from dataclasses import dataclass
from urllib.parse import urlparse

from vetinari.constants import (
    DEFAULT_EMBEDDING_API_URL,
    EMBEDDING_API_TIMEOUT,
    TRUNCATE_KNOWLEDGE_DOC,
)

logger = logging.getLogger(__name__)


_EMBEDDING_API_URL = DEFAULT_EMBEDDING_API_URL
_EMBEDDING_MODEL = os.environ.get("VETINARI_EMBEDDING_MODEL", "text-embedding-nomic-embed-text-v1.5")
_EMBEDDING_DIMENSIONS = int(os.environ.get("VETINARI_EMBEDDING_DIMENSIONS", "768"))
_MAX_DOC_CHARS = TRUNCATE_KNOWLEDGE_DOC
_LOCAL_EMBEDDING_HOSTS = {"localhost", "127.0.0.1", "::1"}


# ── Data Class ────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class KBDocument:
    """A document chunk in the knowledge base."""

    doc_id: str
    content: str
    source: str  # File path or URL
    category: str = "general"  # docs / code / pattern / error / etc.
    score: float = 0.0  # Relevance score (populated by query)

    def __repr__(self) -> str:
        """Show key identifying fields for debugging."""
        return (
            f"KBDocument(doc_id={self.doc_id!r},"
            f" source={self.source!r},"
            f" category={self.category!r}, score={self.score!r})"
        )


# ── Embedding Helpers ────────────────────────────────────────────────────


def kb_embed(text: str) -> list[float] | None:
    """Get embedding from an OpenAI-compatible /v1/embeddings endpoint.

    Returns None if the endpoint is unavailable (graceful fallback to FTS5).

    Args:
        text: Text to embed.

    Returns:
        Float list embedding vector, or None if endpoint unreachable.
    """
    if not _embedding_endpoint_allowed(_EMBEDDING_API_URL):
        logger.warning("KB embedding endpoint blocked by local-only policy: %s", _redact_endpoint(_EMBEDDING_API_URL))
        return None

    try:
        import json

        import httpx

        payload = json.dumps({"input": text, "model": _EMBEDDING_MODEL}).encode("utf-8")
        resp = httpx.post(
            f"{_EMBEDDING_API_URL}/v1/embeddings",
            content=payload,
            headers={"Content-Type": "application/json"},
            timeout=EMBEDDING_API_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        return validate_embedding_vector(data["data"][0]["embedding"], source="embedding_endpoint")
    except Exception as exc:
        logger.warning("KB embedding endpoint unavailable: %s", exc)
        return None


def _embedding_endpoint_allowed(url: str) -> bool:
    """Return True when an embedding endpoint may receive document text."""
    parsed = urlparse(url)
    hostname = (parsed.hostname or "").lower()
    if hostname in _LOCAL_EMBEDDING_HOSTS:
        return True

    allow_remote = os.environ.get("VETINARI_ALLOW_REMOTE_EMBEDDINGS", "").lower() in {"1", "true", "yes"}
    if not allow_remote:
        return False

    allowlist = {
        host.strip().lower()
        for host in os.environ.get("VETINARI_EMBEDDING_API_ALLOWLIST", "").split(",")
        if host.strip()
    }
    return hostname in allowlist


def _redact_endpoint(url: str) -> str:
    parsed = urlparse(url)
    host = parsed.hostname or "<missing-host>"
    return f"{parsed.scheme or '<missing-scheme>'}://{host}"


def pack_embedding(vec: list[float]) -> bytes:
    """Pack a float list into compact float32 bytes.

    Args:
        vec: Float list to pack.

    Returns:
        Little-endian float32 bytes.

    Raises:
        ValueError: If the input is not a list of finite numbers.
    """
    if not isinstance(vec, list):
        raise ValueError("pack_embedding embedding must be a list")
    validated: list[float] = []
    for index, value in enumerate(vec):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(f"pack_embedding embedding[{index}] must be a finite number")
        validated.append(float(value))
    return struct.pack(f"<{len(validated)}f", *validated)


def validate_embedding_vector(vec: object, *, source: str) -> list[float]:
    """Validate embedding dimensions and numeric values before storage/search.

    Returns:
        Normalized float embedding vector with the expected dimensionality.

    Raises:
        ValueError: If the embedding is not a finite numeric vector of the expected size.
    """
    if not isinstance(vec, list):
        raise ValueError(f"{source} embedding must be a list")
    if len(vec) != _EMBEDDING_DIMENSIONS:
        raise ValueError(f"{source} embedding dimension {len(vec)} != expected {_EMBEDDING_DIMENSIONS}")
    validated: list[float] = []
    for index, value in enumerate(vec):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(f"{source} embedding[{index}] must be a finite number")
        validated.append(float(value))
    return validated


def unpack_embedding(blob: bytes) -> list[float]:
    """Unpack float32 bytes back to a float list.

    Args:
        blob: Little-endian float32 bytes.

    Returns:
        Unpacked float list.
    """
    count = len(blob) // 4
    return list(struct.unpack(f"<{count}f", blob))


def load_sqlite_vec(conn: sqlite3.Connection) -> bool:
    """Load sqlite-vec extension into a connection.

    Args:
        conn: SQLite connection to load the extension into.

    Returns:
        True if loaded successfully, False otherwise.
    """
    try:
        import sqlite_vec

        sqlite_vec.load(conn)
        return True
    except Exception as exc:
        logger.warning("sqlite-vec load failed for KB: %s", exc)
        return False
