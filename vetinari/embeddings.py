"""Unified embedding singleton for Vetinari.

Provides a single ``get_embedder()`` compatibility facade backed by the
supervised AM Engine ``/v1/embeddings`` contract. When the engine is not
available, the facade uses a deterministic n-gram vector and emits a labeled,
counted fallback signal.
"""

from __future__ import annotations

import hashlib
import logging
import math
import os
import struct
import threading

logger = logging.getLogger(__name__)


# Dimensionality contract — callers depend on this being 384.
EMBEDDING_DIMENSIONS = 384
_MODEL_NAME = "all-MiniLM-L6-v2"
_ENABLE_NATIVE_EMBEDDINGS_ENV = "VETINARI_ENABLE_SENTENCE_TRANSFORMERS_EMBEDDINGS"
_ENGINE_PROBE_TEXT = "vetinari unified embedding probe"
engine_embedding_fallbacks_total = 0


def _native_embeddings_enabled() -> bool:
    """Return True when in-process sentence-transformers embeddings are enabled."""
    return os.getenv(_ENABLE_NATIVE_EMBEDDINGS_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


# ---------------------------------------------------------------------------
# N-gram fallback embedder
# ---------------------------------------------------------------------------


def _ngram_hash_embed(text: str, dims: int = EMBEDDING_DIMENSIONS) -> list[float]:
    """Produce a deterministic unit vector via character n-gram hashing.

    Uses SHA-256 seeded with overlapping 4-grams to fill a float vector,
    then L2-normalises.  Always returns a ``dims``-dimensional vector.

    Args:
        text: Input string to embed.
        dims: Number of output dimensions (must match EMBEDDING_DIMENSIONS).

    Returns:
        A normalised list of ``dims`` floats.
    """
    vec = [0.0] * dims
    if not text:
        return vec
    # 4-character n-grams; fallback to bigrams for very short text
    n = 4 if len(text) >= 4 else 2
    ngrams = [text[i : i + n] for i in range(len(text) - n + 1)] or [text]
    for gram_index, gram in enumerate(ngrams):
        digest = hashlib.sha256(gram.encode("utf-8", errors="replace")).digest()
        # Unpack 8 floats per digest (32 bytes / 4 bytes per float = 8)
        floats = struct.unpack("8f", digest[:32])
        # Distribute across dims using modulo
        for j, v in enumerate(floats):
            vec[(j * len(ngrams) + gram_index) % dims] += v
    # L2 normalise
    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / norm for x in vec]


# ---------------------------------------------------------------------------
# AM Engine embedder (legacy class name retained for API compatibility)
# ---------------------------------------------------------------------------


class SentenceTransformerEmbedder:
    """Compatibility facade for supervised AM Engine embeddings."""

    def __init__(self, model_name: str = _MODEL_NAME) -> None:
        self._model_name = model_name
        self._available = False
        self._lock = threading.Lock()

    def _try_load(self) -> None:
        """Probe the AM Engine without permanently caching a failed result."""
        with self._lock:
            try:
                self._available = bool(self._embed_engine((_ENGINE_PROBE_TEXT,))[0])
            except Exception as exc:
                self._available = False
                logger.warning(
                    "AM Engine embedding probe unavailable",
                    extra={"fallback_type": "ngram_hash", "exc_class": type(exc).__name__},
                )

    def _embed_engine(self, texts: tuple[str, ...]) -> list[list[float]]:
        from vetinari.engine.client import EmbeddingsRequest, get_engine_client

        response = get_engine_client().embeddings(EmbeddingsRequest(texts, model_id=self._model_name))
        return [[float(value) for value in vector] for vector in response.vectors]

    @staticmethod
    def _record_fallback(exc: Exception) -> None:
        global engine_embedding_fallbacks_total
        engine_embedding_fallbacks_total += 1
        logger.warning(
            "AM Engine embedding unavailable; using deterministic n-gram fallback",
            extra={"fallback_type": "ngram_hash", "exc_class": type(exc).__name__},
        )

    @property
    def available(self) -> bool:
        """Return True when the supervised AM Engine embedding path responds."""
        self._try_load()
        return self._available

    def embed(self, text: str) -> list[float]:
        """Embed a single string into a 384-dimensional unit vector.

        Args:
            text: Input text to embed.

        Returns:
            384-dimensional L2-normalised float list.
        """
        if not text:
            return [0.0] * EMBEDDING_DIMENSIONS
        try:
            vector = self._embed_engine((text,))[0]
            self._available = True
            return vector
        except Exception as exc:
            self._available = False
            self._record_fallback(exc)
        return _ngram_hash_embed(text, EMBEDDING_DIMENSIONS)

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of strings efficiently.

        Args:
            texts: List of input strings to embed.

        Returns:
            List of 384-dimensional unit vectors, one per input string.
        """
        if not texts:
            return []
        try:
            vectors = self._embed_engine(tuple(texts))
            self._available = True
            return vectors
        except Exception as exc:
            self._available = False
            self._record_fallback(exc)
        return [_ngram_hash_embed(t, EMBEDDING_DIMENSIONS) for t in texts]


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_embedder: SentenceTransformerEmbedder | None = None
_embedder_lock = threading.Lock()


def get_embedder() -> SentenceTransformerEmbedder:
    """Return the process-wide embedding singleton (thread-safe).

    Uses double-checked locking so the facade is allocated at most once.

    Returns:
        The shared :class:`SentenceTransformerEmbedder` instance.
    """
    global _embedder
    if _embedder is None:
        with _embedder_lock:
            if _embedder is None:
                _embedder = SentenceTransformerEmbedder()
    return _embedder
