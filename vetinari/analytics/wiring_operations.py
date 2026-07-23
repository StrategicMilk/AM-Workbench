"""Implementation helpers for analytics wiring entry points."""

from __future__ import annotations

import logging
import math
from collections.abc import Callable
from typing import Any

LazyAccessor = Callable[[], Any | None]

_MAX_QUALITY_SCORE_BATCH = 1_000
_MAX_FAILURE_COMPONENTS = 100


def record_inference_cost_impl(
    agent_type: str,
    task_id: str,
    provider: str,
    model_id: str,
    input_tokens: int,
    output_tokens: int,
    latency_ms: float,
    project_id: str | None,
    *,
    lazy_cost_tracker: LazyAccessor,
    lazy_cost_predictor: LazyAccessor,
    lazy_sla_tracker: LazyAccessor,
    logger: logging.Logger,
) -> None:
    """Record cost, cost-predictor calibration, and SLA data for an inference.

    Args:
        agent_type: Agent type value consumed by record_inference_cost_impl().
        task_id: Task id value consumed by record_inference_cost_impl().
        provider: Provider name or adapter selected for the operation.
        model_id: Model identifier used for routing or lookup.
        input_tokens: Input tokens value consumed by record_inference_cost_impl().
        output_tokens: Output tokens value consumed by record_inference_cost_impl().
        latency_ms: Latency ms value consumed by record_inference_cost_impl().
        project_id: Project identifier that scopes the operation.
        lazy_cost_tracker: Lazy cost tracker value consumed by record_inference_cost_impl().
        lazy_cost_predictor: Lazy cost predictor value consumed by record_inference_cost_impl().
        lazy_sla_tracker: Lazy sla tracker value consumed by record_inference_cost_impl().
        logger: Logger used for diagnostic output.
    """
    recorded_entry: Any | None = None
    total_tokens = input_tokens + output_tokens
    try:
        from vetinari.analytics.cost import CostEntry

        tracker = lazy_cost_tracker()
        if tracker is not None:
            recorded_entry = tracker.record(
                CostEntry(
                    agent=agent_type,
                    task_id=task_id,
                    provider=provider,
                    model=model_id,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    project_id=project_id,
                    latency_ms=latency_ms,
                )
            )
    except Exception:
        logger.warning(
            "Cost tracking failed for model %s (task %s) - cost report will be incomplete",
            model_id,
            task_id,
        )

    try:
        predictor = lazy_cost_predictor()
        if predictor is not None:
            if total_tokens <= 0 or latency_ms <= 0.0 or not math.isfinite(latency_ms):
                logger.warning(
                    "Cost predictor calibration skipped for task %s; tokens or latency were unavailable", task_id
                )
            else:
                actual_cost_value = getattr(recorded_entry, "cost_usd", None)
                if actual_cost_value is None:
                    logger.warning(
                        "Cost predictor calibration skipped for task %s; actual cost was unavailable", task_id
                    )
                else:
                    predictor.record_actual(
                        task_type=agent_type.lower(),
                        complexity=1.0,
                        scope_size=total_tokens,
                        model=model_id,
                        actual_tokens=total_tokens,
                        actual_latency=latency_ms / 1000.0,
                        actual_cost=float(actual_cost_value),
                    )
    except Exception:
        logger.warning(
            "Cost predictor calibration failed for model %s (task %s); future predictions may remain stale",
            model_id,
            task_id,
        )

    try:
        sla = lazy_sla_tracker()
        if sla is not None:
            sla.record_latency(f"{provider}:{model_id}", latency_ms, success=True)
            sla.record_request(success=True)
    except Exception:
        logger.warning("SLA tracking failed for %s:%s - SLA compliance reports may be stale", provider, model_id)


def record_inference_failure_impl(
    agent_type: str,
    provider: str,
    model_id: str,
    latency_ms: float,
    *,
    lazy_sla_tracker: LazyAccessor,
    lazy_failure_registry: LazyAccessor,
    logger: logging.Logger,
) -> None:
    """Record failed inference SLA and failure-registry data.

    Args:
        agent_type: Agent type value consumed by record_inference_failure_impl().
        provider: Provider name or adapter selected for the operation.
        model_id: Model identifier used for routing or lookup.
        latency_ms: Latency ms value consumed by record_inference_failure_impl().
        lazy_sla_tracker: Lazy sla tracker value consumed by record_inference_failure_impl().
        lazy_failure_registry: Lazy failure registry value consumed by record_inference_failure_impl().
        logger: Logger used for diagnostic output.
    """
    try:
        sla = lazy_sla_tracker()
        if sla is not None:
            sla.record_latency(f"{provider}:{model_id}", latency_ms, success=False)
            sla.record_request(success=False)
    except Exception:
        logger.warning(
            "SLA failure tracking failed for %s:%s (agent %s) - error rate metrics may be understated",
            provider,
            model_id,
            agent_type,
        )

    try:
        registry = lazy_failure_registry()
        if registry is not None:
            registry.log_failure(
                category="model_timeout",
                severity="error",
                description=(
                    f"Inference failure for model {model_id} via {provider}"
                    f" (agent {agent_type}, latency {latency_ms:.0f}ms)"
                ),
                root_cause=f"Model {model_id} failed to respond via provider {provider}",
                affected_components=[agent_type, provider, model_id],
            )
    except Exception:
        logger.warning("Failure registry logging failed for %s:%s - failure not persisted", provider, model_id)


def record_task_metrics_impl(
    task_id: str,
    agent_type: str,
    latency_ms: float,
    quality_score: float,
    token_count: int,
    success: bool,
    *,
    lazy_anomaly_detector: LazyAccessor,
    lazy_primary_detector: LazyAccessor,
    logger: logging.Logger,
) -> None:
    """Feed post-task metrics to univariate and multivariate anomaly detectors.

    Args:
        task_id: Task id value consumed by record_task_metrics_impl().
        agent_type: Agent type value consumed by record_task_metrics_impl().
        latency_ms: Latency ms value consumed by record_task_metrics_impl().
        quality_score: Score value evaluated by the operation.
        token_count: Token count value consumed by record_task_metrics_impl().
        success: Success value consumed by record_task_metrics_impl().
        lazy_anomaly_detector: Lazy anomaly detector value consumed by record_task_metrics_impl().
        lazy_primary_detector: Lazy primary detector value consumed by record_task_metrics_impl().
        logger: Logger used for diagnostic output.
    """
    try:
        detector = lazy_anomaly_detector()
        if detector is not None:
            detector.detect("task_latency", latency_ms)
            detector.detect("task_quality", quality_score)
            detector.detect("task_tokens", float(token_count))
    except Exception:
        logger.warning(
            "Anomaly detection failed for task %s (agent %s) - anomaly alerts may be delayed", task_id, agent_type
        )

    try:
        primary = lazy_primary_detector()
        if primary is not None:
            primary.observe(
                latency=latency_ms,
                error_rate=0.0 if success else 1.0,
                token_usage=float(token_count),
                quality_score=quality_score,
            )
    except Exception:
        logger.warning(
            "Isolation Forest primary detection failed for task %s (agent %s) - multivariate anomaly alerts may be delayed",
            task_id,
            agent_type,
        )


def record_periodic_metrics_impl(
    request_rate: float,
    avg_latency_ms: float,
    queue_depth: int,
    *,
    lazy_forecaster: LazyAccessor,
    logger: logging.Logger,
) -> None:
    """Feed periodic system metrics to the capacity forecaster.

    Args:
        request_rate: Request object sent through the operation.
        avg_latency_ms: Avg latency ms value consumed by record_periodic_metrics_impl().
        queue_depth: Queue depth value consumed by record_periodic_metrics_impl().
        lazy_forecaster: Lazy forecaster value consumed by record_periodic_metrics_impl().
        logger: Logger used for diagnostic output.
    """
    try:
        forecaster = lazy_forecaster()
        if forecaster is not None:
            forecaster.ingest("request_rate", request_rate)
            forecaster.ingest("avg_latency_ms", avg_latency_ms)
            forecaster.ingest("queue_depth", float(queue_depth))
    except Exception:
        logger.warning(
            "Forecaster ingestion failed (rate=%.2f, latency=%.1fms, queue=%d) - capacity predictions will be stale",
            request_rate,
            avg_latency_ms,
            queue_depth,
        )


def record_quality_score_impl(
    quality_score: float,
    *,
    lazy_drift_ensemble: LazyAccessor,
    logger: logging.Logger,
) -> None:
    """Feed one quality score to the drift detector."""
    validated_score = validate_quality_score(quality_score)
    try:
        drift = lazy_drift_ensemble()
        if drift is not None:
            drift.observe(validated_score)
    except Exception:
        logger.warning(
            "Quality drift observation failed (score=%.4f) - drift detection may miss degradation", validated_score
        )


def record_quality_scores_batch_impl(
    quality_scores: list[float],
    *,
    lazy_drift_ensemble: LazyAccessor,
    logger: logging.Logger,
) -> None:
    """Feed multiple quality scores to the drift detector.

    Raises:
        ValueError: If the batch exceeds the retention cap or contains invalid scores.
    """
    if not quality_scores:
        return
    if len(quality_scores) > _MAX_QUALITY_SCORE_BATCH:
        raise ValueError(f"quality_scores exceeds max_items={_MAX_QUALITY_SCORE_BATCH}")
    validated_scores = [validate_quality_score(score, index=index) for index, score in enumerate(quality_scores)]
    try:
        drift = lazy_drift_ensemble()
        if drift is not None:
            drift.observe_many(validated_scores)
    except Exception:
        logger.warning(
            "Batch quality drift observation failed (%d scores) - drift detection may miss degradation",
            len(validated_scores),
        )


def validate_quality_score(value: Any, *, index: int | None = None) -> float:
    """Validate and normalize a quality score.

    Returns:
        Value produced for the caller.

    Raises:
        ValueError: Propagated when validation, persistence, or execution fails.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        location = f" at index {index}" if index is not None else ""
        raise ValueError(f"quality score{location} must be a numeric value")
    score = float(value)
    if not math.isfinite(score) or score < 0.0 or score > 1.0:
        location = f" at index {index}" if index is not None else ""
        raise ValueError(f"quality score{location} must be finite and in range [0.0, 1.0]")
    return score


def get_quality_drift_stats_impl(*, lazy_drift_ensemble: LazyAccessor, logger: logging.Logger) -> dict[str, float]:
    """Return summary statistics over the quality-score observation window.

    Returns:
        Value produced for the caller.
    """
    try:
        drift = lazy_drift_ensemble()
        if drift is not None:
            return dict[str, float](drift.get_raw_stats())
    except Exception:
        logger.warning("Could not retrieve quality drift stats - drift ensemble may not be initialised")
    return {}


def record_pipeline_event_impl(
    execution_id: str,
    task_id: str,
    agent_type: str,
    timing_event: str,
    metadata: dict[str, Any] | None,
    *,
    lazy_value_stream: LazyAccessor,
    logger: logging.Logger,
) -> None:
    """Record a pipeline stage transition for value stream analysis.

    Args:
        execution_id: Execution id value consumed by record_pipeline_event_impl().
        task_id: Task id value consumed by record_pipeline_event_impl().
        agent_type: Agent type value consumed by record_pipeline_event_impl().
        timing_event: Event object recorded or transformed by the operation.
        metadata: Structured data consumed by the operation.
        lazy_value_stream: Value processed by the operation.
        logger: Logger used for diagnostic output.
    """
    try:
        value_stream = lazy_value_stream()
        if value_stream is not None:
            value_stream.record_event(
                execution_id=execution_id,
                task_id=task_id,
                agent_type=agent_type,
                timing_event=timing_event,
                metadata=metadata or {},
            )
    except Exception:
        logger.warning(
            "Value stream recording failed for execution %s task %s event %s - lead time metrics will be incomplete",
            execution_id,
            task_id,
            timing_event,
        )


def unavailable_cost_estimate(reason: str) -> dict[str, Any]:
    """Return an explicit unavailable cost prediction payload."""
    return {
        "tokens": 0,
        "latency_seconds": 0.0,
        "cost_usd": 0.0,
        "confidence": 0.0,
        "available": False,
        "reason": reason,
    }


def predict_cost_impl(
    task_type: str,
    complexity: float,
    scope_size: int,
    model_id: str,
    *,
    lazy_cost_predictor: LazyAccessor,
    logger: logging.Logger,
) -> dict[str, Any]:
    """Predict cost before task execution.

    Args:
        task_type: Task type value consumed by predict_cost_impl().
        complexity: Complexity value consumed by predict_cost_impl().
        scope_size: Scope size value consumed by predict_cost_impl().
        model_id: Model identifier used for routing or lookup.
        lazy_cost_predictor: Lazy cost predictor value consumed by predict_cost_impl().
        logger: Logger used for diagnostic output.

    Returns:
        Value produced for the caller.
    """
    try:
        predictor = lazy_cost_predictor()
        if predictor is not None:
            estimate = predictor.predict(task_type, complexity, scope_size, model_id)
            return {
                "tokens": estimate.tokens,
                "latency_seconds": estimate.latency_seconds,
                "cost_usd": estimate.cost_usd,
                "confidence": estimate.confidence,
                "available": True,
            }
        return unavailable_cost_estimate("predictor_unavailable")
    except Exception:
        logger.warning(
            "Cost prediction failed for task_type=%s model=%s - no pre-execution estimate available",
            task_type,
            model_id,
        )
    return unavailable_cost_estimate("prediction_failed")


def record_actual_cost_impl(
    task_type: str,
    complexity: float,
    scope_size: int,
    model_id: str,
    actual_tokens: int,
    actual_latency: float,
    actual_cost: float,
    *,
    lazy_cost_predictor: LazyAccessor,
    logger: logging.Logger,
) -> None:
    """Record observed cost after task execution.

    Args:
        task_type: Task type value consumed by record_actual_cost_impl().
        complexity: Complexity value consumed by record_actual_cost_impl().
        scope_size: Scope size value consumed by record_actual_cost_impl().
        model_id: Model identifier used for routing or lookup.
        actual_tokens: Actual tokens value consumed by record_actual_cost_impl().
        actual_latency: Actual latency value consumed by record_actual_cost_impl().
        actual_cost: Actual cost value consumed by record_actual_cost_impl().
        lazy_cost_predictor: Lazy cost predictor value consumed by record_actual_cost_impl().
        logger: Logger used for diagnostic output.
    """
    try:
        predictor = lazy_cost_predictor()
        if predictor is not None:
            predictor.record_actual(
                task_type=task_type,
                complexity=complexity,
                scope_size=scope_size,
                model=model_id,
                actual_tokens=actual_tokens,
                actual_latency=actual_latency,
                actual_cost=actual_cost,
            )
    except Exception:
        logger.warning(
            "Cost actual recording failed for task_type=%s model=%s - predictor calibration will be delayed",
            task_type,
            model_id,
        )


def record_hardware_metrics_impl(
    gpu_util_pct: float,
    vram_util_pct: float,
    model_load_unload_freq: float,
    cache_hit_rate: float,
    *,
    lazy_hardware_detector: LazyAccessor,
    logger: logging.Logger,
) -> None:
    """Feed hardware telemetry to the Isolation Forest hardware anomaly detector.

    Args:
        gpu_util_pct: Gpu util pct value consumed by record_hardware_metrics_impl().
        vram_util_pct: Vram util pct value consumed by record_hardware_metrics_impl().
        model_load_unload_freq: Model load unload freq value consumed by record_hardware_metrics_impl().
        cache_hit_rate: Cache hit rate value consumed by record_hardware_metrics_impl().
        lazy_hardware_detector: Lazy hardware detector value consumed by record_hardware_metrics_impl().
        logger: Logger used for diagnostic output.
    """
    try:
        hw = lazy_hardware_detector()
        if hw is not None:
            hw.observe(
                gpu_util_pct=gpu_util_pct,
                vram_util_pct=vram_util_pct,
                model_load_unload_freq=model_load_unload_freq,
                cache_hit_rate=cache_hit_rate,
            )
    except Exception:
        logger.warning(
            "Isolation Forest hardware detection failed (gpu=%.1f%%, vram=%.1f%%, swaps=%.2f/min, cache_hit=%.3f)",
            gpu_util_pct,
            vram_util_pct,
            model_load_unload_freq,
            cache_hit_rate,
        )


def record_failure_impl(
    category: str,
    severity: str,
    description: str,
    root_cause: str,
    affected_components: list[str] | None,
    *,
    lazy_failure_registry: LazyAccessor,
    logger: logging.Logger,
) -> None:
    """Log a pipeline failure to the persistent failure registry.

    Args:
        category: Category value consumed by record_failure_impl().
        severity: Severity value consumed by record_failure_impl().
        description: Description value consumed by record_failure_impl().
        root_cause: Root cause value consumed by record_failure_impl().
        affected_components: Affected components value consumed by record_failure_impl().
        lazy_failure_registry: Lazy failure registry value consumed by record_failure_impl().
        logger: Logger used for diagnostic output.

    Raises:
        ValueError: If ``affected_components`` exceeds the configured cap.
    """
    if affected_components is not None and len(affected_components) > _MAX_FAILURE_COMPONENTS:
        raise ValueError(f"affected_components exceeds max_items={_MAX_FAILURE_COMPONENTS}")
    try:
        registry = lazy_failure_registry()
        if registry is not None:
            registry.log_failure(
                category=category,
                severity=severity,
                description=description,
                root_cause=root_cause,
                affected_components=affected_components,
            )
    except Exception:
        logger.warning("Failure registry logging failed for category=%s - failure not persisted", category)


def record_unknown_family_task_result_impl(
    model_id: str,
    architecture: str,
    quality_score: float,
    *,
    logger: logging.Logger,
) -> None:
    """Record a task result for a model whose family was previously unknown.

    Args:
        model_id: Model identifier used for routing or lookup.
        architecture: Architecture value consumed by record_unknown_family_task_result_impl().
        quality_score: Score value evaluated by the operation.
        logger: Logger used for diagnostic output.
    """
    try:
        from vetinari.models.model_profiler_data import record_unknown_family_task

        record_unknown_family_task(model_id, architecture, quality_score)
    except Exception:
        logger.warning(
            "Unknown-family task recording failed for model %s - family auto-creation may be delayed", model_id
        )
