"""Benchmark Seeder - Seeds Thompson Sampling priors from capability data.

Provides informed cold-start priors instead of uninformed Beta(1,1)
by mapping model capabilities to task-type affinities.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from vetinari.constants import CACHE_TTL_ONE_WEEK, get_user_dir

logger = logging.getLogger(__name__)


# Capability-to-task-type affinity mapping.
# calibration: internal-heuristic -- no external validation
# Scores represent expected performance 0.0-1.0 for each task type
CAPABILITY_TASK_AFFINITY: dict[str, dict[str, float]] = {
    "code": {
        "coding": 0.85,
        "reasoning": 0.6,
        "data_processing": 0.7,
        "planning": 0.5,
        "documentation": 0.5,
        "creative": 0.3,
        "analysis": 0.6,
        "general": 0.5,
    },
    "reasoning": {
        "coding": 0.5,
        "reasoning": 0.85,
        "data_processing": 0.7,
        "planning": 0.75,
        "documentation": 0.6,
        "creative": 0.5,
        "analysis": 0.8,
        "general": 0.7,
    },
    "chat": {
        "coding": 0.4,
        "reasoning": 0.5,
        "data_processing": 0.4,
        "planning": 0.5,
        "documentation": 0.7,
        "creative": 0.75,
        "analysis": 0.5,
        "general": 0.8,
    },
    "vision": {
        "coding": 0.3,
        "reasoning": 0.4,
        "data_processing": 0.5,
        "planning": 0.3,
        "documentation": 0.4,
        "creative": 0.6,
        "analysis": 0.6,
        "general": 0.5,
    },
    "math": {
        "coding": 0.6,
        "reasoning": 0.8,
        "data_processing": 0.85,
        "planning": 0.5,
        "documentation": 0.3,
        "creative": 0.2,
        "analysis": 0.75,
        "general": 0.4,
    },
}

# Model name pattern → capability hints
MODEL_CAPABILITY_PATTERNS: list[tuple[str, list[str]]] = [
    ("coder", ["code"]),
    ("code", ["code"]),
    ("deepseek-coder", ["code", "reasoning"]),
    ("codellama", ["code"]),
    ("starcoder", ["code"]),
    ("qwen3-coder", ["code", "reasoning"]),
    ("devstral", ["code"]),
    ("math", ["math", "reasoning"]),
    ("wizard-math", ["math"]),
    ("vl", ["vision"]),
    ("vision", ["vision"]),
    ("llava", ["vision"]),
    ("instruct", ["chat", "reasoning"]),
    ("chat", ["chat"]),
    ("uncensored", ["chat", "creative"]),
]

# Size-based quality scaling (smaller models → lower baseline)
SIZE_SCALING: dict[str, float] = {
    "tiny": 0.5,  # < 1B
    "small": 0.65,  # 1-3B
    "medium": 0.75,  # 3-8B
    "large": 0.85,  # 8-30B
    "xlarge": 0.92,  # 30B+
}


@dataclass(frozen=True, slots=True)
class BenchmarkPrior:
    """Prior parameters for a model+task_type Thompson arm."""

    model_id: str
    task_type: str
    alpha: float
    beta: float
    source: str  # "benchmark", "capability", "size", "default"
    confidence: float = 0.5  # How confident we are in this prior

    def __repr__(self) -> str:
        return f"BenchmarkPrior(model_id={self.model_id!r}, task_type={self.task_type!r}, source={self.source!r})"


@dataclass
class BenchmarkCache:
    """Cached benchmark/capability data."""

    priors: dict[str, BenchmarkPrior] = field(default_factory=dict)
    benchmark_scores: dict[str, float] = field(default_factory=dict)
    last_updated: float = 0.0
    ttl_seconds: float = CACHE_TTL_ONE_WEEK  # 7 days

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}("
            f"priors={self.priors!r}, "
            f"benchmark_scores={self.benchmark_scores!r}, "
            f"last_updated={self.last_updated!r}, "
            f"ttl_seconds={self.ttl_seconds!r}"
            ")"
        )


class BenchmarkSeeder:
    """Seeds Thompson Sampling priors from model capabilities and benchmarks."""

    def __init__(self):
        self._cache = BenchmarkCache()
        self._lock = threading.Lock()
        self._load_cache()

    def get_prior(self, model_id: str, task_type: str) -> tuple[float, float]:
        """Get informed prior (alpha, beta) for a model+task_type pair.

        Returns:
            Tuple of (alpha, beta) for Beta distribution initialization.

        Args:
            model_id: The model id.
            task_type: The task type.
        """
        key = f"{model_id}:{task_type}"

        with self._lock:
            # Check cache first
            if key in self._cache.priors:
                p = self._cache.priors[key]
                return p.alpha, p.beta

            # Compute from capabilities and persist while the cache mutation is
            # still guarded so concurrent callers cannot double-compute.
            prior = self._compute_capability_prior(model_id, task_type)
            self._cache.priors[key] = prior
            self._save_cache()
            return prior.alpha, prior.beta

    def seed_model(self, model_id: str, model_metadata: dict[str, Any] | None = None) -> dict[str, tuple[float, float]]:
        """Seed all task-type priors for a model.

        Args:
            model_id: The model identifier.
            model_metadata: Optional metadata with capabilities, context_len, etc.

        Returns:
            Dict mapping task_type → (alpha, beta) priors.
        """
        task_types = [
            "coding",
            "reasoning",
            "data_processing",
            "planning",
            "documentation",
            "creative",
            "analysis",
            "general",
        ]
        priors = {}
        for tt in task_types:
            priors[tt] = self.get_prior(model_id, tt)
        return priors

    def record_measured_benchmark(self, model_id: str, task_type: str, score: float) -> BenchmarkPrior:
        """Record a measured benchmark score and rebuild the cold-start prior.

        Args:
            model_id: Model identifier used for routing or lookup.
            task_type: Task type value consumed by record_measured_benchmark().
            score: Score value evaluated by the operation.

        Returns:
            Value produced for the caller.

        Raises:
            ValueError: Propagated when validation, persistence, or execution fails.
        """
        if not 0.0 <= score <= 1.0:
            raise ValueError("score must be between 0.0 and 1.0")
        key = f"{model_id}:{task_type}"
        with self._lock:
            self._cache.benchmark_scores[key] = float(score)
            prior = self._prior_from_score(model_id, task_type, score, source="benchmark", confidence=0.9)
            self._cache.priors[key] = prior
            self._save_cache()
            return prior

    def _compute_capability_prior(self, model_id: str, task_type: str) -> BenchmarkPrior:
        """Compute prior from measured benchmark scores before name hints."""
        key = f"{model_id}:{task_type}"
        measured_score = self._cache.benchmark_scores.get(key)
        if measured_score is not None:
            return self._prior_from_score(model_id, task_type, measured_score, source="benchmark", confidence=0.9)

        model_lower = model_id.lower()
        task_lower = task_type.lower()

        # Step 1: Detect capabilities from model name
        detected_caps = []
        for pattern, caps in MODEL_CAPABILITY_PATTERNS:
            if pattern in model_lower:
                detected_caps.extend(caps)
        if not detected_caps:
            detected_caps = ["chat"]  # Default assumption

        # Step 2: Aggregate affinity scores across detected capabilities
        affinities = []
        for cap in set(detected_caps):
            cap_map = CAPABILITY_TASK_AFFINITY.get(cap, {})
            aff = cap_map.get(task_lower, 0.5)
            affinities.append(aff)

        avg_affinity = sum(affinities) / len(affinities) if affinities else 0.5

        # Step 3: Scale by estimated model size
        size_scale = self._estimate_size_scale(model_lower)
        scaled_affinity = avg_affinity * size_scale

        # Step 4: Convert to Beta distribution parameters
        # Higher affinity → higher alpha, lower beta
        # Use moderate prior strength (equivalent to ~10 observations)
        return self._prior_from_score(
            model_id,
            task_type,
            scaled_affinity,
            source="capability_name_fallback",
            confidence=0.6 if detected_caps != ["chat"] else 0.3,
        )

    @staticmethod
    def _prior_from_score(
        model_id: str,
        task_type: str,
        score: float,
        *,
        source: str,
        confidence: float,
    ) -> BenchmarkPrior:
        """Convert a measured or fallback score into Beta prior parameters."""
        prior_strength = 10
        alpha = score * prior_strength + 1
        beta = (1 - score) * prior_strength + 1
        return BenchmarkPrior(
            model_id=model_id,
            task_type=task_type,
            alpha=round(alpha, 2),
            beta=round(beta, 2),
            source=source,
            confidence=confidence,
        )

    @staticmethod
    def _estimate_size_scale(model_lower: str) -> float:
        """Estimate quality scaling from model name size hints."""
        # Try to extract parameter count from model name
        size_match = re.search(r"(\d+\.?\d*)[bB]", model_lower)
        if size_match:
            params_b = float(size_match.group(1))
            if params_b < 1:
                return SIZE_SCALING["tiny"]
            if params_b < 3:
                return SIZE_SCALING["small"]
            if params_b < 8:
                return SIZE_SCALING["medium"]
            if params_b < 30:
                return SIZE_SCALING["large"]
            return SIZE_SCALING["xlarge"]
        # No size info — assume medium
        return SIZE_SCALING["medium"]

    def _load_cache(self) -> None:
        try:
            cache_file = get_user_dir() / "benchmark_cache.json"
            if cache_file.exists():
                with cache_file.open(encoding="utf-8") as f:
                    data = json.load(f)
                self._cache.last_updated = data.get("last_updated", 0.0)
                self._cache.benchmark_scores = {
                    str(key): float(value) for key, value in data.get("benchmark_scores", {}).items()
                }
                for key, p in data.get("priors", {}).items():
                    self._cache.priors[key] = BenchmarkPrior(**p)
                logger.debug("[BenchmarkSeeder] Loaded %s cached priors", len(self._cache.priors))
        except Exception as e:
            logger.warning("[BenchmarkSeeder] Could not load benchmark cache — starting fresh: %s", e)

    def _save_cache(self) -> None:
        try:
            state_dir = get_user_dir()
            state_dir.mkdir(parents=True, exist_ok=True)
            cache_file = state_dir / "benchmark_cache.json"
            data = {
                "last_updated": time.time(),
                "benchmark_scores": self._cache.benchmark_scores,
                "priors": {k: asdict(v) for k, v in self._cache.priors.items()},
            }
            with cache_file.open("w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.warning("[BenchmarkSeeder] Could not save benchmark cache: %s", e)


# Singleton
_benchmark_seeder: BenchmarkSeeder | None = None
_benchmark_seeder_lock = threading.Lock()


def get_benchmark_seeder() -> BenchmarkSeeder:
    """Get or create the module-level BenchmarkSeeder singleton.

    Uses double-checked locking to ensure thread-safe lazy initialization.
    Call ``reset_benchmark_seeder()`` when ``VETINARI_USER_DIR`` changes
    (e.g., in tests) so the next call re-creates the instance against the
    new path.

    Returns:
        The shared BenchmarkSeeder instance.
    """
    global _benchmark_seeder
    if _benchmark_seeder is None:
        with _benchmark_seeder_lock:
            if _benchmark_seeder is None:
                _benchmark_seeder = BenchmarkSeeder()
    return _benchmark_seeder


def reset_benchmark_seeder() -> None:
    """Discard the cached BenchmarkSeeder singleton.

    The next call to ``get_benchmark_seeder()`` will re-create the instance,
    which re-resolves ``get_user_dir()`` so tests that change
    ``VETINARI_USER_DIR`` between calls get a fresh instance pointing at the
    correct path rather than the one from construction time.
    """
    global _benchmark_seeder
    with _benchmark_seeder_lock:
        _benchmark_seeder = None
