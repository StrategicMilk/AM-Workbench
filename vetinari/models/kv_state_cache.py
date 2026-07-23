"""Prompt KV State Cache — reuse computed KV cache for repeated system prompts.

When the same system prompt is used across multiple inference calls (common
for agent identity prompts), we can save and restore the llama.cpp KV cache
state to avoid recomputing the prefix on every call, saving 1-5 seconds.

The cache is keyed by a hash of the system prompt text.  When the prompt
changes (e.g., after a prompt evolver promotion), the old entry is
invalidated automatically.

Usage::

    from vetinari.models.kv_state_cache import get_kv_state_cache

    cache = get_kv_state_cache()
    state = cache.get(model_id, system_prompt_hash)
    if state is not None:
        model.load_state(state)
    else:
        # ... run inference with system prompt ...
        cache.put(model_id, system_prompt_hash, model.save_state())
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


# Maximum number of cached states per model partition (scales with model count)
MAX_CACHE_ENTRIES_PER_MODEL = 20

# Total cache cap across all models (prevents unbounded memory growth)
MAX_CACHE_ENTRIES_TOTAL = 200

# Maximum age of a cache entry in seconds (1 hour)
MAX_CACHE_AGE_SECONDS = 3600


@dataclass
class _CacheEntry:
    """A cached KV state with metadata for eviction."""

    model_id: str
    prompt_hash: str
    state: Any  # llama.cpp state bytes
    created_at: float
    last_used: float
    hit_count: int = 0

    def __repr__(self) -> str:
        return f"_CacheEntry(model={self.model_id!r}, hash={self.prompt_hash[:8]!r}, hits={self.hit_count})"


def hash_system_prompt(prompt: str) -> str:
    """Compute a stable hash of a system prompt for cache keying.

    Args:
        prompt: The system prompt text.

    Returns:
        Hex digest string (first 16 chars of SHA256).
    """
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16]


class KVStateCache:
    """In-memory cache for llama.cpp KV state snapshots with per-model partitioning.

    Keyed by (model_id, prompt_hash) pairs. Each model gets its own partition
    capped at ``max_entries_per_model`` entries, with a global cap of
    ``max_entries_total`` across all models. LRU eviction within each partition
    prevents one model from starving others. Entries older than
    ``MAX_CACHE_AGE_SECONDS`` are automatically purged.
    """

    def __init__(
        self,
        max_entries_per_model: int = MAX_CACHE_ENTRIES_PER_MODEL,
        max_entries_total: int = MAX_CACHE_ENTRIES_TOTAL,
    ):
        self._cache: dict[tuple[str, str], _CacheEntry] = {}
        self._lock = threading.Lock()
        self._max_entries_per_model = max_entries_per_model
        self._max_entries_total = max_entries_total
        self._hits = 0
        self._misses = 0

    @staticmethod
    def _key(model_id: str, prompt_hash: str) -> tuple[str, str]:
        return (model_id, prompt_hash)

    def get(self, model_id: str, prompt_hash: str) -> Any | None:
        """Retrieve a cached KV state for a (model, prompt) pair.

        Args:
            model_id: The model identifier.
            prompt_hash: Hash of the system prompt (from ``hash_system_prompt``).

        Returns:
            The saved llama.cpp state, or None on cache miss.
        """
        key = self._key(model_id, prompt_hash)
        with self._lock:
            entry = self._cache.get(key)
            if entry is None:
                self._misses += 1
                return None

            # Check age
            if time.time() - entry.created_at > MAX_CACHE_AGE_SECONDS:
                del self._cache[key]
                self._misses += 1
                return None

            entry.last_used = time.time()
            entry.hit_count += 1
            self._hits += 1
            logger.debug("KV state cache HIT for %s (hash=%s, hits=%d)", model_id, prompt_hash[:8], entry.hit_count)
            return entry.state

    def put(self, model_id: str, prompt_hash: str, state: Any) -> None:
        """Store a KV state snapshot in the cache.

        Evicts the least-recently-used entry if the cache is full.

        Args:
            model_id: The model identifier.
            prompt_hash: Hash of the system prompt.
            state: The llama.cpp state bytes from ``model.save_state()``.
        """
        key = self._key(model_id, prompt_hash)
        now = time.time()

        with self._lock:
            # Evict stale entries
            stale_keys = [k for k, v in self._cache.items() if now - v.created_at > MAX_CACHE_AGE_SECONDS]
            for k in stale_keys:
                del self._cache[k]

            # Per-model partition eviction: tuple keys avoid aliasing when
            # model IDs themselves contain ":".
            model_entries = {k: v for k, v in self._cache.items() if k[0] == model_id}
            while len(model_entries) >= self._max_entries_per_model:
                oldest_key = min(model_entries, key=lambda k: model_entries[k].last_used)
                del self._cache[oldest_key]
                del model_entries[oldest_key]

            # Global cap eviction: evict LRU across all models if total exceeds cap
            while len(self._cache) >= self._max_entries_total:
                oldest_key = min(self._cache, key=lambda k: self._cache[k].last_used)
                del self._cache[oldest_key]

            self._cache[key] = _CacheEntry(
                model_id=model_id,
                prompt_hash=prompt_hash,
                state=state,
                created_at=now,
                last_used=now,
            )

        logger.debug("KV state cached for %s (hash=%s)", model_id, prompt_hash[:8])

    def invalidate(self, model_id: str, prompt_hash: str | None = None) -> int:
        """Remove cached state(s) for a model.

        Args:
            model_id: The model identifier.
            prompt_hash: If provided, only invalidate this specific prompt's
                cache. Otherwise, invalidate all entries for the model.

        Returns:
            Number of entries removed.
        """
        removed = 0
        with self._lock:
            if prompt_hash:
                key = self._key(model_id, prompt_hash)
                if key in self._cache:
                    del self._cache[key]
                    removed = 1
            else:
                keys_to_remove = [k for k in self._cache if k[0] == model_id]
                for k in keys_to_remove:
                    del self._cache[k]
                    removed += 1

        if removed:
            logger.debug("Invalidated %d KV state cache entries for %s", removed, model_id)
        return removed

    def get_stats(self) -> dict[str, Any]:
        """Return cache statistics.

        Returns:
            Dict with size, hits, misses, and hit_rate.
        """
        with self._lock:
            total = self._hits + self._misses
            # Count entries per model for partition visibility
            models: dict[str, int] = {}
            for entry in self._cache.values():
                models[entry.model_id] = models.get(entry.model_id, 0) + 1
            return {
                "size": len(self._cache),
                "max_entries_per_model": self._max_entries_per_model,
                "max_entries_total": self._max_entries_total,
                "hits": self._hits,
                "misses": self._misses,
                "hit_rate": self._hits / total if total > 0 else 0.0,
                "entries_per_model": models,
            }


# -- Singleton -----------------------------------------------------------------

_kv_cache: KVStateCache | None = None
_kv_cache_lock = threading.Lock()


def get_kv_state_cache() -> KVStateCache:
    """Return the singleton KVStateCache.

    Returns:
        The shared KVStateCache instance.
    """
    global _kv_cache
    if _kv_cache is None:
        with _kv_cache_lock:
            if _kv_cache is None:
                _kv_cache = KVStateCache()
    return _kv_cache


def reset_kv_state_cache() -> None:
    """Reset the singleton (for testing)."""
    global _kv_cache
    with _kv_cache_lock:
        _kv_cache = None
