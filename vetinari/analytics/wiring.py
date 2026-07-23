"""Analytics pipeline wiring for execution and observability feedback loops."""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING, Any

from vetinari.analytics import wiring_operations as _ops

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from vetinari.analytics.anomaly import AnomalyDetector
    from vetinari.analytics.cost import CostTracker
    from vetinari.analytics.cost_predictor import CostPredictor
    from vetinari.analytics.failure_registry import FailureRegistry
    from vetinari.analytics.forecasting import Forecaster
    from vetinari.analytics.isolation_forest import HardwareAnomalyDetector, PrimaryAnomalyDetector
    from vetinari.analytics.quality_drift import QualityDriftDetector
    from vetinari.analytics.sla import SLATracker
    from vetinari.analytics.value_stream import ValueStreamAnalyzer

_cost_tracker = None
_sla_tracker = None
_anomaly_detector = None
_forecaster = None
_drift_ensemble = None
_value_stream = None
_cost_predictor = None
_primary_detector = None
_hardware_detector = None
_failure_registry = None

_lock = threading.Lock()


def _lazy_cost_tracker() -> CostTracker | None:
    global _cost_tracker
    if _cost_tracker is None:
        with _lock:
            if _cost_tracker is None:
                from vetinari.analytics.cost import get_cost_tracker

                _cost_tracker = get_cost_tracker()
    return _cost_tracker


def _lazy_sla_tracker() -> SLATracker | None:
    global _sla_tracker
    if _sla_tracker is None:
        with _lock:
            if _sla_tracker is None:
                from vetinari.analytics.sla import get_sla_tracker, register_default_slos

                _sla_tracker = get_sla_tracker()
                register_default_slos()
    return _sla_tracker


def _lazy_anomaly_detector() -> AnomalyDetector | None:
    global _anomaly_detector
    if _anomaly_detector is None:
        with _lock:
            if _anomaly_detector is None:
                from vetinari.analytics.anomaly import get_anomaly_detector

                _anomaly_detector = get_anomaly_detector()
    return _anomaly_detector


def _lazy_forecaster() -> Forecaster | None:
    global _forecaster
    if _forecaster is None:
        with _lock:
            if _forecaster is None:
                from vetinari.analytics.forecasting import get_forecaster

                _forecaster = get_forecaster()
    return _forecaster


def _lazy_drift_ensemble() -> QualityDriftDetector | None:
    global _drift_ensemble
    if _drift_ensemble is None:
        with _lock:
            if _drift_ensemble is None:
                from vetinari.analytics.quality_drift import get_drift_ensemble

                _drift_ensemble = get_drift_ensemble()
    return _drift_ensemble


def _lazy_value_stream() -> ValueStreamAnalyzer | None:
    global _value_stream
    if _value_stream is None:
        with _lock:
            if _value_stream is None:
                from vetinari.analytics.value_stream import get_value_stream_analyzer

                _value_stream = get_value_stream_analyzer()
    return _value_stream


def _lazy_cost_predictor() -> CostPredictor | None:
    global _cost_predictor
    if _cost_predictor is None:
        with _lock:
            if _cost_predictor is None:
                from vetinari.analytics.cost_predictor import CostPredictor

                _cost_predictor = CostPredictor()
    return _cost_predictor


def _lazy_primary_detector() -> PrimaryAnomalyDetector | None:
    global _primary_detector
    if _primary_detector is None:
        with _lock:
            if _primary_detector is None:
                from vetinari.analytics.isolation_forest import PrimaryAnomalyDetector

                _primary_detector = PrimaryAnomalyDetector()
    return _primary_detector


def _lazy_hardware_detector() -> HardwareAnomalyDetector | None:
    global _hardware_detector
    if _hardware_detector is None:
        with _lock:
            if _hardware_detector is None:
                from vetinari.analytics.isolation_forest import HardwareAnomalyDetector

                _hardware_detector = HardwareAnomalyDetector()
    return _hardware_detector


def _lazy_failure_registry() -> FailureRegistry | None:
    global _failure_registry
    if _failure_registry is None:
        with _lock:
            if _failure_registry is None:
                from vetinari.analytics.failure_registry import get_failure_registry

                _failure_registry = get_failure_registry()
    return _failure_registry


def record_inference_cost(
    agent_type: str,
    task_id: str,
    provider: str,
    model_id: str,
    input_tokens: int,
    output_tokens: int,
    latency_ms: float,
    project_id: str | None = None,
) -> None:
    """Record cost, SLA, and cost-predictor data for a successful inference."""
    return _ops.record_inference_cost_impl(
        agent_type,
        task_id,
        provider,
        model_id,
        input_tokens,
        output_tokens,
        latency_ms,
        project_id,
        lazy_cost_tracker=_lazy_cost_tracker,
        lazy_cost_predictor=_lazy_cost_predictor,
        lazy_sla_tracker=_lazy_sla_tracker,
        logger=logger,
    )


def record_inference_failure(agent_type: str, provider: str, model_id: str, latency_ms: float) -> None:
    """Record SLA and failure-registry data for a failed inference."""
    return _ops.record_inference_failure_impl(
        agent_type,
        provider,
        model_id,
        latency_ms,
        lazy_sla_tracker=_lazy_sla_tracker,
        lazy_failure_registry=_lazy_failure_registry,
        logger=logger,
    )


def record_task_metrics(
    task_id: str,
    agent_type: str,
    latency_ms: float,
    quality_score: float,
    token_count: int,
    success: bool,
) -> None:
    """Feed post-task metrics to anomaly detectors."""
    return _ops.record_task_metrics_impl(
        task_id,
        agent_type,
        latency_ms,
        quality_score,
        token_count,
        success,
        lazy_anomaly_detector=_lazy_anomaly_detector,
        lazy_primary_detector=_lazy_primary_detector,
        logger=logger,
    )


def record_periodic_metrics(request_rate: float, avg_latency_ms: float, queue_depth: int) -> None:
    """Feed periodic system metrics to the capacity forecaster."""
    return _ops.record_periodic_metrics_impl(
        request_rate,
        avg_latency_ms,
        queue_depth,
        lazy_forecaster=_lazy_forecaster,
        logger=logger,
    )


def record_quality_score(quality_score: float) -> None:
    """Feed one quality score to the drift detector."""
    return _ops.record_quality_score_impl(
        quality_score,
        lazy_drift_ensemble=_lazy_drift_ensemble,
        logger=logger,
    )


def record_quality_scores_batch(quality_scores: list[float]) -> None:
    """Feed multiple quality scores to the drift detector."""
    return _ops.record_quality_scores_batch_impl(
        quality_scores,
        lazy_drift_ensemble=_lazy_drift_ensemble,
        logger=logger,
    )


def _validate_quality_score(value: Any, *, index: int | None = None) -> float:
    """Validate and normalize a quality score."""
    return _ops.validate_quality_score(value, index=index)


def get_quality_drift_stats() -> dict[str, float]:
    """Return summary statistics over retained quality-score observations."""
    return _ops.get_quality_drift_stats_impl(lazy_drift_ensemble=_lazy_drift_ensemble, logger=logger)


def record_pipeline_event(
    execution_id: str,
    task_id: str,
    agent_type: str,
    timing_event: str,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Record a pipeline stage transition for value stream analysis."""
    return _ops.record_pipeline_event_impl(
        execution_id,
        task_id,
        agent_type,
        timing_event,
        metadata,
        lazy_value_stream=_lazy_value_stream,
        logger=logger,
    )


def _unavailable_cost_estimate(reason: str) -> dict[str, Any]:
    """Return an explicit unavailable cost prediction payload."""
    return _ops.unavailable_cost_estimate(reason)


def predict_cost(task_type: str, complexity: float, scope_size: int, model_id: str) -> dict[str, Any]:
    """Predict cost before task execution using the calibrated cost model."""
    return _ops.predict_cost_impl(
        task_type,
        complexity,
        scope_size,
        model_id,
        lazy_cost_predictor=_lazy_cost_predictor,
        logger=logger,
    )


def record_actual_cost(
    task_type: str,
    complexity: float,
    scope_size: int,
    model_id: str,
    actual_tokens: int,
    actual_latency: float,
    actual_cost: float,
) -> None:
    """Record observed task cost for cost-predictor calibration."""
    return _ops.record_actual_cost_impl(
        task_type,
        complexity,
        scope_size,
        model_id,
        actual_tokens,
        actual_latency,
        actual_cost,
        lazy_cost_predictor=_lazy_cost_predictor,
        logger=logger,
    )


def record_hardware_metrics(
    gpu_util_pct: float,
    vram_util_pct: float,
    model_load_unload_freq: float,
    cache_hit_rate: float,
) -> None:
    """Feed hardware telemetry to the hardware anomaly detector."""
    return _ops.record_hardware_metrics_impl(
        gpu_util_pct,
        vram_util_pct,
        model_load_unload_freq,
        cache_hit_rate,
        lazy_hardware_detector=_lazy_hardware_detector,
        logger=logger,
    )


def record_failure(
    category: str,
    severity: str,
    description: str,
    root_cause: str = "",
    affected_components: list[str] | None = None,
) -> None:
    """Log a pipeline failure to the persistent failure registry."""
    return _ops.record_failure_impl(
        category,
        severity,
        description,
        root_cause,
        affected_components,
        lazy_failure_registry=_lazy_failure_registry,
        logger=logger,
    )


def record_unknown_family_task_result(model_id: str, architecture: str, quality_score: float) -> None:
    """Record a task result for a model whose family was previously unknown."""
    return _ops.record_unknown_family_task_result_impl(
        model_id,
        architecture,
        quality_score,
        logger=logger,
    )


def reset_all() -> None:
    """Reset all lazy singletons to None for test isolation."""
    global \
        _cost_tracker, \
        _sla_tracker, \
        _anomaly_detector, \
        _forecaster, \
        _drift_ensemble, \
        _value_stream, \
        _cost_predictor, \
        _primary_detector, \
        _hardware_detector, \
        _failure_registry
    _cost_tracker = None
    _sla_tracker = None
    _anomaly_detector = None
    _forecaster = None
    _drift_ensemble = None
    _value_stream = None
    _cost_predictor = None
    _primary_detector = None
    _hardware_detector = None
    _failure_registry = None
    from vetinari.analytics.cost_predictor import reset_cost_predictor_records
    from vetinari.analytics.quality_drift import reset_drift_ensemble_for_test

    reset_cost_predictor_records()
    reset_drift_ensemble_for_test()


reset_wiring = reset_all
