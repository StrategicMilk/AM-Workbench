"""Freshness checking for model scout upgrade suggestions."""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any

from vetinari.constants import get_user_dir
from vetinari.learning.atomic_writers import write_json_atomic
from vetinari.reliability_guards import coerce_aware_datetime

logger = logging.getLogger(__name__)


def _backend_for_format(model_format: str) -> str:
    """Return the serving backend preferred for a model artifact format.

    Args:
        model_format: Artifact format such as ``gguf`` or ``safetensors``.

    Returns:
        Backend identifier used by download and inference surfaces.
    """
    return "llama_cpp" if model_format == "gguf" else "vllm"


@dataclass
class ModelUpgradeCandidate:
    """A model that may be better than what the user currently has.

    Attributes:
        current_model_id: The user's current model that this would replace.
        candidate_name: Name of the potentially better model.
        candidate_repo_id: HuggingFace repo ID for download.
        benchmark_score: Aggregate benchmark score (0.0-1.0, higher is better).
        sentiment_score: Community sentiment score (0.0-1.0, higher is better).
        overall_score: Combined score weighting benchmarks (60%) and sentiment (40%).
        available_formats: Model formats available (gguf, awq, gptq, safetensors).
        recommended_backend: Backend to prefer for the upgrade download.
        recommended_format: Artifact format to prefer for the upgrade download.
        vram_estimate_gb: Estimated VRAM requirement.
        reason: Why this model is recommended as an upgrade.
    """

    current_model_id: str = ""
    candidate_name: str = ""
    candidate_repo_id: str = ""
    benchmark_score: float = 0.0
    sentiment_score: float = 0.0
    overall_score: float = 0.0
    available_formats: list[str] = field(default_factory=list)
    recommended_backend: str = "vllm"
    recommended_format: str = "safetensors"
    vram_estimate_gb: float = 0.0
    reason: str = ""

    def __repr__(self) -> str:
        return (
            f"ModelUpgradeCandidate(candidate={self.candidate_name!r},"
            f" replaces={self.current_model_id!r},"
            f" score={self.overall_score:.3f})"
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dictionary.

        Returns:
            Dictionary representation of the upgrade candidate.
        """
        return {
            "current_model_id": self.current_model_id,
            "candidate_name": self.candidate_name,
            "candidate_repo_id": self.candidate_repo_id,
            "benchmark_score": round(self.benchmark_score, 3),
            "sentiment_score": round(self.sentiment_score, 3),
            "overall_score": round(self.overall_score, 3),
            "available_formats": self.available_formats,
            "recommended_backend": self.recommended_backend,
            "recommended_format": self.recommended_format,
            "vram_estimate_gb": round(self.vram_estimate_gb, 1),
            "reason": self.reason,
        }


# Benchmark weight (60%) vs sentiment weight (40%) for overall scoring
_BENCHMARK_WEIGHT = 0.6
_SENTIMENT_WEIGHT = 0.4

# How many days between automatic freshness checks
FRESHNESS_CHECK_INTERVAL_DAYS = 7


@lru_cache(maxsize=1)
def _get_model_discovery() -> Any:
    from vetinari.model_discovery import ModelDiscovery

    return ModelDiscovery()


def _best_current_model(task_type: str, current_models: dict[str, dict[str, Any]]) -> tuple[str, float]:
    best_current_score = 0.0
    best_current_id = ""
    for model_id, info in current_models.items():
        task_score = info.get("tasks", {}).get(task_type, 0.0)
        if task_score > best_current_score:
            best_current_score = task_score
            best_current_id = model_id
    return best_current_id, best_current_score


def _discover_upgrade_candidates(task_type: str) -> list[Any]:
    query = f"best {task_type} LLM 2026 benchmark vLLM NIM safetensors AWQ GPTQ"
    try:
        from vetinari.resilience.wiring import call_with_breaker

        return call_with_breaker("model_scout", _get_model_discovery().search, query)
    except Exception:
        logger.warning("ModelDiscovery unavailable for freshness check - no upgrade candidates found")
        return []


class ModelFreshnessChecker:
    """Periodically checks for newer, better-performing models.

    Compares the user's currently installed models against the latest
    community benchmarks and user sentiment signals.  When a significantly
    better model is found, it surfaces an upgrade suggestion.

    Benchmark sources: Open LLM Leaderboard, LiveCodeBench, HumanEval
    Sentiment sources: HuggingFace likes/downloads, Reddit mentions

    This is step 4 of the kaizen cycle:
    Monitor -> Detect -> **Suggest Upgrade** -> User Decision -> Install
    """

    # Only suggest upgrades with at least this much improvement
    MIN_IMPROVEMENT_THRESHOLD = 0.10  # 10% better than current model

    def __init__(self, vram_budget_gb: float = 32.0) -> None:
        self._vram_budget_gb = vram_budget_gb
        self._last_check_file = get_user_dir() / "last_model_check.json"
        self._lock = threading.Lock()

    def should_check(self) -> bool:
        """Whether it's time for a freshness check (weekly interval).

        Returns:
            True if more than FRESHNESS_CHECK_INTERVAL_DAYS have passed
            since the last check.
        """
        if not self._last_check_file.exists():
            return True  # noqa: VET024 - unreadable state means the freshness check is due.

        try:
            data = json.loads(self._last_check_file.read_text(encoding="utf-8"))
            last_check = coerce_aware_datetime(data.get("last_check", "2000-01-01"), label="last_check")
            days_since = (datetime.now(timezone.utc) - last_check).days
            return days_since >= FRESHNESS_CHECK_INTERVAL_DAYS
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:  # noqa: VET024
            logger.warning("Could not read last freshness check file - assuming check is due: %s", exc)
            return True  # noqa: VET024 - unreadable state means the freshness check is due.

    def check_for_upgrades(self) -> list[ModelUpgradeCandidate]:
        """Check for better models than what the user currently has.

        Queries ModelDiscovery for the latest models, scores them against
        benchmarks and community sentiment, and returns upgrade candidates
        that meaningfully outperform the current models.

        Returns:
            List of ModelUpgradeCandidate objects, sorted by overall score
            descending.  Empty if no upgrades are available.
        """
        with self._lock:
            candidates: list[ModelUpgradeCandidate] = []

            # Get current models from the adapter
            current_models = self._get_current_models()
            if not current_models:
                logger.info("No models currently installed — skipping freshness check")
                return []

            # Search for latest models across all formats
            for task_type in ("coding", "reasoning", "general"):
                task_candidates = self._find_upgrades_for_task(task_type, current_models)
                candidates.extend(task_candidates)

            # Record this check and persist candidates
            self._record_check(len(candidates), candidates)

            candidates.sort(key=lambda c: c.overall_score, reverse=True)
            if candidates:
                logger.info(
                    "Model freshness check found %d potential upgrade(s)",
                    len(candidates),
                )
            return candidates

    @staticmethod
    def _get_current_models() -> dict[str, dict[str, Any]]:
        """Get dict of currently installed models with their performance data.

        Returns:
            Dict mapping model_id to performance metadata.
        """
        try:
            from vetinari.learning.model_selector import get_thompson_selector

            selector = get_thompson_selector()
            result: dict[str, dict[str, Any]] = {}

            for task_type in ("coding", "reasoning", "general"):
                rankings = selector.get_rankings(task_type)
                for model_id, mean_score in rankings:
                    if model_id not in result:
                        result[model_id] = {
                            "model_id": model_id,
                            "best_task": task_type,
                            "best_score": mean_score,
                            "tasks": {},
                        }
                    result[model_id]["tasks"][task_type] = mean_score
            return result
        except Exception:
            logger.warning(
                "Thompson selector unavailable for freshness check — current model scores not factored into upgrade comparison"
            )
            return {}

    def _find_upgrades_for_task(
        self,
        task_type: str,
        current_models: dict[str, dict[str, Any]],
    ) -> list[ModelUpgradeCandidate]:
        """Search for models that outperform current models for a task type.

        Uses ModelDiscovery to find candidates, then scores them against
        benchmarks and sentiment.  Only returns candidates that exceed
        the improvement threshold.

        Args:
            task_type: Task type to search for (e.g. "coding").
            current_models: Dict of current models with performance scores.

        Returns:
            List of upgrade candidates for this task type.
        """
        best_current_id, best_current_score = _best_current_model(task_type, current_models)
        if not best_current_id:
            return []

        raw_candidates = _discover_upgrade_candidates(task_type)
        upgrades: list[ModelUpgradeCandidate] = []

        for candidate in raw_candidates[:10]:
            upgrade = self._build_upgrade_candidate(
                candidate,
                task_type=task_type,
                current_models=current_models,
                best_current_id=best_current_id,
                best_current_score=best_current_score,
            )
            if upgrade is None:
                continue
            upgrades.append(upgrade)

        return upgrades

    def _build_upgrade_candidate(
        self,
        candidate: Any,
        *,
        task_type: str,
        current_models: dict[str, dict[str, Any]],
        best_current_id: str,
        best_current_score: float,
    ) -> ModelUpgradeCandidate | None:
        candidate_name = candidate.name or candidate.id
        if any(candidate_name.lower() in mid.lower() for mid in current_models):
            return None
        vram_needed = float(candidate.memory_gb)
        if vram_needed > self._vram_budget_gb * 2:
            return None
        benchmark_score = self._estimate_benchmark_score(candidate)
        sentiment_score = self._estimate_sentiment_score(candidate)
        overall_score = benchmark_score * _BENCHMARK_WEIGHT + sentiment_score * _SENTIMENT_WEIGHT
        improvement = overall_score - best_current_score
        if improvement < self.MIN_IMPROVEMENT_THRESHOLD:
            return None
        formats = self._detect_available_formats(candidate)
        recommended_format = next(
            (fmt for fmt in ("awq", "gptq", "safetensors", "gguf") if fmt in formats), "safetensors"
        )
        return ModelUpgradeCandidate(
            current_model_id=best_current_id,
            candidate_name=candidate_name,
            candidate_repo_id=getattr(candidate, "repo_id", ""),
            benchmark_score=benchmark_score,
            sentiment_score=sentiment_score,
            overall_score=overall_score,
            available_formats=formats,
            recommended_backend=_backend_for_format(recommended_format),
            recommended_format=recommended_format,
            vram_estimate_gb=vram_needed,
            reason=(
                f"Scores {improvement:.0%} higher than {best_current_id} for {task_type} tasks "
                f"(benchmark={benchmark_score:.2f}, sentiment={sentiment_score:.2f})"
            ),
        )

    @staticmethod
    def _estimate_benchmark_score(candidate: Any) -> float:
        """Estimate benchmark quality from candidate metadata.

        Uses available signals: final_score from discovery, parameter count
        heuristics, and any benchmark data in the metadata.

        Args:
            candidate: A ModelCandidate from ModelDiscovery.

        Returns:
            Score between 0.0 and 1.0.
        """
        score = getattr(candidate, "final_score", 0.0)

        # Boost for known high-quality families
        name_lower = (candidate.name or candidate.id or "").lower()
        if any(fam in name_lower for fam in ("qwen3", "qwen2.5", "llama-3.3", "llama-3.1")):
            score = max(score, 0.6)
        if any(fam in name_lower for fam in ("deepseek-v3", "mistral-large")):
            score = max(score, 0.7)

        return min(1.0, score)

    @staticmethod
    def _estimate_sentiment_score(candidate: Any) -> float:
        """Estimate community sentiment from download counts and engagement.

        Args:
            candidate: A ModelCandidate from ModelDiscovery.

        Returns:
            Score between 0.0 and 1.0.
        """
        score = 0.3  # Base score — if we found it, someone uses it

        # Use likes/downloads if available
        likes = getattr(candidate, "likes", 0) or 0
        downloads = getattr(candidate, "downloads", 0) or 0

        if likes > 1000:
            score += 0.3
        elif likes > 100:
            score += 0.15
        elif likes > 10:
            score += 0.05

        if downloads > 100000:
            score += 0.3
        elif downloads > 10000:
            score += 0.15
        elif downloads > 1000:
            score += 0.05

        return min(1.0, score)

    @staticmethod
    def _detect_available_formats(candidate: Any) -> list[str]:
        """Detect which model formats are available for a candidate.

        Args:
            candidate: A ModelCandidate from ModelDiscovery.

        Returns:
            List of available format strings (e.g. ["gguf", "awq"]).
        """
        name_lower = (candidate.name or candidate.id or "").lower()
        metrics = getattr(candidate, "metrics", {}) or {}
        if isinstance(metrics, dict):
            name_lower = f"{name_lower} {' '.join(str(value) for value in metrics.values())}".lower()
        formats = []

        if "safetensors" in name_lower:
            formats.append("safetensors")
        if "gguf" in name_lower:
            formats.append("gguf")
        if "awq" in name_lower:
            formats.append("awq")
        if "gptq" in name_lower:
            formats.append("gptq")

        # Most popular models have GGUF and SafeTensors variants
        if not formats:
            formats = ["safetensors", "gguf"]

        return formats

    def get_cached_upgrades(self) -> list[ModelUpgradeCandidate]:
        """Load upgrade candidates from the last freshness check.

        Returns:
            List of cached upgrade candidates, or empty if no check has run.
        """
        upgrades_file = self._last_check_file.parent / "model_upgrades.json"
        if not upgrades_file.exists():
            return []
        try:
            data = json.loads(upgrades_file.read_text(encoding="utf-8"))
            return [ModelUpgradeCandidate(**entry) for entry in data.get("upgrades", [])]
        except Exception:
            logger.warning("Could not load cached upgrade candidates — returning empty list")
            return []

    def _record_check(self, candidates_found: int, upgrades: list[ModelUpgradeCandidate] | None = None) -> None:
        """Record the freshness check timestamp and any upgrade candidates.

        Writes both the check metadata and the upgrade candidates to disk
        so they can be surfaced by API endpoints or CLI commands.

        Args:
            candidates_found: Number of upgrade candidates found.
            upgrades: The actual upgrade candidates to persist.
        """
        try:
            write_json_atomic(
                self._last_check_file,
                {
                    "last_check": datetime.now(timezone.utc).isoformat(),
                    "candidates_found": candidates_found,
                },
            )

            # Persist upgrade candidates for API/CLI consumption
            if upgrades:
                upgrades_file = self._last_check_file.parent / "model_upgrades.json"
                write_json_atomic(
                    upgrades_file,
                    {
                        "checked_at": datetime.now(timezone.utc).isoformat(),
                        "upgrades": [u.to_dict() for u in upgrades],
                    },
                )
        except Exception:
            logger.warning(
                "Could not write freshness check results — upgrade candidates will not persist between sessions"
            )


__all__ = ["FRESHNESS_CHECK_INTERVAL_DAYS", "ModelFreshnessChecker", "ModelUpgradeCandidate"]
