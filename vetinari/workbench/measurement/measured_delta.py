"""Measured delta helpers for Workbench method evidence."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from statistics import fmean

from vetinari.workbench.evals import EvalResult
from vetinari.workbench.measurement.baseline_collector import (
    DEFAULT_MINIMUM_BASELINE_SAMPLES,
    BaselineEvidenceError,
    MeasurementBaseline,
    collect_metric_baselines,
    is_baseline_eval,
)
from vetinari.workbench.measurement.promotion_stats import (
    DEFAULT_MINIMUM_PROMOTION_SAMPLES,
    PromotionStats,
)
from vetinari.workbench.shadow_tester import evaluate_shadow_promotion


class MeasuredDeltaError(ValueError):
    """Raised when measured deltas cannot be computed safely."""


@dataclass(frozen=True, slots=True)
class MetricDelta:
    """Measured method delta against a measured control baseline."""

    metric_name: str
    baseline_value: float
    method_value: float
    delta: float
    sign: str
    evidence_eval_id: str
    captured_at_utc: str
    baseline_sample_count: int
    method_sample_count: int
    baseline_source: str
    baseline_eval_ids: tuple[str, ...]
    p_value: float
    effect_size: float
    minimum_sample_count: int
    promotion_stats: PromotionStats

    def __repr__(self) -> str:
        return (
            "MetricDelta("
            f"metric_name={self.metric_name!r}, sign={self.sign!r}, "
            f"delta={self.delta!r}, method_sample_count={self.method_sample_count}, "
            f"baseline_sample_count={self.baseline_sample_count})"
        )


def summarize_measured_deltas(
    method_evals: Iterable[EvalResult],
    all_evals: Iterable[EvalResult],
    *,
    minimum_sample_count: int = DEFAULT_MINIMUM_PROMOTION_SAMPLES,
    minimum_baseline_samples: int = DEFAULT_MINIMUM_BASELINE_SAMPLES,
) -> tuple[MetricDelta, ...]:
    """Summarize method deltas without using score thresholds as baselines.

    Args:
        method_evals: Candidate method evals to summarize.
        all_evals: Full eval corpus containing measured baseline/control evals.
        minimum_sample_count: Minimum method sample count for promotion stats.
        minimum_baseline_samples: Minimum baseline/control samples per metric.

    Returns:
        Metric deltas for every candidate metric.

    Raises:
        MeasuredDeltaError: if required measured baselines are missing or
            under-sampled.
    """
    method_evals_tuple = tuple(eval_result for eval_result in method_evals if not is_baseline_eval(eval_result))
    if not method_evals_tuple:
        return ()
    try:
        baselines = collect_metric_baselines(all_evals, minimum_sample_count=minimum_baseline_samples)
    except BaselineEvidenceError as exc:
        raise MeasuredDeltaError(str(exc)) from exc
    values: dict[str, list[float]] = defaultdict(list)
    eval_ids: dict[str, list[str]] = defaultdict(list)
    captured: dict[str, list[str]] = defaultdict(list)
    for eval_result in method_evals_tuple:
        for score in eval_result.scores:
            values[score.metric_name].append(float(score.value))
            eval_ids[score.metric_name].append(eval_result.eval_id)
            captured[score.metric_name].append(eval_result.captured_at_utc)
    deltas: list[MetricDelta] = []
    for metric_name, method_values in values.items():
        baseline = baselines.get(metric_name)
        if baseline is None:
            raise MeasuredDeltaError(f"missing measured baseline for metric {metric_name!r}")
        stats = _stats_for_values(
            baseline=baseline,
            method_values=tuple(method_values),
            minimum_sample_count=minimum_sample_count,
        )
        method_value = float(fmean(method_values))
        delta = method_value - baseline.value
        if stats.promotable:
            sign = "positive"
        elif delta < 0 and stats.sample_count >= stats.minimum_sample_count:
            sign = "negative"
        else:
            sign = "neutral"
        deltas.append(
            MetricDelta(
                metric_name=metric_name,
                baseline_value=baseline.value,
                method_value=method_value,
                delta=delta,
                sign=sign,
                evidence_eval_id=",".join(eval_ids[metric_name]),
                captured_at_utc=max(captured[metric_name]),
                baseline_sample_count=baseline.sample_count,
                method_sample_count=len(method_values),
                baseline_source=baseline.source,
                baseline_eval_ids=baseline.source_eval_ids,
                p_value=stats.p_value,
                effect_size=stats.effect_size,
                minimum_sample_count=stats.minimum_sample_count,
                promotion_stats=stats,
            )
        )
    return tuple(deltas)


def _stats_for_values(
    *,
    baseline: MeasurementBaseline,
    method_values: tuple[float, ...],
    minimum_sample_count: int,
) -> PromotionStats:
    return evaluate_shadow_promotion(
        baseline.sample_values,
        method_values,
        minimum_sample_count=minimum_sample_count,
    ).stats


__all__ = ["MeasuredDeltaError", "MetricDelta", "summarize_measured_deltas"]
