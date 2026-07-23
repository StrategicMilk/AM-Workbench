"""Benchmark comparison runner for backend tuning candidates."""

from __future__ import annotations

from pathlib import Path

from vetinari.tuning.backend_tuning import (
    BackendTuningConfig,
    BenchmarkMetricSnapshot,
    BenchmarkVerdict,
    evaluate_metric_windows,
    load_backend_tuning_config,
    load_metric_snapshot,
)


def compare_backend_tuning_candidate(
    config: BackendTuningConfig | str | Path,
    baseline: BenchmarkMetricSnapshot | str | Path,
    candidate: BenchmarkMetricSnapshot | str | Path,
) -> BenchmarkVerdict:
    """
    Compare candidate and baseline windows under the configured policy.

    Args:
        config: Config value consumed by compare_backend_tuning_candidate().
        baseline: Baseline value consumed by compare_backend_tuning_candidate().
        candidate: Candidate value consumed by compare_backend_tuning_candidate().

    Returns:
        BenchmarkVerdict value produced by compare_backend_tuning_candidate().
    """

    loaded_config = load_backend_tuning_config(config) if isinstance(config, str | Path) else config
    loaded_baseline = load_metric_snapshot(baseline) if isinstance(baseline, str | Path) else baseline
    loaded_candidate = load_metric_snapshot(candidate) if isinstance(candidate, str | Path) else candidate
    return evaluate_metric_windows(loaded_config, loaded_baseline, loaded_candidate)
