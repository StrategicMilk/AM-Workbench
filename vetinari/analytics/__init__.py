"""Vetinari Analytics Package — Phase 5: Advanced Analytics & Cost Optimization.

Modules
-------
    anomaly      AI-driven anomaly detection (Z-score, IQR, EWMA)
    cost         Cost attribution per agent / task / provider / model
    quality_drift  Output quality drift and embedding distribution shift detection
    sla          SLA / SLO tracking and compliance reporting
    forecasting  Capacity-planning forecasts (SMA, ES, linear trend, seasonal)

Quick start
-----------
    from vetinari.analytics import (
        get_anomaly_detector, AnomalyConfig,
        get_cost_tracker,    CostEntry, ModelPricing,
        get_sla_tracker,     SLOTarget, SLOType,
        get_forecaster,      ForecastRequest,
        QualityDriftDetector, EmbeddingDriftDetector, DriftResult,
    )
"""

from __future__ import annotations

from .anomaly import (
    AnomalyConfig,
    AnomalyDetector,
    AnomalyResult,
    get_anomaly_detector,
    reset_anomaly_detector,
)
from .cost import (
    CostEntry,
    CostReport,
    CostTracker,
    ModelPricing,
    get_cost_tracker,
    reset_cost_tracker,
)
from .cost_predictor import (
    CostEstimate,
    CostPredictor,
    reset_cost_predictor_records,
)
from .failure_registry import (
    FailureRegistry,
    FailureRegistryEntry,
    FailureStatus,
    PreventionRule,
    PreventionRuleType,
    get_failure_registry,
    reset_failure_registry,
)
from .failure_taxonomy import (
    FailureClassifier,
    FailureRecord,
    FailureTracker,
    get_failure_tracker,
    reset_failure_tracker,
)
from .forecasting import (
    Forecaster,
    ForecastRequest,
    ForecastResult,
    get_forecaster,
    reset_forecaster,
)
from .quality_drift import (
    ADWINDetector,
    CUSUMDriftDetector,
    DriftResult,
    EmbeddingDriftDetector,
    PageHinkleyDetector,
    QualityDriftDetector,
    get_drift_ensemble,
)
from .sla import (
    SLABreach,
    SLAReport,
    SLATracker,
    SLOTarget,
    SLOType,
    get_sla_tracker,
    reset_sla_tracker,
)
from .wiring import (
    predict_cost,
    record_actual_cost,
    record_failure,
    record_inference_cost,
    record_inference_failure,
    record_periodic_metrics,
    record_pipeline_event,
    record_quality_score,
    record_task_metrics,
    record_unknown_family_task_result,
    reset_wiring,
)

__all__ = [
    "ADWINDetector",
    "AnomalyConfig",
    "AnomalyDetector",
    "AnomalyResult",
    "CUSUMDriftDetector",
    "CostEntry",
    "CostEstimate",
    "CostPredictor",
    "CostReport",
    "CostTracker",
    "DriftResult",
    "EmbeddingDriftDetector",
    "FailureClassifier",
    "FailureRecord",
    "FailureRegistry",
    "FailureRegistryEntry",
    "FailureStatus",
    "FailureTracker",
    "ForecastRequest",
    "ForecastResult",
    "Forecaster",
    "ModelPricing",
    "PageHinkleyDetector",
    "PreventionRule",
    "PreventionRuleType",
    "QualityDriftDetector",
    "SLABreach",
    "SLAReport",
    "SLATracker",
    "SLOTarget",
    "SLOType",
    "get_anomaly_detector",
    "get_cost_tracker",
    "get_drift_ensemble",
    "get_failure_registry",
    "get_failure_tracker",
    "get_forecaster",
    "get_sla_tracker",
    "predict_cost",
    "record_actual_cost",
    "record_failure",
    "record_inference_cost",
    "record_inference_failure",
    "record_periodic_metrics",
    "record_pipeline_event",
    "record_quality_score",
    "record_task_metrics",
    "record_unknown_family_task_result",
    "reset_anomaly_detector",
    "reset_cost_predictor_records",
    "reset_cost_tracker",
    "reset_failure_registry",
    "reset_failure_tracker",
    "reset_forecaster",
    "reset_sla_tracker",
    "reset_wiring",
]

__version__ = "5.0.0"
