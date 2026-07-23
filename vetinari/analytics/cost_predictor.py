"""Predictive Cost Modeling — vetinari.analytics.cost_predictor.

Estimates task execution cost (tokens, latency, USD) before the task runs,
using a lightweight heuristic regression that calibrates itself over time as
actual outcomes are recorded.

Architecture
------------
- A pure-Python heuristic (no scikit-learn) computes token estimates from
  task_type base values, complexity multipliers, and scope multipliers.
- Actual outcome records are stored in memory.  Once 50+ records exist for a
  task_type the estimator switches to a least-squares calibrated model.
- Confidence is proportional to the number of records: ``min(1.0, n / 50)``.

Usage
-----
    from vetinari.analytics.cost_predictor import CostPredictor

    predictor = CostPredictor()
    estimate = predictor.predict("coding", complexity=2.0, scope_size=500,
                                 model="claude-sonnet")
    cost, conf = estimate.cost_usd, estimate.confidence

    predictor.record_actual("coding", 2.0, 500, "claude-sonnet",
                             actual_tokens=4200, actual_latency=18.5,
                             actual_cost=0.021)
"""

from __future__ import annotations

import logging
import math
import threading
from collections import deque
from dataclasses import dataclass
from typing import Any

from vetinari.analytics.cost_models import _DEFAULT_PRICING

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Base token counts per task_type (heuristic starting point).
# Values represent a "typical" unit of work at complexity=1, scope_size=100.
_BASE_TOKENS: dict[str, int] = {
    "coding": 2000,
    "analysis": 1500,
    "planning": 1200,
    "review": 1000,
    "search": 500,
    "summarisation": 800,
    "default": 1000,
}

_MODEL_COST_ALIAS_PARTS: dict[tuple[str, ...], tuple[str, ...]] = {
    ("claude", "opus"): ("claude", "opus", "4", "8"),
    ("claude", "sonnet"): ("claude", "sonnet", "4", "6"),
    ("claude", "haiku"): ("claude", "haiku", "4", "5", "20251001"),
    ("default",): ("claude", "sonnet", "4", "6"),
}


def _model_key(parts: tuple[str, ...]) -> str:
    return "-".join(parts)


def _average_token_price_usd(model_key: str) -> float:
    pricing = _DEFAULT_PRICING[model_key]
    return (pricing.input_per_1k + pricing.output_per_1k) / 2_000


def _build_per_token_costs() -> dict[str, float]:
    costs = {key.split(":", 1)[1]: _average_token_price_usd(key) for key in _DEFAULT_PRICING}
    costs.update({_model_key(alias): costs[_model_key(target)] for alias, target in _MODEL_COST_ALIAS_PARTS.items()})
    return costs


# Per-token cost in USD, derived from the versioned cost model registry.
_PER_TOKEN_COST: dict[str, float] = _build_per_token_costs()

# Approximate throughput in tokens per second for each model tier.
_TOKENS_PER_SECOND: dict[str, float] = {
    "claude-opus": 60.0,
    "claude-opus-4-8": 60.0,
    "claude-opus-4-7": 60.0,
    "claude-sonnet": 80.0,
    "claude-sonnet-4-6": 80.0,
    "claude-haiku": 120.0,
    "claude-haiku-4-5-20251001": 120.0,
    "gpt-4o": 80.0,
    "gpt-4o-mini": 150.0,
    "o3-mini": 90.0,
    "gemini-3.5-flash": 130.0,
    "gemini-3.1-flash-lite": 180.0,
    "*": 80.0,
    "default": 80.0,
}

# Minimum records needed before switching from pure heuristic to calibrated OLS.
_CALIBRATION_THRESHOLD: int = 50

# Scope normalisation denominator — "100 units of scope" is the baseline.
_SCOPE_BASE: float = 100.0


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------


@dataclass
class CostEstimate:
    """Predicted cost for a task before it executes.

    Attributes:
        tokens: Estimated total token usage.
        latency_seconds: Estimated wall-clock time in seconds.
        cost_usd: Estimated cost in US dollars.
        confidence: Confidence score in [0, 1]; higher means more historical
            data is available for the given task_type.
    """

    tokens: int
    latency_seconds: float
    cost_usd: float
    confidence: float

    def __repr__(self) -> str:
        return f"CostEstimate(tokens={self.tokens!r}, cost_usd={self.cost_usd:.6f}, confidence={self.confidence:.2f})"


# ---------------------------------------------------------------------------
# Internal record type (stored as plain dict for zero-dependency serialisation)
# ---------------------------------------------------------------------------

_Record = dict[str, Any]  # keys: task_type, complexity, scope_size, model, tokens, latency, cost


# ---------------------------------------------------------------------------
# OLS helper (reuse pattern from forecasting.py)
# ---------------------------------------------------------------------------


def _ols_fit(xs: list[float], ys: list[float]) -> tuple[float, float]:
    """Fit a simple OLS line y = slope * x + intercept.

    Args:
        xs: Predictor values (one-dimensional).
        ys: Response values.

    Returns:
        Tuple of (slope, intercept).
    """
    n = len(xs)
    if n < 2:
        return 0.0, (ys[0] if ys else 0.0)
    sx = sum(xs)
    sy = sum(ys)
    sx2 = sum(x * x for x in xs)
    sxy = sum(x * y for x, y in zip(xs, ys))
    denom = n * sx2 - sx * sx
    if denom == 0.0:
        return 0.0, sy / n
    slope = (n * sxy - sx * sy) / denom
    intercept = (sy - slope * sx) / n
    return slope, intercept


def _complexity_factor(complexity: float) -> float:
    """Map a complexity score to a token multiplier.

    Uses a mild power law so doubling complexity doesn't double cost linearly.

    Args:
        complexity: Raw complexity value, typically in [0.5, 5.0].

    Returns:
        Multiplicative factor >= 0.5.
    """
    return max(0.5, math.pow(complexity, 1.2))


def _scope_factor(scope_size: int) -> float:
    """Map a scope size to a token multiplier relative to the 100-unit baseline.

    Args:
        scope_size: Size of the task scope (e.g. lines of code, number of files).

    Returns:
        Multiplicative factor >= 0.1.
    """
    return max(0.1, scope_size / _SCOPE_BASE)


def reset_cost_predictor_records() -> None:
    """Compatibility reset for callers that reset analytics singletons.

    Calibration records are owned by each ``CostPredictor`` instance; production
    reuse happens through ``vetinari.analytics.wiring``'s singleton reference.
    """
    return None


def _resolve_model_key(model: str, table: dict[str, float]) -> str:
    """Resolve provider-qualified model IDs to pricing/throughput keys.

    Examples:
        ``anthropic:claude-sonnet-4-6`` resolves to ``claude-sonnet-4-6``.
        ``anthropic/claude-sonnet-4-6`` resolves to ``claude-sonnet-4-6``.
    """
    if model in table:
        return model
    _, _, unqualified = model.partition(":")
    if unqualified in table:
        return unqualified
    slash_unqualified = model.rsplit("/", 1)[-1]
    if slash_unqualified in table:
        return slash_unqualified
    if model.startswith("local:") and "*" in table:
        return "*"
    return "default"


# ---------------------------------------------------------------------------
# Main predictor class
# ---------------------------------------------------------------------------


class CostPredictor:
    """Predict task execution cost before the task runs.

    Uses a heuristic regression that calibrates itself with actual outcomes
    recorded via :meth:`record_actual`.  Switches from pure-heuristic to an
    OLS-calibrated model once 50+ records are available for a given task_type.

    The predictor is thread-safe; all mutations are protected by an internal
    lock.

    Example::

        predictor = CostPredictor()
        est = predictor.predict("coding", complexity=2.0,
                                scope_size=300, model="claude-sonnet")
        logger.info("Estimated cost: $%s, confidence: %s", est.cost_usd, est.confidence)
    """

    def __init__(self, records: deque[_Record] | None = None) -> None:
        """Initialise the predictor with an instance-local calibration store."""
        self._records: deque[_Record] = records if records is not None else deque(maxlen=1000)
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def predict(
        self,
        task_type: str,
        complexity: float,
        scope_size: int,
        model: str = "default",
    ) -> CostEstimate:
        """Estimate cost for a task before it executes.

        Args:
            task_type: Category of task (e.g. ``"coding"``, ``"analysis"``).
                Falls back to ``"default"`` when unknown.
            complexity: Abstract complexity score, typically in [0.5, 5.0].
                A score of 1.0 represents a baseline unit of work.
            scope_size: Size of the task scope (e.g. lines of code, number of
                files, number of documents).  The baseline is 100 units.
            model: Model identifier used for per-token cost and throughput
                lookups.  Falls back to ``"default"`` when unknown.

        Returns:
            A :class:`CostEstimate` with predicted tokens, latency, cost, and
            confidence.
        """
        with self._lock:
            records_for_type = [r for r in self._records if r["task_type"] == task_type]
            record_count = len(records_for_type)

        token_estimate = self._estimate_tokens(task_type, complexity, scope_size, records_for_type)
        cost_key = _resolve_model_key(model, _PER_TOKEN_COST)
        tps_key = _resolve_model_key(model, _TOKENS_PER_SECOND)
        per_token = _PER_TOKEN_COST[cost_key]
        tps = _TOKENS_PER_SECOND[tps_key]

        cost_usd = token_estimate * per_token
        latency_seconds = token_estimate / tps
        confidence = min(1.0, record_count / _CALIBRATION_THRESHOLD)

        logger.debug(
            "predict task_type=%s complexity=%s scope=%s model=%s -> tokens=%d cost=%.6f confidence=%.2f",
            task_type,
            complexity,
            scope_size,
            model,
            token_estimate,
            cost_usd,
            confidence,
        )

        return CostEstimate(
            tokens=token_estimate,
            latency_seconds=latency_seconds,
            cost_usd=cost_usd,
            confidence=confidence,
        )

    def record_actual(
        self,
        task_type: str,
        complexity: float,
        scope_size: int,
        model: str,
        actual_tokens: int,
        actual_latency: float,
        actual_cost: float,
    ) -> None:
        """Store an actual outcome for future calibration.

        Args:
            task_type: Category of the task that was executed.
            complexity: Complexity score used when the task was predicted.
            scope_size: Scope size used when the task was predicted.
            model: Model that executed the task.
            actual_tokens: Token count observed after execution.
            actual_latency: Wall-clock latency in seconds observed after
                execution.
            actual_cost: Actual cost in USD observed after execution.
        """
        self._validate_actual_record(
            actual_tokens=actual_tokens,
            actual_latency=actual_latency,
            actual_cost=actual_cost,
            complexity=complexity,
            scope_size=scope_size,
        )
        record: _Record = {
            "task_type": task_type,
            "complexity": float(complexity),
            "scope_size": int(scope_size),
            "model": model,
            "tokens": int(actual_tokens),
            "latency": float(actual_latency),
            "cost": float(actual_cost),
        }
        with self._lock:
            self._records.append(record)

        logger.debug(
            "record_actual task_type=%s tokens=%d latency=%.2f cost=%.6f (total records=%d)",
            task_type,
            actual_tokens,
            actual_latency,
            actual_cost,
            len(self._records),
        )

    def record_count(self, task_type: str | None = None) -> int:
        """Return the number of stored outcome records.

        Args:
            task_type: When provided, count only records for this task type.
                When ``None``, count all records.

        Returns:
            Integer count of matching records.
        """
        with self._lock:
            if task_type is None:
                return len(self._records)
            return sum(1 for r in self._records if r["task_type"] == task_type)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_actual_record(
        *,
        actual_tokens: int,
        actual_latency: float,
        actual_cost: float,
        complexity: float,
        scope_size: int,
    ) -> None:
        """Reject malformed actual outcomes before they affect calibration."""
        if actual_tokens <= 0:
            raise ValueError("actual_tokens must be positive")
        if actual_latency <= 0.0 or not math.isfinite(actual_latency):
            raise ValueError("actual_latency must be a positive finite value")
        if actual_cost < 0.0 or not math.isfinite(actual_cost):
            raise ValueError("actual_cost must be a non-negative finite value")
        if complexity <= 0.0 or not math.isfinite(complexity):
            raise ValueError("complexity must be a positive finite value")
        if scope_size <= 0:
            raise ValueError("scope_size must be positive")

    def _estimate_tokens(
        self,
        task_type: str,
        complexity: float,
        scope_size: int,
        records_for_type: list[_Record],
    ) -> int:
        """Compute token estimate using heuristic or calibrated OLS.

        Switches to OLS calibration when sufficient records exist.

        Args:
            task_type: Task category for base-token lookup.
            complexity: Complexity score.
            scope_size: Scope size.
            records_for_type: All stored records for this task_type.

        Returns:
            Estimated token count (always >= 1).
        """
        if len(records_for_type) >= _CALIBRATION_THRESHOLD:
            return self._ols_estimate(complexity, scope_size, records_for_type)
        return self._heuristic_estimate(task_type, complexity, scope_size)

    @staticmethod
    def _heuristic_estimate(
        task_type: str,
        complexity: float,
        scope_size: int,
    ) -> int:
        """Pure-heuristic token estimate.

        Formula: ``base_tokens * complexity_factor * scope_factor``.

        Args:
            task_type: Task category.
            complexity: Complexity score.
            scope_size: Scope size.

        Returns:
            Estimated token count (always >= 1).
        """
        base = _BASE_TOKENS.get(task_type, _BASE_TOKENS["default"])
        tokens = base * _complexity_factor(complexity) * _scope_factor(scope_size)
        return max(1, round(tokens))

    @staticmethod
    def _ols_estimate(
        complexity: float,
        scope_size: int,
        records: list[_Record],
    ) -> int:
        """OLS-calibrated token estimate using a composite predictor.

        Fits a line between the composite heuristic score and actual tokens,
        then applies it to the current inputs.

        Args:
            complexity: Complexity score for the new prediction.
            scope_size: Scope size for the new prediction.
            records: Historical records for this task_type (>= 50 required).

        Returns:
            Calibrated token estimate (always >= 1).
        """
        # Build xs as the heuristic predictor value for each record so that
        # the OLS learns a correction factor over the baseline heuristic.
        xs = [_complexity_factor(r["complexity"]) * _scope_factor(r["scope_size"]) for r in records]
        ys = [float(r["tokens"]) for r in records]

        slope, intercept = _ols_fit(xs, ys)
        x_new = _complexity_factor(complexity) * _scope_factor(scope_size)
        predicted = slope * x_new + intercept

        return max(1, round(predicted))
