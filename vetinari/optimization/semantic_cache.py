"""Semantic caching for LLM responses (P10.5).

Stores query/response pairs and retrieves cached responses when a new query
is semantically similar to a previously cached one.  Uses a 3-tier MinCache
pattern:

  Tier 1 — exact match via SHA-256 hash (O(1))
  Tier 2 — approximate match via MinHash LSH (O(1), requires datasketch)
  Tier 3 — AM Engine embedding cosine similarity with a trigram Jaccard
           fallback when the supervised engine is unavailable (O(n))
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

from vetinari.constants import CACHE_MAX_ENTRIES_SEMANTIC
from vetinari.security.redaction import redact_text
from vetinari.utils.lazy_import import lazy_import

from .semantic_cache_lookup import _SemanticCacheLookupMixin

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Optional datasketch import
# ---------------------------------------------------------------------------

_datasketch, _DATASKETCH_AVAILABLE = lazy_import("datasketch")
_datasketch_module: Any = _datasketch
MinHash: Any = _datasketch_module.MinHash if _datasketch_module is not None else None
MinHashLSH: Any = _datasketch_module.MinHashLSH if _datasketch_module is not None else None

# ---------------------------------------------------------------------------
# Supervised AM Engine embedding availability (probed lazily)
# ---------------------------------------------------------------------------

_EMBEDDER_REPROBE_INTERVAL_SECONDS: float = 30.0
_EMBEDDER_PROBE_TEXT = "vetinari embedding availability probe"
_last_embedder_probe_ts: float | None = None
_embedder_available = False
_embedder_state_lock = threading.Lock()
engine_embedding_failures_total = 0


def __getattr__(name: str) -> Any:
    """Expose the retired availability latch only to compatibility patchers."""
    if name == "_EMBEDDER_AVAILABLE":
        with _embedder_state_lock:
            return _embedder_available
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _check_embedder_available() -> bool:
    """Probe whether the supervised AM Engine can produce embeddings.

    The fixed probe never contains user data. Every attempt updates the
    monotonic timestamp so an unavailable engine is retried after a bounded
    cooldown instead of being latched off for the process lifetime.

    Returns:
        ``True`` when the embedder is importable and ready, ``False`` otherwise.
    """
    global _last_embedder_probe_ts
    _last_embedder_probe_ts = time.monotonic()
    try:
        from vetinari.engine.client import EmbeddingsRequest, get_engine_client

        response = get_engine_client().embeddings(EmbeddingsRequest((_EMBEDDER_PROBE_TEXT,)))
        return bool(response.vectors and response.vectors[0])
    except Exception as exc:
        logger.warning(
            "AM Engine embedding probe unavailable; semantic cache will use trigram similarity",
            extra={"fallback_type": "trigram", "exc_class": type(exc).__name__},
        )
        return False


def _get_embedder_available() -> bool:
    """Return engine availability, re-probing after a failed-probe cooldown."""
    # Compatibility for older tests and extensions that temporarily patch the
    # retired latch name. The production module never defines or persists it.
    compatibility_override = globals().get("_EMBEDDER_AVAILABLE")
    if isinstance(compatibility_override, bool):
        return compatibility_override

    global _embedder_available
    now = time.monotonic()
    with _embedder_state_lock:
        should_probe = _last_embedder_probe_ts is None or (
            not _embedder_available and now - _last_embedder_probe_ts >= _EMBEDDER_REPROBE_INTERVAL_SECONDS
        )
        if should_probe:
            _embedder_available = _check_embedder_available()
        return _embedder_available


def _compute_embedding(text: str) -> list[float] | None:
    """Compute a dense embedding through the supervised AM Engine client.

    Args:
        text: Input string to embed.

    Returns:
        A list of floats representing the embedding.
    """
    global engine_embedding_failures_total
    try:
        from vetinari.engine.client import EmbeddingsRequest, get_engine_client

        response = get_engine_client().embeddings(EmbeddingsRequest((text,)))
        return [float(value) for value in response.vectors[0]]
    except Exception as exc:
        with _embedder_state_lock:
            engine_embedding_failures_total += 1
        logger.warning(
            "AM Engine embedding failed for semantic cache; using trigram similarity",
            extra={"fallback_type": "trigram", "exc_class": type(exc).__name__},
        )
        return None


_DEFAULT_TTL_SECONDS: int = 86400  # 24 hours
_DEFAULT_SIMILARITY_THRESHOLD: float = 0.85

# "semantic" is the backend label used in TelemetryCollector.memory_metrics for
# cache dedup accounting — distinct from 'oc' / 'mnemosyne' memory backends.
_TELEMETRY_BACKEND: str = "semantic"


def _cache_context_ref(text: str) -> str:
    """Return a stable non-reversible cache reference for secret-bearing text."""
    if not text:
        return ""
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return f"sha256:{digest}:len:{len(text)}"


def _report_cache_hit() -> None:
    """Increment the dedup-hit counter in TelemetryCollector for the semantic cache."""
    try:
        from vetinari.telemetry import get_telemetry_collector

        get_telemetry_collector().record_dedup_hit(_TELEMETRY_BACKEND)
    except Exception:
        # Telemetry is best-effort — never let a metrics failure break a cache lookup.
        logger.warning("SemanticCache could not report dedup hit to telemetry — hit counter may be underreported")


def _report_cache_miss() -> None:
    """Increment the dedup-miss counter in TelemetryCollector for the semantic cache."""
    try:
        from vetinari.telemetry import get_telemetry_collector

        get_telemetry_collector().record_dedup_miss(_TELEMETRY_BACKEND)
    except Exception:
        logger.warning("SemanticCache could not report dedup miss to telemetry — miss counter may be underreported")


# Task-aware similarity thresholds — creative/code tasks need stricter matching
# because small prompt differences yield very different outputs, while error
# handling tasks can reuse cached responses more aggressively.
TASK_TYPE_THRESHOLDS: dict[str, float] = {
    "coding": 0.95,
    "code": 0.95,
    "creative": 0.95,
    "creative_writing": 0.95,
    "docs": 0.85,
    "documentation": 0.85,
    "research": 0.85,
    "error": 0.75,
    "error_recovery": 0.75,
    "security": 0.90,
    "data": 0.85,
    "general": 0.85,
}


def get_threshold_for_task_type(task_type: str) -> float:
    """Return the similarity threshold for a given task type.

    Creative tasks need stricter thresholds (0.95) to avoid stale cache hits.
    Error recovery can be lenient (0.75) since similar errors need similar fixes.

    Args:
        task_type: The task type string (e.g. "coding", "docs", "error").

    Returns:
        Similarity threshold between 0.0 and 1.0.
    """
    return TASK_TYPE_THRESHOLDS.get(task_type, _DEFAULT_SIMILARITY_THRESHOLD)


_MINHASH_NUM_PERM: int = 128
_MINHASH_THRESHOLD: float = 0.5

_instance: SemanticCache | None = None
_instance_lock: threading.Lock = threading.Lock()


@dataclass
class CacheEntry:
    r"""A single entry stored in the :class:`SemanticCache`.

    Attributes:
        query_hash: SHA-256 hex digest of the composite key
            ``query + "\\x00" + model_id + "\\x00" + system_prompt``.
        query_text: Original query string.
        response: Cached LLM response string.
        embedding: Pre-computed trigram set for similarity lookups.
        timestamp: Monotonic insertion time.
        model_id: The model this response was generated by.  Empty string
            means "any model" (backwards-compatible default).
        system_prompt: The system prompt in use when the response was cached.
            Empty string means "any system prompt".
        hit_count: Number of times this entry has been retrieved.
        dense_embedding: Sentence-transformer embedding vector, or ``None``
            when sentence-transformers is not available.
    """

    query_hash: str
    query_text: str
    response: str
    embedding: frozenset[str]
    timestamp: float
    model_id: str = ""  # model that produced this cached response
    system_prompt: str = ""  # system prompt active when response was cached
    hit_count: int = 0
    dense_embedding: list[float] | None = None  # sentence-transformer embedding

    def __repr__(self) -> str:
        """Show key identifying fields for debugging."""
        return f"CacheEntry(query_hash={self.query_hash!r}, model_id={self.model_id!r}, hit_count={self.hit_count!r})"


class SemanticCache(_SemanticCacheLookupMixin):
    """Thread-safe semantic cache for LLM query/response pairs.

    Uses a 3-tier lookup strategy:

    1. **Exact** — O(1) SHA-256 hash match.
    2. **MinHash LSH** — O(1) approximate-nearest-neighbour when
       ``datasketch`` is installed.
    3. **Trigram Jaccard scan** — O(n) linear scan, always available.

    Args:
        ttl: Time-to-live in seconds.  Default 86400 (24 hours).
        max_entries: Maximum entries before LRU eviction.  Default 500.
        similarity_threshold: Default minimum Jaccard score for a cache hit.
    """

    def __init__(
        self,
        ttl: int = _DEFAULT_TTL_SECONDS,
        max_entries: int = CACHE_MAX_ENTRIES_SEMANTIC,
        similarity_threshold: float = _DEFAULT_SIMILARITY_THRESHOLD,
    ) -> None:
        self._ttl = ttl
        self._max_entries = max_entries
        self._default_threshold = similarity_threshold
        self._lock = threading.Lock()
        # Ordered by insertion / access time for LRU eviction
        self._store: OrderedDict[str, CacheEntry] = OrderedDict()

        # Tier 1: exact hash index — SHA-256 hex → cache key (same value, kept separate for clarity)
        self._exact_index: dict[str, str] = {}

        # Tier 2: MinHash LSH index (optional — None when datasketch unavailable)
        self._minhash_index: Any | None = None
        if _DATASKETCH_AVAILABLE:
            self._minhash_index = MinHashLSH(threshold=_MINHASH_THRESHOLD, num_perm=_MINHASH_NUM_PERM)

        # Per-tier hit counters
        self._exact_hits: int = 0
        self._minhash_hits: int = 0
        self._semantic_hits: int = 0
        self._trigram_hits: int = 0

        self._hits: int = 0
        self._misses: int = 0
        self._tokens_saved: int = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def put(
        self,
        query: str,
        response: str,
        model_id: str = "",
        system_prompt: str = "",
    ) -> None:
        r"""Store a query/response pair in the cache.

        Inserts into all three tier indices.  If an identical composite key is
        already present the entry is refreshed with the new response and
        moved to the most-recently-used position.

        The composite cache key is ``SHA-256(query + "\\x00" + model_id + "\\x00"
        + system_prompt)`` so entries for different models or system prompts
        are always stored separately and never collide.

        Args:
            query: The query text.
            response: The LLM response to cache.
            model_id: The model that generated ``response``.  Used to
                      isolate cache entries by model — a lookup with a
                      different ``model_id`` will never hit this entry.
            system_prompt: The system prompt active when ``response`` was
                           generated.  Same isolation contract as ``model_id``.
        """
        system_prompt_ref = _cache_context_ref(system_prompt)
        key_material = f"{query}\x00{model_id}\x00{system_prompt_ref}"
        composite_hash = hashlib.sha256(key_material.encode("utf-8")).hexdigest()
        embedding = _trigrams(query)

        dense_emb = _compute_embedding(query) if _get_embedder_available() else None

        with self._lock:
            self._evict_expired()
            entry = CacheEntry(
                query_hash=composite_hash,
                query_text=_cache_context_ref(query),
                response=redact_text(response),
                embedding=embedding,
                timestamp=time.monotonic(),
                model_id=model_id,
                system_prompt=system_prompt_ref,
                dense_embedding=dense_emb,
            )
            self._store[composite_hash] = entry
            self._store.move_to_end(composite_hash)

            # Tier 1: exact index (composite_hash → composite_hash)
            self._exact_index[composite_hash] = composite_hash

            # Tier 2: MinHash LSH — keyed by composite_hash so different
            # model_id/system_prompt entries live at distinct LSH keys.
            if _DATASKETCH_AVAILABLE and self._minhash_index is not None:
                minhash = _make_minhash(query)
                try:
                    # Remove stale entry first (LSH insert is not idempotent)
                    if composite_hash in self._minhash_index:
                        self._minhash_index.remove(composite_hash)
                    self._minhash_index.insert(composite_hash, minhash)
                except Exception as exc:
                    logger.warning("MinHash insert failed for key %s: %s", composite_hash[:8], exc)

            self._evict_lru()

    def get_stats(self) -> dict:
        """Return cache statistics including per-tier hit counters.

        Returns:
            Dictionary with keys: ``hit_rate``, ``cache_size``,
            ``estimated_savings`` (token count), ``total_hits``,
            ``total_misses``, ``exact_hits``, ``minhash_hits``,
            ``semantic_hits``, ``trigram_hits``.
        """
        with self._lock:
            total = self._hits + self._misses
            hit_rate = self._hits / total if total > 0 else 0.0
            return {
                "hit_rate": hit_rate,
                "cache_size": len(self._store),
                "estimated_savings": self._tokens_saved,
                "total_hits": self._hits,
                "total_misses": self._misses,
                "exact_hits": self._exact_hits,
                "minhash_hits": self._minhash_hits,
                "semantic_hits": self._semantic_hits,
                "trigram_hits": self._trigram_hits,
            }

    def clear(self) -> None:
        """Remove all entries and reset statistics."""
        with self._lock:
            self._store.clear()
            self._exact_index.clear()
            if _DATASKETCH_AVAILABLE and self._minhash_index is not None:
                # Re-create the LSH index (no bulk-clear API in datasketch)
                self._minhash_index = MinHashLSH(threshold=_MINHASH_THRESHOLD, num_perm=_MINHASH_NUM_PERM)
            self._hits = 0
            self._misses = 0
            self._tokens_saved = 0
            self._exact_hits = 0
            self._minhash_hits = 0
            self._semantic_hits = 0
            self._trigram_hits = 0

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _evict_expired(self) -> None:
        """Remove expired entries (must be called under lock)."""
        now = time.monotonic()
        expired = [k for k, e in self._store.items() if now - e.timestamp >= self._ttl]
        for k in expired:
            del self._store[k]
            self._exact_index.pop(k, None)
            if _DATASKETCH_AVAILABLE and self._minhash_index is not None:
                try:
                    if k in self._minhash_index:
                        self._minhash_index.remove(k)
                except Exception as exc:
                    logger.warning("MinHash evict-expired remove failed for key %s: %s", k[:8], exc)

    def _evict_lru(self) -> None:
        """Remove least-recently-used entries until within capacity (must be called under lock)."""
        while len(self._store) > self._max_entries:
            key, _ = self._store.popitem(last=False)
            self._exact_index.pop(key, None)
            if _DATASKETCH_AVAILABLE and self._minhash_index is not None:
                try:
                    if key in self._minhash_index:
                        self._minhash_index.remove(key)
                except Exception as exc:
                    logger.warning("MinHash evict-lru remove failed for key %s: %s", key[:8], exc)

    # ------------------------------------------------------------------
    # Similarity (exposed for testing)
    # ------------------------------------------------------------------

    @staticmethod
    def _compute_similarity(a: str, b: str) -> float:
        """Compute character trigram Jaccard similarity between two strings.

        Args:
            a: First string.
            b: Second string.

        Returns:
            Jaccard similarity score in [0, 1].
        """
        return _jaccard(_trigrams(a), _trigrams(b))


# ---------------------------------------------------------------------------
# Module-level similarity helpers
# ---------------------------------------------------------------------------


def _trigrams(text: str) -> frozenset[str]:
    """Extract character trigrams from *text*.

    Args:
        text: Input string.

    Returns:
        Frozenset of 3-character substrings.
    """
    t = text.lower()
    if len(t) < 3:
        return frozenset({t}) if t else frozenset()
    return frozenset(t[i : i + 3] for i in range(len(t) - 2))


def _jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    """Compute Jaccard similarity between two frozensets.

    Args:
        a: First set.
        b: Second set.

    Returns:
        |a n b| / |a U b|, or 1.0 if both sets are empty.
    """
    if not a and not b:
        return 1.0
    union = a | b
    if not union:
        return 0.0
    return len(a & b) / len(union)


def _make_minhash(text: str) -> Any:
    """Create a MinHash from character trigrams of *text*.

    Args:
        text: Input string.

    Returns:
        A ``datasketch.MinHash`` with trigrams as shingles.
    """
    mh = MinHash(num_perm=_MINHASH_NUM_PERM)
    for trigram in _trigrams(text):
        mh.update(trigram.encode("utf-8"))
    return mh


# ---------------------------------------------------------------------------
# Singleton accessor
# ---------------------------------------------------------------------------


def get_semantic_cache(
    ttl: int = _DEFAULT_TTL_SECONDS,
    max_entries: int = CACHE_MAX_ENTRIES_SEMANTIC,
    similarity_threshold: float = _DEFAULT_SIMILARITY_THRESHOLD,
) -> SemanticCache:
    """Return the module-level singleton :class:`SemanticCache`.

    Args:
        ttl: TTL in seconds (used on first creation only).
        max_entries: Max entries (used on first creation only).
        similarity_threshold: Default similarity threshold (used on first creation only).

    Returns:
        The singleton :class:`SemanticCache` instance.
    """
    global _instance
    if _instance is None:
        with _instance_lock:
            if _instance is None:
                _instance = SemanticCache(
                    ttl=ttl,
                    max_entries=max_entries,
                    similarity_threshold=similarity_threshold,
                )
    return _instance


def reset_semantic_cache() -> None:
    """Destroy the singleton so the next call to ``get_semantic_cache`` creates a fresh one.

    Intended for use in tests only.
    """
    global _instance
    with _instance_lock:
        _instance = None
