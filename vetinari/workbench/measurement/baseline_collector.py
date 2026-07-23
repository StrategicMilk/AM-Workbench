"""Measured baseline collection for Workbench evaluation records."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from statistics import fmean

from vetinari.workbench.evals import EvalResult

DEFAULT_MINIMUM_BASELINE_SAMPLES = 3


class BaselineEvidenceError(ValueError):
    """Raised when a measured baseline is missing or under-sampled."""


@dataclass(frozen=True, slots=True)
class MeasurementBaseline:
    """Measured control baseline for one metric."""

    metric_name: str
    value: float
    sample_count: int
    source_eval_ids: tuple[str, ...]
    sample_values: tuple[float, ...]
    source: str
    captured_at_utc: str

    def __repr__(self) -> str:
        return (
            "MeasurementBaseline("
            f"metric_name={self.metric_name!r}, value={self.value!r}, "
            f"sample_count={self.sample_count}, source={self.source!r})"
        )


def is_baseline_eval(eval_result: EvalResult) -> bool:
    """Return true when an eval result is explicitly marked as control evidence.

    Returns:
        True when the run id, asset id, or notes identify the eval as baseline
        or control evidence.
    """
    markers = (
        eval_result.run_id,
        eval_result.asset_id,
        eval_result.notes,
    )
    return any("baseline" in value.lower() or "control" in value.lower() for value in markers)


def collect_metric_baselines(
    evals: Iterable[EvalResult],
    *,
    minimum_sample_count: int = DEFAULT_MINIMUM_BASELINE_SAMPLES,
) -> dict[str, MeasurementBaseline]:
    """Collect measured control baselines by metric name.

    Returns:
        Baseline records keyed by metric name.

    Raises:
        BaselineEvidenceError: if no baseline evals are present or any metric is
            under the minimum sample count.
    """
    if minimum_sample_count < 1:
        raise ValueError("minimum_sample_count must be at least 1")
    values: dict[str, list[float]] = defaultdict(list)
    eval_ids: dict[str, list[str]] = defaultdict(list)
    captured: dict[str, list[str]] = defaultdict(list)
    for eval_result in evals:
        if not is_baseline_eval(eval_result):
            continue
        for score in eval_result.scores:
            values[score.metric_name].append(float(score.value))
            eval_ids[score.metric_name].append(eval_result.eval_id)
            captured[score.metric_name].append(eval_result.captured_at_utc)
    if not values:
        raise BaselineEvidenceError("no measured baseline evals available")
    baselines: dict[str, MeasurementBaseline] = {}
    under_sampled: list[str] = []
    for metric_name, metric_values in values.items():
        if len(metric_values) < minimum_sample_count:
            under_sampled.append(f"{metric_name}:{len(metric_values)}")
            continue
        baselines[metric_name] = MeasurementBaseline(
            metric_name=metric_name,
            value=float(fmean(metric_values)),
            sample_count=len(metric_values),
            source_eval_ids=tuple(eval_ids[metric_name]),
            sample_values=tuple(float(value) for value in metric_values),
            source="measured_control_eval",
            captured_at_utc=max(captured[metric_name]),
        )
    if under_sampled:
        raise BaselineEvidenceError(
            f"baseline metric sample counts below minimum {minimum_sample_count}: {', '.join(sorted(under_sampled))}"
        )
    return baselines


__all__ = [
    "DEFAULT_MINIMUM_BASELINE_SAMPLES",
    "BaselineEvidenceError",
    "MeasurementBaseline",
    "collect_metric_baselines",
    "is_baseline_eval",
]
