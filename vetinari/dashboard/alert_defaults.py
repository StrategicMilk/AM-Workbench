"""Default dashboard alert thresholds."""

from __future__ import annotations

from vetinari.dashboard.alert_types import AlertCondition, AlertSeverity, AlertThreshold

DEFAULT_ALERT_THRESHOLDS = (
    AlertThreshold(
        name="inference_timeout_rate",
        metric_key="adapters.timeout_rate_percent",
        condition=AlertCondition.GREATER_THAN,
        threshold_value=5.0,
        severity=AlertSeverity.HIGH,
        channels=["log", "dashboard"],
        fail_on_missing_metric=True,
        runbook_url="docs/runbooks/operability-release-recovery.md#inference-timeouts",
    ),
    AlertThreshold(
        name="workbench_spine_corruption",
        metric_key="workbench.spine_corruption_count",
        condition=AlertCondition.GREATER_THAN,
        threshold_value=0.0,
        severity=AlertSeverity.HIGH,
        channels=["log", "dashboard"],
        fail_on_missing_metric=True,
        runbook_url="docs/runbooks/operability-release-recovery.md#corrupt-workbench-spine",
    ),
    AlertThreshold(
        name="authz_rejection_rate",
        metric_key="security.authz_rejection_rate_percent",
        condition=AlertCondition.GREATER_THAN,
        threshold_value=0.0,
        severity=AlertSeverity.HIGH,
        channels=["log", "dashboard"],
        fail_on_missing_metric=True,
        runbook_url="docs/runbooks/incident-response.md",
    ),
    AlertThreshold(
        name="rag_embedding_fallback_rate",
        metric_key="rag.embedding_fallback_rate_percent",
        condition=AlertCondition.GREATER_THAN,
        threshold_value=0.0,
        severity=AlertSeverity.HIGH,
        channels=["log", "dashboard"],
        fail_on_missing_metric=True,
        runbook_url="docs/runbooks/operability-release-recovery.md#rag-fallbacks",
    ),
    AlertThreshold(
        name="inference_failure_rate_spike",
        metric_key="adapters.failure_rate_percent",
        condition=AlertCondition.GREATER_THAN,
        threshold_value=10.0,
        severity=AlertSeverity.HIGH,
        channels=["log", "dashboard"],
    ),
    AlertThreshold(
        name="training_rejection_rate_spike",
        metric_key="training.rejection_rate_percent",
        condition=AlertCondition.GREATER_THAN,
        threshold_value=20.0,
        severity=AlertSeverity.HIGH,
        channels=["log", "dashboard"],
    ),
    AlertThreshold(
        name="health_degradation_detected",
        metric_key="health.degraded_components",
        condition=AlertCondition.GREATER_THAN,
        threshold_value=0.0,
        severity=AlertSeverity.HIGH,
        channels=["log", "dashboard"],
    ),
)
