"""Provider-level prompt caching for Vetinari (P10.6).

Caches system prompts and repeated context to avoid re-sending identical
content to the model provider, reducing token costs for repeated inference.
"""

from __future__ import annotations

import hashlib
import logging
import re
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass

from vetinari.constants import CACHE_MAX_ENTRIES_PROMPT, CACHE_TTL_ONE_HOUR
from vetinari.security.fail_closed import UntrustedInputError, sanitize_untrusted_text

logger = logging.getLogger(__name__)


_DEFAULT_TTL_SECONDS: int = CACHE_TTL_ONE_HOUR

_instance: PromptCache | None = None
_instance_lock: threading.Lock = threading.Lock()
_CACHE_KEY_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


@dataclass(frozen=True, slots=True)
class CacheResult:
    """Result returned from a prompt cache lookup.

    Attributes:
        hit: True if the prompt was found in the cache.
        prompt: The cached or original prompt string.
        savings_tokens: Estimated tokens saved (0 on miss).
    """

    hit: bool
    prompt: str
    savings_tokens: int


class PromptCache:
    """Thread-safe LRU cache for system prompts and repeated context.

    Caches prompts by a caller-supplied hash.  Entries expire after ``ttl``
    seconds and the cache is capped at ``max_entries`` items with LRU eviction.

    Args:
        ttl: Time-to-live in seconds for each cached entry.  Default 3600.
        max_entries: Maximum number of entries before LRU eviction.  Default 1000.
    """

    def __init__(
        self,
        ttl: int = _DEFAULT_TTL_SECONDS,
        max_entries: int = CACHE_MAX_ENTRIES_PROMPT,
    ) -> None:
        self._ttl = ttl
        self._max_entries = max_entries
        self._lock = threading.Lock()
        # OrderedDict used as the LRU store: key -> (prompt_digest, token_estimate, expiry_ts).
        # Raw prompt text is intentionally not retained.
        self._store: OrderedDict[str, tuple[str, int, float]] = OrderedDict()
        self._hits: int = 0
        self._misses: int = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_or_cache(self, prompt_hash: str, prompt: str) -> CacheResult:
        """Return a cached prompt if available, otherwise store it.

        On a cache hit the entry is moved to the most-recently-used position.
        Expired entries are treated as misses and replaced.

        Args:
            prompt_hash: A stable hash that uniquely identifies *prompt*.
                         Callers should use ``hash_prompt()`` for consistency.
            prompt: The full prompt string to cache on a miss.

        Returns:
            A :class:`CacheResult` describing whether the lookup was a hit and
            the estimated token savings.

        Raises:
            UntrustedInputError: If the prompt hash or prompt is unsafe.
        """
        prompt_hash = _validate_cache_key(prompt_hash)
        if not isinstance(prompt, str):
            raise UntrustedInputError("prompt must be a string")

        with self._lock:
            self._evict_expired()
            now = time.monotonic()

            if prompt_hash in self._store:
                _prompt_digest, token_estimate, expiry = self._store[prompt_hash]
                if now < expiry:
                    # Move to end (most recently used)
                    self._store.move_to_end(prompt_hash)
                    self._hits += 1
                    return CacheResult(hit=True, prompt=prompt, savings_tokens=token_estimate)
                # Expired — fall through to miss handling
                del self._store[prompt_hash]

            # Miss: store the prompt
            self._misses += 1
            self._store[prompt_hash] = (hash_prompt(prompt), _estimate_tokens(prompt), now + self._ttl)
            self._store.move_to_end(prompt_hash)
            self._evict_lru()
            return CacheResult(hit=False, prompt=prompt, savings_tokens=0)

    def invalidate(self, prompt_hash: str) -> None:
        """Remove a specific entry from the cache.

        Args:
            prompt_hash: The hash key of the entry to remove.
        """
        with self._lock:
            self._store.pop(prompt_hash, None)

    def clear(self) -> None:
        """Remove all entries and reset statistics."""
        with self._lock:
            self._store.clear()
            self._hits = 0
            self._misses = 0

    def get_stats(self) -> dict:
        """Return cache statistics.

        Returns:
            Dictionary with keys: ``hit_rate``, ``total_hits``,
            ``total_misses``, ``cache_size``.
        """
        with self._lock:
            total = self._hits + self._misses
            hit_rate = self._hits / total if total > 0 else 0.0
            return {
                "hit_rate": hit_rate,
                "total_hits": self._hits,
                "total_misses": self._misses,
                "cache_size": len(self._store),
            }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _evict_expired(self) -> None:
        """Remove all expired entries (must be called under lock)."""
        now = time.monotonic()
        expired_keys = [k for k, (_, _, exp) in self._store.items() if now >= exp]
        for k in expired_keys:
            del self._store[k]

    def _evict_lru(self) -> None:
        """Remove least-recently-used entries until within capacity (must be called under lock)."""
        while len(self._store) > self._max_entries:
            self._store.popitem(last=False)


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def hash_prompt(prompt: str) -> str:
    """Compute a stable SHA-256 hex digest for a prompt string.

    Args:
        prompt: The prompt text to hash.

    Returns:
        A 64-character hex string suitable as a cache key.

    Raises:
        UntrustedInputError: If ``prompt`` is not a string.
    """
    if not isinstance(prompt, str):
        raise UntrustedInputError("prompt must be a string")
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()


def _estimate_tokens(text: str) -> int:
    """Rough token count estimate: ~4 characters per token.

    Args:
        text: The text to estimate tokens for.

    Returns:
        Estimated token count.
    """
    return max(1, len(text) // 4)


def _validate_cache_key(value: object) -> str:
    key = sanitize_untrusted_text(value, max_length=128)
    if not _CACHE_KEY_RE.fullmatch(key):
        raise UntrustedInputError("prompt cache key contains unsupported characters")
    return key


# ---------------------------------------------------------------------------
# Singleton accessor
# ---------------------------------------------------------------------------


def get_prompt_cache(
    ttl: int = _DEFAULT_TTL_SECONDS,
    max_entries: int = CACHE_MAX_ENTRIES_PROMPT,
) -> PromptCache:
    """Return the module-level singleton :class:`PromptCache`.

    The first call creates the instance with the supplied parameters; subsequent
    calls return the existing instance (parameters ignored).

    Args:
        ttl: TTL in seconds for new cache entries.
        max_entries: Maximum number of entries before LRU eviction.

    Returns:
        The singleton :class:`PromptCache` instance.
    """
    global _instance
    if _instance is None:
        with _instance_lock:
            if _instance is None:
                _instance = PromptCache(ttl=ttl, max_entries=max_entries)
    return _instance


def reset_prompt_cache() -> None:
    """Destroy the singleton so the next call to ``get_prompt_cache`` creates a fresh one.

    Intended for use in tests only.
    """
    global _instance
    with _instance_lock:
        _instance = None
