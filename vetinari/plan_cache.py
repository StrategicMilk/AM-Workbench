"""Plan caching layer — reuse past plans for similar goals."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from vetinari.constants import get_user_dir
from vetinari.learning.atomic_writers import write_json_atomic
from vetinari.privacy.envelope import PRIVACY_ENVELOPE_KEY, extract_privacy_envelope, privacy_receipt
from vetinari.security.redaction import redact_text, redact_value
from vetinari.utils.serialization import dataclass_to_dict

logger = logging.getLogger(__name__)


class PlanCachePrivacyError(ValueError):
    """Raised when persisted plan-cache state lacks a valid privacy receipt."""


def _plan_privacy_receipt(goal_hash: str) -> dict[str, Any]:
    return privacy_receipt(
        privacy_class="operational",
        retention_days=30,
        source="plan_cache",
        erasure_token=f"plan_cache:{goal_hash}",
    )


def _validate_plan_privacy_receipt(envelope: Any, *, context: str) -> dict[str, Any]:
    if not isinstance(envelope, dict):
        raise PlanCachePrivacyError(f"{context} missing privacy envelope")
    try:
        return extract_privacy_envelope({PRIVACY_ENVELOPE_KEY: envelope})
    except Exception as exc:
        raise PlanCachePrivacyError(f"{context} has invalid privacy envelope: {exc}") from exc


@dataclass
class CachedPlan:
    """Cached plan."""

    goal: str
    goal_hash: str
    plan_data: dict[str, Any]
    created_at: float
    hit_count: int = 0
    last_hit: float = 0.0
    quality_score: float = 0.0
    privacy_envelope: dict[str, Any] = field(default_factory=lambda: _plan_privacy_receipt("unknown"))

    def __repr__(self) -> str:
        """Show key identifying fields for debugging."""
        return (
            f"CachedPlan(goal_hash={self.goal_hash!r},"
            f" hit_count={self.hit_count!r}, quality_score={self.quality_score!r})"
        )

    def to_dict(self) -> dict:
        """Serialize to a JSON-compatible dictionary."""
        return dataclass_to_dict(self)

    @classmethod
    def from_dict(cls, d: dict) -> CachedPlan:
        """Reconstruct a CachedPlan from a serialized dictionary.

        Args:
            d: Dictionary previously produced by ``to_dict()``.

        Returns:
            A new CachedPlan instance populated from the dictionary values.
        """
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


class PlanCache:
    """Cache past plans keyed by goal similarity.

    30s planning -> 300ms cache hit on similar goals.
    Uses keyword overlap for similarity matching (no embedding model needed).
    """

    DEFAULT_CACHE_DIR = "plan_cache"
    MAX_CACHE_SIZE = 100
    DEFAULT_THRESHOLD = 0.6
    DEFAULT_MAX_AGE_DAYS = 30

    def __init__(self, cache_dir: str | None = None):
        self._cache_dir = self._resolve_cache_dir(cache_dir)
        self._cache: dict[str, CachedPlan] = {}
        self._loaded = False

    @classmethod
    def _resolve_cache_dir(cls, cache_dir: str | None = None) -> Path:
        if cache_dir:
            return Path(cache_dir)
        if override := os.environ.get("VETINARI_PLAN_CACHE_DIR"):
            return Path(override)
        return get_user_dir() / cls.DEFAULT_CACHE_DIR

    def _ensure_loaded(self):
        if not self._loaded:
            self._load_cache()
            self._loaded = True

    def _load_cache(self):
        cache_file = self._cache_dir / "plans.json"
        if cache_file.exists():
            try:
                data = json.loads(cache_file.read_text(encoding="utf-8"))
                if not isinstance(data, list):
                    raise PlanCachePrivacyError("plan cache root must be a list")
                for entry in data:
                    if not isinstance(entry, dict):
                        raise PlanCachePrivacyError("plan cache entry must be an object")
                    _validate_plan_privacy_receipt(
                        entry.get("privacy_envelope"),
                        context=f"plan cache entry {entry.get('goal_hash', '<unknown>')}",
                    )
                    plan = CachedPlan.from_dict(entry)
                    self._cache[plan.goal_hash] = plan
            except Exception as e:
                raise PlanCachePrivacyError(f"Plan cache load error: {e}") from e

    def _save_cache(self):
        cache_file = self._cache_dir / "plans.json"
        data = [p.to_dict() for p in self._cache.values()]
        for entry in data:
            _validate_plan_privacy_receipt(
                entry.get("privacy_envelope"),
                context=f"plan cache entry {entry.get('goal_hash', '<unknown>')}",
            )
        write_json_atomic(cache_file, data)

    @staticmethod
    def _goal_hash(goal: str) -> str:
        normalized = " ".join(goal.lower().split())
        return hashlib.sha256(normalized.encode()).hexdigest()[:16]

    def _extract_keywords(self, text: str) -> set:
        stop_words = {
            "the",
            "a",
            "an",
            "is",
            "are",
            "was",
            "were",
            "be",
            "been",
            "being",
            "have",
            "has",
            "had",
            "do",
            "does",
            "did",
            "will",
            "would",
            "could",
            "should",
            "may",
            "might",
            "can",
            "shall",
            "to",
            "of",
            "in",
            "for",
            "on",
            "with",
            "at",
            "by",
            "from",
            "and",
            "or",
            "but",
            "not",
            "no",
            "if",
            "then",
            "than",
            "that",
            "this",
            "it",
            "its",
            "my",
            "your",
            "we",
            "they",
            "i",
            "me",
        }
        words = set(text.lower().split())
        return words - stop_words

    def _similarity(self, goal_a: str, goal_b: str) -> float:
        kw_a = self._extract_keywords(goal_a)
        kw_b = self._extract_keywords(goal_b)
        if not kw_a or not kw_b:
            return 0.0
        intersection = kw_a & kw_b
        union = kw_a | kw_b
        return len(intersection) / len(union) if union else 0.0

    def _evict_expired_loaded(self, older_than_days: int | None = None) -> int:
        older_than_days = older_than_days or self.DEFAULT_MAX_AGE_DAYS
        cutoff = time.time() - (older_than_days * 86400)
        expired = [goal_hash for goal_hash, plan in self._cache.items() if plan.created_at < cutoff]
        for goal_hash in expired:
            del self._cache[goal_hash]
        if expired:
            self._save_cache()
        return len(expired)

    def find_similar(self, goal: str, threshold: float | None = None) -> CachedPlan | None:
        """Find a cached plan similar to the given goal.

        Args:
            goal: The goal.
            threshold: The threshold.

        Returns:
            The CachedPlan | None result.
        """
        self._ensure_loaded()
        threshold = threshold or self.DEFAULT_THRESHOLD
        self._evict_expired_loaded()

        # Exact match first
        goal_hash = self._goal_hash(goal)
        if goal_hash in self._cache:
            plan = self._cache[goal_hash]
            if plan.quality_score <= 0:
                logger.info("Plan cache exact hit rejected because quality_score=%.3f", plan.quality_score)
                return None
            plan.hit_count += 1
            plan.last_hit = time.time()
            return plan

        # Similarity search
        best_plan = None
        best_score = 0.0

        for plan in self._cache.values():
            if plan.quality_score <= 0:
                continue
            score = self._similarity(goal, plan.goal)
            if score > best_score and score >= threshold:
                best_score = score
                best_plan = plan

        if best_plan:
            best_plan.hit_count += 1
            best_plan.last_hit = time.time()
            logger.info("Plan cache hit (similarity=%.2f)", best_score)

        return best_plan

    def store(self, goal: str, plan_data: dict[str, Any], quality_score: float = 0.0) -> None:
        """Store a plan in the cache.

        Args:
            goal: The goal.
            plan_data: The plan data.
            quality_score: The quality score.
        """
        self._ensure_loaded()

        goal_hash = self._goal_hash(goal)
        self._cache[goal_hash] = CachedPlan(
            goal=redact_text(goal),
            goal_hash=goal_hash,
            plan_data=redact_value(plan_data),
            created_at=time.time(),
            quality_score=quality_score,
            privacy_envelope=_plan_privacy_receipt(goal_hash),
        )

        # Evict oldest if over limit
        if len(self._cache) > self.MAX_CACHE_SIZE:
            oldest_key = min(self._cache, key=lambda k: self._cache[k].last_hit or self._cache[k].created_at)
            del self._cache[oldest_key]

        self._save_cache()

    def invalidate(self, older_than_days: int | None = None) -> int:
        """Remove stale entries. Returns count of removed entries.

        Returns:
            Number of cache entries that were expired and deleted.
        """
        self._ensure_loaded()
        return self._evict_expired_loaded(older_than_days)

    def get_stats(self) -> dict[str, Any]:
        """Return aggregate statistics about the plan cache.

        Returns:
            Dictionary with total_cached (entry count), total_hits (sum of
            hit_count across all entries), and avg_quality (mean quality score).
        """
        self._ensure_loaded()
        return {
            "total_cached": len(self._cache),
            "total_hits": sum(p.hit_count for p in self._cache.values()),
            "avg_quality": (sum(p.quality_score for p in self._cache.values()) / max(len(self._cache), 1)),
        }


_plan_cache: PlanCache | None = None
_plan_cache_lock = threading.Lock()


def get_plan_cache(cache_dir: str | None = None) -> PlanCache:
    """Return the process-global PlanCache, creating it on first call.

    Args:
        cache_dir: Optional custom cache directory; only used when
            constructing the instance for the first time.

    Returns:
        The singleton PlanCache instance.
    """
    global _plan_cache
    if _plan_cache is None:
        with _plan_cache_lock:
            if _plan_cache is None:
                _plan_cache = PlanCache(cache_dir)
    return _plan_cache
