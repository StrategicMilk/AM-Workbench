"""Model Scout — Online discovery and recommendation loop.

When Thompson Sampling identifies task types where all available models score
poorly, the scout searches for better models using existing ModelDiscovery
adapters (HuggingFace, Reddit, GitHub, PapersWithCode).

Factory analogy: equipment procurement — the factory actively scouts for
better machines when current equipment underperforms.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from dataclasses import replace as dataclass_replace
from enum import Enum
from functools import lru_cache
from typing import Any

from vetinari.models.model_scout_freshness import (
    FRESHNESS_CHECK_INTERVAL_DAYS,
    ModelFreshnessChecker,
    ModelUpgradeCandidate,
)

logger = logging.getLogger(__name__)


class BackendEnum(str, Enum):
    """Supported model scout backend recommendations."""

    LLAMA_CPP = "llama_cpp"
    UNKNOWN = "unknown"


__all__ = [
    "FRESHNESS_CHECK_INTERVAL_DAYS",
    "ModelFreshnessChecker",
    "ModelRecommendation",
    "ModelScout",
    "ModelUpgradeCandidate",
    "get_model_scout",
    "reset_model_scout",
]


def _infer_candidate_format(candidate: Any) -> str:
    identity = f"{getattr(candidate, 'id', '')} {getattr(candidate, 'name', '')}".lower()
    metrics = getattr(candidate, "metrics", {}) or {}
    if isinstance(metrics, dict):
        identity = f"{identity} {' '.join(str(value) for value in metrics.values())}".lower()
    if "gguf" in identity:
        return "gguf"
    if "awq" in identity:
        return "awq"
    if "gptq" in identity:
        return "gptq"
    return "safetensors"


def _backend_for_format(model_format: str) -> str:
    if model_format == "gguf":
        return BackendEnum.LLAMA_CPP.value
    logger.info(
        "format %r has no supported backend in current AM Engine configuration; returning UNKNOWN",
        model_format,
    )
    return BackendEnum.UNKNOWN.value


@lru_cache(maxsize=1)
def _get_model_discovery() -> Any:
    from vetinari.model_discovery import ModelDiscovery

    return ModelDiscovery()


@dataclass(frozen=True, slots=True)
class ModelRecommendation:
    """A recommended model from the scout.

    Args:
        model_name: The model name/identifier.
        source: Where it was found (huggingface, reddit, github, etc.).
        task_type: The task type this model excels at.
        estimated_quality: Estimated quality score based on benchmarks/sentiment.
        vram_estimate_gb: Estimated VRAM requirement in GB.
        reason: Why this model was recommended.
        recommended_backend: Backend to use for download/inference.
        recommended_format: Artifact format to download.
    """

    model_name: str = ""
    source: str = ""
    task_type: str = ""
    estimated_quality: float = 0.0
    vram_estimate_gb: float = 0.0
    reason: str = ""
    recommended_backend: str = "unknown"
    recommended_format: str = "safetensors"

    def __repr__(self) -> str:
        """Show key identifying fields for debugging."""
        return (
            f"ModelRecommendation(model_name={self.model_name!r},"
            f" task_type={self.task_type!r},"
            f" estimated_quality={self.estimated_quality!r})"
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dictionary.

        Returns:
            Dictionary representation of the recommendation.
        """
        return {
            "model_name": self.model_name,
            "source": self.source,
            "task_type": self.task_type,
            "estimated_quality": round(self.estimated_quality, 3),
            "vram_estimate_gb": round(self.vram_estimate_gb, 1),
            "reason": self.reason,
            "recommended_backend": self.recommended_backend,
            "recommended_format": self.recommended_format,
        }


class ModelScout:
    """Scouts for better models when current ones underperform.

    Integrates with Thompson Sampling to detect underperformance and
    with ModelDiscovery to search for alternatives.
    """

    UNDERPERFORMANCE_THRESHOLD = 0.5  # Beta mean below this triggers scouting
    MIN_DISCOVERY_IMPROVEMENT = 0.05
    MAX_RECOMMENDATIONS = 5
    MAX_CACHE_AGE_SECONDS = FRESHNESS_CHECK_INTERVAL_DAYS * 24 * 60 * 60

    # Map task types to focused search queries for ModelDiscovery adapters.
    # Prefer AM Engine-compatible snapshots and GGUF llama.cpp fallbacks; vLLM
    # is intentionally not emitted as a scout recommendation backend.
    SEARCH_QUERIES: dict[str, str] = {
        "coding": "best coding LLM AM Engine safetensors GGUF local 2026",
        "reasoning": "best reasoning LLM AM Engine safetensors GGUF local 2026",
        "general": "best general purpose LLM AM Engine safetensors GGUF local 2026",
        "review": "best code review LLM AM Engine safetensors GGUF local 2026",
        "architecture": "best architecture design LLM AM Engine safetensors GGUF local 2026",
    }

    def __init__(self) -> None:
        self._cache: dict[str, list[ModelRecommendation]] = {}
        # Tracks when each task_type was last populated so we can warn on stale hits
        self._cache_timestamps: dict[str, float] = {}
        self._lock = threading.Lock()

    def is_underperforming(self, task_type: str) -> bool:
        """Check if all available models are underperforming for a task type.

        Returns True when all models tracked by Thompson Sampling have a Beta
        distribution mean below UNDERPERFORMANCE_THRESHOLD for the given task
        type.

        Args:
            task_type: The task type to check.

        Returns:
            True if all models are underperforming, False otherwise.
        """
        try:
            from vetinari.learning.model_selector import get_thompson_selector

            selector = get_thompson_selector()
            rankings = selector.get_rankings(task_type)
            if not rankings:
                return False
            return all(mean < self.UNDERPERFORMANCE_THRESHOLD for _, mean in rankings)
        except Exception:
            logger.warning("Cannot check model performance for task_type=%s", task_type, exc_info=True)
            return False

    def _cache_age_s(self, task_type: str) -> float:
        """Return how many seconds ago the cache for task_type was populated.

        Args:
            task_type: The task type key to look up.

        Returns:
            Age in seconds, or 0.0 if no timestamp recorded.
        """
        ts = self._cache_timestamps.get(task_type, 0.0)
        return time.time() - ts if ts > 0 else 0.0

    @staticmethod
    def _best_current_score(task_type: str) -> float | None:
        """Return the best known current-model score for task_type, if measured."""
        try:
            from vetinari.learning.model_selector import get_thompson_selector

            rankings = get_thompson_selector().get_rankings(task_type)
        except Exception:
            logger.warning(
                "Cannot load current model scores for task_type=%s; model scout will not filter by incumbent score",
                task_type,
                exc_info=True,
            )
            return None
        if not rankings:
            return None
        return max(mean for _, mean in rankings)

    def scout_for_task(self, task_type: str) -> list[ModelRecommendation]:
        """Search for models that excel at the given task type.

        Uses ModelDiscovery adapters to search HuggingFace, Reddit, GitHub,
        and PapersWithCode. Results are cached with an age gate so stale
        recommendations are invalidated automatically; clear_cache() can still
        force an immediate refresh.

        Args:
            task_type: The task type to search for (e.g., "coding", "reasoning").

        Returns:
            Ranked list of model recommendations, sorted by estimated quality
            descending.
        """
        best_current_score = self._best_current_score(task_type)
        with self._lock:
            if task_type in self._cache:
                age_s = self._cache_age_s(task_type)
                if age_s >= self.MAX_CACHE_AGE_SECONDS:
                    logger.warning(
                        "Invalidating stale model scout cache for task_type=%s; cached %.0fs ago",
                        task_type,
                        age_s,
                    )
                    del self._cache[task_type]
                    self._cache_timestamps.pop(task_type, None)
                    cache_clear = getattr(_get_model_discovery, "cache_clear", None)
                    if callable(cache_clear):
                        cache_clear()
                else:
                    return _filter_recommendations_by_incumbent(self._cache[task_type], best_current_score)

        recommendations: list[ModelRecommendation] = []
        query = self.SEARCH_QUERIES.get(
            task_type,
            f"best {task_type} LLM AM Engine safetensors GGUF local",
        )

        try:
            from vetinari.resilience.wiring import call_with_breaker

            candidates = call_with_breaker("model_scout", _get_model_discovery().search, query)

            for candidate in candidates[: self.MAX_RECOMMENDATIONS]:
                if not _recommendation_outperforms_incumbent(candidate.final_score, best_current_score):
                    continue
                recommended_format = _infer_candidate_format(candidate)
                rec = ModelRecommendation(
                    model_name=candidate.name or candidate.id,
                    source=candidate.source_type,
                    task_type=task_type,
                    estimated_quality=candidate.final_score,
                    vram_estimate_gb=float(candidate.memory_gb),
                    reason=candidate.short_rationale or f"Found via {candidate.source_type}",
                    recommended_backend=_backend_for_format(recommended_format),
                    recommended_format=recommended_format,
                )
                recommendations.append(rec)
        except Exception:
            logger.warning(
                "ModelDiscovery unavailable for task_type=%s, returning empty recommendations",
                task_type,
                exc_info=True,
            )

        # Sort by estimated quality descending before deduplication so the first
        # occurrence of each model_name is always the highest-quality entry.
        recommendations.sort(key=lambda r: r.estimated_quality, reverse=True)

        # Deduplicate by model_name — keep first (highest-quality) occurrence.
        seen: set[str] = set()
        deduplicated: list[ModelRecommendation] = []
        for rec in recommendations:
            if rec.model_name not in seen:
                seen.add(rec.model_name)
                deduplicated.append(rec)

        with self._lock:
            self._cache[task_type] = deduplicated
            self._cache_timestamps[task_type] = time.time()

        return deduplicated

    def get_recommendations(self, task_type: str) -> list[ModelRecommendation]:
        """Get ranked recommendations for a task type.

        Checks the in-memory cache first, then scouts via ModelDiscovery if
        no cached results exist. Returns a defensive copy of each recommendation
        so callers cannot mutate the internal cache state.

        Args:
            task_type: The task type to get recommendations for.

        Returns:
            Ranked list of new ModelRecommendation objects (copies, not references).
        """
        # Defensive copy: callers must not be able to mutate the cached list or
        # the individual recommendation objects — both are returned as fresh copies.
        return [dataclass_replace(r) for r in self.scout_for_task(task_type)]

    def get_status(self) -> dict[str, Any]:
        """Return scout status for health checks.

        Returns:
            Dictionary with ok flag, cached task types, and total recommendation
            count.
        """
        with self._lock:
            return {
                "ok": True,
                "cached_task_types": list(self._cache.keys()),
                "total_recommendations": sum(len(recs) for recs in self._cache.values()),
            }

    def clear_cache(self) -> None:
        """Clear the in-memory recommendation cache.

        Forces fresh ModelDiscovery searches on the next call to
        get_recommendations or scout_for_task.
        """
        with self._lock:
            self._cache.clear()
            self._cache_timestamps.clear()


# ── Model Freshness Checker ──────────────────────────────────────────────────
# Periodic check for newer, better models that have been released since the
# user's current models were downloaded.  Compares against community benchmarks
# (Open LLM Leaderboard, LiveCodeBench) and user sentiment (HuggingFace likes,
# Reddit discussions) to surface genuinely better options.
#
# Runs weekly via the kaizen system or manually via `vetinari check-models`.


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_model_scout: ModelScout | None = None
_scout_lock = threading.Lock()


def get_model_scout() -> ModelScout:
    """Return the singleton ModelScout instance (thread-safe, lazy init).

    Returns:
        The shared ModelScout instance.
    """
    global _model_scout
    if _model_scout is None:
        with _scout_lock:
            if _model_scout is None:
                _model_scout = ModelScout()
    return _model_scout


def reset_model_scout() -> None:
    """Reset the singleton ModelScout for testing.

    After calling this, the next call to get_model_scout creates a fresh
    instance. Only intended for use in test teardown.
    """
    global _model_scout
    with _scout_lock:
        _model_scout = None
    _get_model_discovery.cache_clear()


def _recommendation_outperforms_incumbent(candidate_score: float, best_current_score: float | None) -> bool:
    if best_current_score is None:
        return True
    return candidate_score >= best_current_score + ModelScout.MIN_DISCOVERY_IMPROVEMENT


def _filter_recommendations_by_incumbent(
    recommendations: list[ModelRecommendation],
    best_current_score: float | None,
) -> list[ModelRecommendation]:
    if best_current_score is None:
        return recommendations
    return [
        recommendation
        for recommendation in recommendations
        if _recommendation_outperforms_incumbent(recommendation.estimated_quality, best_current_score)
    ]
