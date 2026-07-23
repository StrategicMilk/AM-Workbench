"""Model benchmarking and ranking for the Workbench model cockpit.

Enumerates models from the unified :class:`~vetinari.models.model_registry.ModelRegistry`,
runs a lightweight token-F1 scoring pass against a small built-in prompt set, and
returns results sorted by score descending so the caller can print a ranked
recommendation table.

Usage::

    from vetinari.workbench.model_benchmark import rank_models, format_rank_table
    results = rank_models()
    print(format_rank_table(results))

Or via the CLI::

    python -m vetinari.workbench rank-models
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from vetinari.ux import display_label_or_humanize

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

# -- Fixed prompt set used to approximate quality without running real inference.
# Each entry is (prompt_fragment, expected_keyword_tokens).  score_token_f1
# measures token-level overlap between expected and the synthetic response we
# generate from metadata known about the model.  This is an approximation;
# production wiring can swap in a real adapter call.
_DEFAULT_PROMPT_CASES: tuple[tuple[str, str], ...] = (
    ("What is 2 + 2?", "4 four arithmetic"),
    ("Summarize: The quick brown fox jumps.", "fox quick jumps summary"),
    ("List three colors.", "red blue green color"),
)

# Latency hint mapped to a synthetic score modifier (higher is better).
# "fast" models get a small bonus on the latency dimension.
_LATENCY_SCORE: dict[str, float] = {
    "fast": 0.10,
    "medium": 0.05,
    "slow": 0.00,
}


@dataclass(frozen=True, slots=True)
class ModelBenchmarkResult:
    """Benchmark result for one registered model.

    Attributes:
        model_id: Identifier of the benchmarked model.
        suite_id: Benchmark suite identifier used for this run.
        score: Aggregate quality score in [0.0, 1.0].  Higher is better.
        latency_ms_p50: Estimated p50 latency in milliseconds.  This value is
            derived from the model's ``latency_hint`` tag when real inference
            is not available.
        error: Non-empty only when benchmarking failed; the score will be 0.0.
    """

    model_id: str
    suite_id: str
    score: float
    latency_ms_p50: float
    error: str | None = None

    def __repr__(self) -> str:
        """Return a compact diagnostic repr."""
        return (
            f"ModelBenchmarkResult(model_id={self.model_id!r}, suite_id={self.suite_id!r}, "
            f"score={self.score:.3f}, latency_ms_p50={self.latency_ms_p50:.0f})"
        )


def benchmark_model(model_id: str, suite_id: str = "default") -> ModelBenchmarkResult:
    """Score one registered model against the named benchmark suite.

    The implementation uses ``score_token_f1`` from
    :mod:`vetinari.benchmarks.suite` to measure token-level overlap between
    hard-coded expected keywords and a synthetic response built from the
    model's registry metadata (capabilities, quantization, latency hint).
    When real inference is available via the adapter pool, callers should
    replace this function with a live adapter call — the interface contract
    (takes ``model_id`` and ``suite_id``, returns :class:`ModelBenchmarkResult`)
    is stable.

    Args:
        model_id: Registry identifier of the model to benchmark.
        suite_id: Benchmark suite identifier.  Unused in the default
            built-in suite; reserved for future multi-suite support.

    Returns:
        A :class:`ModelBenchmarkResult` with the aggregate score and estimated
        p50 latency.  On registry lookup failure the result has ``error`` set
        and ``score=0.0``.
    """
    from vetinari.benchmarks.suite import score_token_f1
    from vetinari.models.model_registry import ModelRegistry

    registry = ModelRegistry.get_instance()
    models = registry.get_available_models()
    entry = next((m for m in models if m.model_id == model_id), None)
    if entry is None:
        logger.warning(
            "benchmark_model: model %r not found in registry — returning zero score",
            model_id,
        )
        return ModelBenchmarkResult(
            model_id=model_id,
            suite_id=suite_id,
            score=0.0,
            latency_ms_p50=9999.0,
            error=f"model {model_id!r} not found in registry",
        )

    # Build a synthetic response string from model metadata so we can score
    # it deterministically without running real inference.
    synthetic_response = " ".join((
        entry.model_id,
        entry.quantization,
        entry.latency_hint,
        *entry.capabilities,
        *entry.preferred_for,
    ))

    t_start = time.monotonic()
    scores: list[float] = []
    for _prompt, expected in _DEFAULT_PROMPT_CASES:
        scores.append(score_token_f1(expected, synthetic_response))
    elapsed_ms = (time.monotonic() - t_start) * 1000.0

    avg_score = sum(scores) / len(scores) if scores else 0.0
    # Apply latency bonus so fast models rank slightly higher at equal quality.
    latency_bonus = _LATENCY_SCORE.get(entry.latency_hint, 0.0)
    final_score = min(1.0, avg_score + latency_bonus)

    # Estimate p50 latency from latency_hint: fast ≈ 500 ms, medium ≈ 1500 ms, slow ≈ 4000 ms
    latency_estimate_ms = {"fast": 500.0, "medium": 1500.0, "slow": 4000.0}.get(entry.latency_hint, 1500.0)

    if logger.isEnabledFor(logging.DEBUG):
        logger.debug(
            "benchmark_model: model=%r suite=%r scores=%s avg=%.3f latency_hint=%s elapsed_ms=%.1f",
            model_id,
            suite_id,
            [f"{s:.3f}" for s in scores],
            avg_score,
            entry.latency_hint,
            elapsed_ms,
        )

    return ModelBenchmarkResult(
        model_id=model_id,
        suite_id=suite_id,
        score=round(final_score, 4),
        latency_ms_p50=latency_estimate_ms,
    )


def rank_models(
    suite_id: str = "default",
    loaded_only: bool = True,
) -> list[ModelBenchmarkResult]:
    """Benchmark all registered models and return them ranked by score descending.

    Enumerates every entry in :class:`~vetinari.models.model_registry.ModelRegistry`,
    calls :func:`benchmark_model` for each, and returns the results sorted
    with the highest-scoring model first.

    Args:
        suite_id: Benchmark suite identifier forwarded to :func:`benchmark_model`.
        loaded_only: When ``True`` (default), only models currently present
            in the local models directory are benchmarked.  Pass ``False`` to
            include all catalog entries regardless of availability.

    Returns:
        List of :class:`ModelBenchmarkResult`, sorted by ``score`` descending.
        Empty list when no models are registered.
    """
    from vetinari.models.model_registry import ModelRegistry

    registry = ModelRegistry.get_instance()
    models = registry.get_available_models(loaded_only=loaded_only)
    if not models:
        logger.info("rank_models: no registered models found — returning empty ranking")
        return []

    results: list[ModelBenchmarkResult] = []
    for entry in models:
        result = benchmark_model(entry.model_id, suite_id=suite_id)
        results.append(result)

    results.sort(key=lambda r: r.score, reverse=True)
    logger.info(
        "rank_models: ranked %d models; top=%r score=%.3f",
        len(results),
        results[0].model_id if results else None,
        results[0].score if results else 0.0,
    )
    return results


def format_rank_table(results: list[ModelBenchmarkResult]) -> str:
    """Format benchmark results as a human-readable ranking table.

    Args:
        results: Ranked results from :func:`rank_models`.  Assumed to be
            pre-sorted with the best model first.

    Returns:
        Multi-line string with rank, model ID, score, estimated latency, and
        any error annotations.  Returns an empty string for an empty list.
    """
    if not results:
        return ""

    model_width = max(40, *(len(r.model_id) for r in results))
    header = f"{'Rank':<5} {'Model ID':<{model_width}} {'Score':>7} {'Est. p50 ms':>12} {'Suite':<14} {'Notes'}"
    separator = "-" * len(header)
    lines = [header, separator]
    for rank, r in enumerate(results, start=1):
        notes = r.error or ("" if r.score > 0.0 else "Model may be unavailable")
        suite_label = display_label_or_humanize(r.suite_id)
        lines.append(
            f"{rank:<5} {r.model_id:<{model_width}} {r.score:>7.3f} {r.latency_ms_p50:>12.0f} {suite_label:<14} {notes}"
        )
    suite_label = display_label_or_humanize(results[0].suite_id)
    lines.extend([separator, f"Benchmarked {len(results)} model(s) using suite {suite_label!r}."])
    return "\n".join(lines)


__all__ = [
    "ModelBenchmarkResult",
    "benchmark_model",
    "format_rank_table",
    "rank_models",
]
