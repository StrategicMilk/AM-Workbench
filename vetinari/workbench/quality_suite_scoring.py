"""Private parsing and scoring helpers for the Workbench quality suite."""

from __future__ import annotations

from typing import Any


def _suite_from_mapping(payload: dict[str, Any]) -> Any:
    from vetinari.workbench.quality_suite import (
        _READINESS_ORDER,
        DEFAULT_BENCHMARK_SOURCES_PATH,
        QualitySuiteError,
        ReadinessLevel,
        WorkbenchQualitySuite,
        load_benchmark_source_catalog,
    )

    schema_version = int(payload.get("schema_version", 0))
    if schema_version != 1:
        raise QualitySuiteError("quality suite schema_version must be 1")
    readiness_levels = tuple(
        ReadinessLevel(name=str(name), minimum_score=float(config["minimum_score"]))
        for name, config in dict(payload.get("readiness_levels", {})).items()
    )
    if tuple(level.name for level in readiness_levels) != _READINESS_ORDER:
        raise QualitySuiteError("readiness_levels must be beta, pro, team, enterprise_signal in order")
    categories = tuple(
        _category_from_mapping(str(category_id), dict(config))
        for category_id, config in dict(payload.get("categories", {})).items()
    )
    if not categories:
        raise QualitySuiteError("quality suite must define categories")
    _validate_weight_sum("category weights", (category.weight for category in categories))
    benchmark_sources_path = payload.get("external_benchmark_sources_path", DEFAULT_BENCHMARK_SOURCES_PATH)
    return WorkbenchQualitySuite(
        schema_version=schema_version,
        suite_id=_required_str(payload, "suite_id"),
        description=_required_str(payload, "description"),
        readiness_levels=readiness_levels,
        categories=categories,
        benchmark_sources=load_benchmark_source_catalog(benchmark_sources_path),
    )


def _category_from_mapping(category_id: str, payload: dict[str, Any]) -> Any:
    from vetinari.workbench.quality_suite import CategorySpec, QualitySuiteError

    metrics = tuple(
        _metric_from_mapping(category_id, str(metric_id), dict(config))
        for metric_id, config in dict(payload.get("metrics", {})).items()
    )
    if not metrics:
        raise QualitySuiteError(f"category {category_id} must define metrics")
    _validate_weight_sum(f"{category_id} metric weights", (metric.weight for metric in metrics))
    return CategorySpec(
        category_id=category_id,
        weight=float(payload["weight"]),
        claim=_required_str(payload, "claim"),
        metrics=metrics,
    )


def _metric_from_mapping(category_id: str, metric_id: str, payload: dict[str, Any]) -> Any:
    from vetinari.workbench.quality_suite import _READINESS_ORDER, MetricDirection, MetricSpec, QualitySuiteError

    gate = payload.get("gate")
    if gate is not None and str(gate) not in _READINESS_ORDER:
        raise QualitySuiteError(f"metric {metric_id} has unknown gate {gate}")
    direction = MetricDirection(str(payload["direction"]))
    zero_score_at = float(payload["zero_score_at"])
    target = float(payload["target"])
    excellent = float(payload["excellent"])
    if direction is MetricDirection.GTE and not zero_score_at <= target <= excellent:
        raise QualitySuiteError(f"metric {metric_id} must satisfy zero_score_at <= target <= excellent")
    if direction is MetricDirection.LTE and not zero_score_at >= target >= excellent:
        raise QualitySuiteError(f"metric {metric_id} must satisfy zero_score_at >= target >= excellent")
    return MetricSpec(
        metric_id=metric_id,
        category_id=category_id,
        weight=float(payload["weight"]),
        direction=direction,
        zero_score_at=zero_score_at,
        target=target,
        excellent=excellent,
        unit=_required_str(payload, "unit"),
        claim=_required_str(payload, "claim"),
        false_green_risk=_required_str(payload, "false_green_risk"),
        gate=str(gate) if gate is not None else None,
    )


def _score_metric(metric: Any, observation: Any) -> Any:
    from vetinari.workbench.quality_suite import MetricDirection, MetricScore, ObservationStatus

    if observation.status is not ObservationStatus.MEASURED:
        return MetricScore(
            metric.metric_id,
            metric.category_id,
            observation.value,
            0.0,
            metric.target,
            False,
            observation.status,
            metric.gate,
            observation.status.value,
        )
    if observation.value is None:
        return MetricScore(
            metric.metric_id,
            metric.category_id,
            None,
            0.0,
            metric.target,
            False,
            ObservationStatus.MISSING,
            metric.gate,
            "missing",
        )
    provenance_reason = _observation_provenance_gap(observation)
    if provenance_reason:
        return MetricScore(
            metric.metric_id,
            metric.category_id,
            observation.value,
            0.0,
            metric.target,
            False,
            ObservationStatus.UNKNOWN,
            metric.gate,
            provenance_reason,
        )
    value = float(observation.value)
    if metric.direction is MetricDirection.GTE:
        score = _score_higher_is_better(value, metric.zero_score_at, metric.target, metric.excellent)
        passed = value >= metric.target
    else:
        score = _score_lower_is_better(value, metric.zero_score_at, metric.target, metric.excellent)
        passed = value <= metric.target
    return MetricScore(
        metric.metric_id,
        metric.category_id,
        value,
        score,
        metric.target,
        passed,
        ObservationStatus.MEASURED,
        metric.gate,
        "passed" if passed else "below-target",
    )


def _score_higher_is_better(value: float, zero_score_at: float, target: float, excellent: float) -> float:
    if value <= zero_score_at:
        return 0.0
    if value >= excellent:
        return 100.0
    if value < target:
        return 80.0 * ((value - zero_score_at) / (target - zero_score_at))
    return 100.0 if excellent == target else 80.0 + 20.0 * ((value - target) / (excellent - target))


def _score_lower_is_better(value: float, zero_score_at: float, target: float, excellent: float) -> float:
    if value >= zero_score_at:
        return 0.0
    if value <= excellent:
        return 100.0
    if value > target:
        return 80.0 * ((zero_score_at - value) / (zero_score_at - target))
    return 100.0 if excellent == target else 80.0 + 20.0 * ((target - value) / (target - excellent))


def _readiness_level(overall_score: float, readiness_levels: tuple[Any, ...], blocking_gates: list[str]) -> str:
    from vetinari.workbench.quality_suite import _READINESS_ORDER

    blocked_by_level = {gate.split(":", 1)[0] for gate in blocking_gates}
    achieved = "not_ready"
    for level in readiness_levels:
        if overall_score < level.minimum_score:
            continue
        if any(_READINESS_ORDER.index(blocked) <= _READINESS_ORDER.index(level.name) for blocked in blocked_by_level):
            continue
        achieved = level.name
    return achieved


def _weighted_mean(values: Any) -> float:
    from vetinari.workbench.quality_suite import QualitySuiteError

    pairs = list(values)
    total_weight = sum(float(weight) for _, weight in pairs)
    if total_weight <= 0:
        raise QualitySuiteError("weights must sum to a positive value")
    return sum(float(value) * float(weight) for value, weight in pairs) / total_weight


def _metric_by_id(category: Any, metric_id: str) -> Any:
    from vetinari.workbench.quality_suite import QualitySuiteError

    for metric in category.metrics:
        if metric.metric_id == metric_id:
            return metric
    raise QualitySuiteError(f"metric not found in category: {metric_id}")


def _observation_from_value(value: Any) -> Any:
    from vetinari.workbench.quality_suite import ObservationStatus, QualityMetricObservation, QualitySuiteError

    if isinstance(value, QualityMetricObservation):
        return value
    if isinstance(value, int | float):
        return QualityMetricObservation(value=float(value))
    if isinstance(value, dict):
        status = ObservationStatus(str(value.get("status", ObservationStatus.MEASURED.value)))
        raw_value = value.get("value")
        return QualityMetricObservation(
            value=float(raw_value) if raw_value is not None else None,
            status=status,
            sample_size=int(value["sample_size"]) if value.get("sample_size") is not None else None,
            captured_at_utc=str(value["captured_at_utc"]) if value.get("captured_at_utc") is not None else None,
            evidence_ref=str(value.get("evidence_ref", "")),
            lineage_ref=str(value.get("lineage_ref", "")),
            note=str(value.get("note", "")),
        )
    raise QualitySuiteError(f"unsupported observation value: {value!r}")


def _observation_provenance_gap(observation: Any) -> str:
    if observation.sample_size is None or observation.sample_size < 1:
        return "sample-size-missing"
    if not observation.captured_at_utc:
        return "captured-at-missing"
    if not observation.evidence_ref.strip():
        return "evidence-ref-missing"
    if not observation.lineage_ref.strip():
        return "lineage-ref-missing"
    return ""


def _required_str(payload: dict[str, Any], key: str) -> str:
    from vetinari.workbench.quality_suite import QualitySuiteError

    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise QualitySuiteError(f"{key} must be a non-empty string")
    return value


def _validate_weight_sum(label: str, weights: Any) -> None:
    from vetinari.workbench.quality_suite import QualitySuiteError

    total = sum(float(weight) for weight in weights)
    if not 0.999 <= total <= 1.001:
        raise QualitySuiteError(f"{label} must sum to 1.0, got {total:.4f}")
