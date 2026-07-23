"""Event payload contracts published through the Vetinari EventBus.

The EventBus uses these immutable dataclasses as messages between orchestration,
analytics, training, and runtime subsystems. The public import path remains
``vetinari.events``; this module owns the payload definitions so the bus runtime
does not also own every domain event shape.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from vetinari.boundary_guards import require_score_in_range


@dataclass(frozen=True, slots=True)
class Event:
    """Base class for all events in the Vetinari event bus.

    Args:
        event_type: Discriminator string identifying the event kind.
        timestamp: Wall-clock time when the event was created.
    """

    event_type: str
    timestamp: float

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(event_type={self.event_type!r}, timestamp={self.timestamp!r})"


@dataclass(frozen=True, slots=True)
class TaskStarted(Event):
    """Published when a task begins execution.

    Args:
        task_id: Unique identifier of the task.
        agent_type: The ``AgentType.value`` string of the executing agent.
        timestamp: Wall-clock time when the event was created.
    """

    task_id: str = ""
    agent_type: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "event_type", "TaskStarted")

    def __repr__(self) -> str:
        return f"TaskStarted(task_id={self.task_id!r}, agent_type={self.agent_type!r})"


@dataclass(frozen=True, slots=True)
class TaskCompleted(Event):
    """Published when a task finishes execution.

    Args:
        task_id: Unique identifier of the task.
        agent_type: The ``AgentType.value`` string of the executing agent.
        success: Whether the task succeeded.
        duration_ms: Elapsed wall-clock time in milliseconds.
        model_id: Model that produced the task output, when known.
        task_type: Learning category for model-quality memory.
        quality_score: Explicit quality signal in [0.0, 1.0], when measured.
        timestamp: Wall-clock time when the event was created.
    """

    task_id: str = ""
    agent_type: str = ""
    success: bool = False
    duration_ms: float = 0.0
    model_id: str = ""
    task_type: str = ""
    quality_score: float | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "event_type", "TaskCompleted")

    def __repr__(self) -> str:
        return (
            f"TaskCompleted(task_id={self.task_id!r}, agent_type={self.agent_type!r}, "
            f"success={self.success!r}, duration_ms={self.duration_ms!r})"
        )


@dataclass(frozen=True, slots=True)
class QualityGateResult(Event):
    """Published after a quality review completes.

    Args:
        task_id: Unique identifier of the reviewed task.
        passed: Whether the quality gate passed.
        score: Numeric quality score in the range ``[0.0, 1.0]``.
        issues: List of human-readable issue descriptions.
        timestamp: Wall-clock time when the event was created.
    """

    task_id: str = ""
    passed: bool = False
    score: float = 0.0
    issues: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        object.__setattr__(self, "event_type", "QualityGateResult")
        object.__setattr__(
            self,
            "score",
            require_score_in_range(self.score, field_name="score"),
        )

    def __repr__(self) -> str:
        return (
            f"QualityGateResult(task_id={self.task_id!r}, passed={self.passed!r}, "
            f"score={self.score!r}, issues={len(self.issues)})"
        )


@dataclass(frozen=True, slots=True)
class ResourceRequest(Event):
    """Published when an agent requests an external resource.

    Args:
        agent_type: The ``AgentType.value`` string of the requesting agent.
        resource_type: Category of resource being requested.
        details: Arbitrary metadata describing the request.
        timestamp: Wall-clock time when the event was created.
    """

    agent_type: str = ""
    resource_type: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "event_type", "ResourceRequest")

    def __repr__(self) -> str:
        return f"ResourceRequest(agent_type={self.agent_type!r}, resource_type={self.resource_type!r})"


@dataclass(frozen=True, slots=True)
class HumanApprovalNeeded(Event):
    """Published when a task requires human approval to proceed.

    Args:
        task_id: Unique identifier of the task requiring approval.
        reason: Human-readable explanation of why approval is needed.
        context: Arbitrary metadata providing additional context for the approver.
        timestamp: Wall-clock time when the event was created.
    """

    task_id: str = ""
    reason: str = ""
    context: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "event_type", "HumanApprovalNeeded")

    def __repr__(self) -> str:
        return f"HumanApprovalNeeded(task_id={self.task_id!r}, reason={self.reason!r})"


@dataclass(frozen=True, slots=True)
class AnomalyDetected(Event):
    """Published when an ensemble anomaly detector confirms an anomaly.

    Args:
        agent_type: The agent type where the anomaly was detected.
        anomaly_type: The type of anomaly.
        triggered_detectors: List of detector names that triggered.
        score: Anomaly severity score.
        timestamp: Wall-clock time when the event was created.
    """

    agent_type: str = ""
    anomaly_type: str = ""
    triggered_detectors: list[str] = field(default_factory=list)
    score: float = 0.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "event_type", "AnomalyDetected")

    def __repr__(self) -> str:
        return (
            f"AnomalyDetected(agent_type={self.agent_type!r}, anomaly_type={self.anomaly_type!r}, score={self.score!r})"
        )


@dataclass(frozen=True, slots=True)
class RetrainingRecommended(Event):
    """Published when forecasting predicts quality dropping below SLA threshold.

    Args:
        metric: The quality metric being forecast.
        predicted_quality: The predicted quality value at breach point.
        days_until_breach: Estimated days until SLA breach.
        confidence_interval: The confidence interval width at breach point.
        forecast_method_used: Which forecast method produced this prediction.
        timestamp: Wall-clock time when the event was created.
    """

    metric: str = ""
    predicted_quality: float = 0.0
    days_until_breach: int = 0
    confidence_interval: float = 0.0
    forecast_method_used: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "event_type", "RetrainingRecommended")

    def __repr__(self) -> str:
        return (
            f"RetrainingRecommended(metric={self.metric!r}, "
            f"predicted_quality={self.predicted_quality!r}, "
            f"days_until_breach={self.days_until_breach!r})"
        )


@dataclass(frozen=True, slots=True)
class TaskTimingRecord(Event):
    """Timing record for value stream analysis.

    Captures when each stage transition happens for a task, enabling
    computation of queue time, processing time, and waste.

    Args:
        task_id: Unique identifier of the task.
        execution_id: ID of the overall execution this task belongs to.
        agent_type: The agent type processing this task.
        timing_event: Which stage transition occurred.
        metadata: Additional context such as queue depth or model used.
        timestamp: Wall-clock time when the event was created.
    """

    task_id: str = ""
    execution_id: str = ""
    agent_type: str = ""
    timing_event: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "event_type", "TaskTimingRecord")

    def __repr__(self) -> str:
        return (
            f"TaskTimingRecord(task_id={self.task_id!r}, execution_id={self.execution_id!r}, "
            f"timing_event={self.timing_event!r})"
        )


@dataclass(frozen=True, slots=True)
class KaizenImprovementProposed(Event):
    """Published when a new kaizen improvement is proposed.

    Args:
        improvement_id: Unique identifier of the proposed improvement.
        hypothesis: What the improvement is expected to achieve.
        metric: Which metric is being improved.
        applied_by: Which subsystem proposed this improvement.
        timestamp: Wall-clock time when the event was created.
    """

    improvement_id: str = ""
    hypothesis: str = ""
    metric: str = ""
    applied_by: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "event_type", "KaizenImprovementProposed")

    def __repr__(self) -> str:
        return (
            f"KaizenImprovementProposed(improvement_id={self.improvement_id!r}, "
            f"metric={self.metric!r}, applied_by={self.applied_by!r})"
        )


@dataclass(frozen=True, slots=True)
class KaizenImprovementConfirmed(Event):
    """Published when a kaizen improvement is confirmed.

    Args:
        improvement_id: Unique identifier of the confirmed improvement.
        metric: Which metric was improved.
        baseline_value: Metric value before improvement.
        actual_value: Measured metric value after observation.
        applied_by: Which subsystem applied this improvement.
        timestamp: Wall-clock time when the event was created.
    """

    improvement_id: str = ""
    metric: str = ""
    baseline_value: float = 0.0
    actual_value: float = 0.0
    applied_by: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "event_type", "KaizenImprovementConfirmed")

    def __repr__(self) -> str:
        return (
            f"KaizenImprovementConfirmed(improvement_id={self.improvement_id!r}, "
            f"metric={self.metric!r}, baseline_value={self.baseline_value!r}, "
            f"actual_value={self.actual_value!r})"
        )


@dataclass(frozen=True, slots=True)
class KaizenImprovementActive(Event):
    """Published when a kaizen improvement moves from proposed to active.

    Args:
        improvement_id: Unique identifier of the improvement now under trial.
        metric: Which metric the improvement targets.
        applied_by: Which subsystem activated this improvement.
        timestamp: Wall-clock time when the event was created.
    """

    improvement_id: str = ""
    metric: str = ""
    applied_by: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "event_type", "KaizenImprovementActive")

    def __repr__(self) -> str:
        return (
            f"KaizenImprovementActive(improvement_id={self.improvement_id!r}, "
            f"metric={self.metric!r}, applied_by={self.applied_by!r})"
        )


@dataclass(frozen=True, slots=True)
class KaizenImprovementReverted(Event):
    """Published when a kaizen improvement is reverted due to regression.

    Args:
        improvement_id: Unique identifier of the reverted improvement.
        metric: Which metric regressed.
        reason: Why the improvement was reverted.
        timestamp: Wall-clock time when the event was created.
    """

    improvement_id: str = ""
    metric: str = ""
    reason: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "event_type", "KaizenImprovementReverted")

    def __repr__(self) -> str:
        return (
            f"KaizenImprovementReverted(improvement_id={self.improvement_id!r}, "
            f"metric={self.metric!r}, reason={self.reason!r})"
        )


@dataclass(frozen=True, slots=True)
class KaizenLintFinding(Event):
    """Published when knowledge lint detects an issue.

    Args:
        finding_id: Unique identifier for the lint finding.
        category: Lint category.
        description: Human-readable description of the finding.
        severity: Finding severity.
    """

    finding_id: str = ""
    category: str = ""
    description: str = ""
    severity: str = "warning"

    def __post_init__(self) -> None:
        object.__setattr__(self, "event_type", "KaizenLintFinding")

    def __repr__(self) -> str:
        return f"KaizenLintFinding(finding_id={self.finding_id!r}, category={self.category!r})"


@dataclass(frozen=True, slots=True)
class QualityDriftDetected(Event):
    """Published when ensemble drift detectors confirm a quality shift.

    Fired by ``QualityDriftDetector`` when multiple detectors agree on drift.

    Args:
        task_type: The task type experiencing drift.
        triggered_detectors: Names of detectors that triggered.
        observation_count: Total observations processed at time of detection.
        timestamp: Wall-clock time when the event was created.
    """

    task_type: str = ""
    triggered_detectors: list[str] = field(default_factory=list)
    observation_count: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "event_type", "QUALITY_DRIFT")

    def __repr__(self) -> str:
        return f"QualityDriftDetected(task_type={self.task_type!r}, triggered_detectors={self.triggered_detectors!r})"


@dataclass(frozen=True, slots=True)
class TelemetryAlertEvent(Event):
    """Published when a telemetry threshold breach is detected.

    Args:
        alert_type: Short identifier for the breach.
        message: Human-readable description of what breached and by how much.
        metadata: Numeric context for the alert.
    """

    alert_type: str = ""
    message: str = ""
    metadata: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "event_type", "TELEMETRY_ALERT")

    def __repr__(self) -> str:
        return f"TelemetryAlertEvent(alert_type={self.alert_type!r}, message={self.message!r})"


@dataclass(frozen=True, slots=True)
class ClarificationRequested(Event):
    """Published when a durable execution task pauses for clarification.

    Args:
        execution_id: Execution that contains the paused task.
        task_id: Task that raised ``ClarificationNeeded``.
        question_id: Persisted question-set identifier.
        questions: Questions that require answers before resume.
        paused_at: ISO-8601 UTC timestamp when the pause was persisted.
    """

    execution_id: str = ""
    task_id: str = ""
    question_id: str = ""
    questions: list[str] = field(default_factory=list)
    paused_at: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "event_type", "clarification.requested")

    def __repr__(self) -> str:
        return (
            f"ClarificationRequested(execution_id={self.execution_id!r}, "
            f"task_id={self.task_id!r}, question_id={self.question_id!r}, "
            f"questions={len(self.questions)!r})"
        )


@dataclass(frozen=True, slots=True)
class CpuTierStatusChanged(Event):
    """Published when a resident CPU inference tier changes state.

    Args:
        compute_id: Router compute id for the tier.
        state: Health or lifecycle state.
        queue_depth: Current queue depth observed by the tier.
    """

    compute_id: str = ""
    state: str = ""
    queue_depth: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "event_type", "CpuTierStatusChanged")

    def __repr__(self) -> str:
        return (
            f"CpuTierStatusChanged(compute_id={self.compute_id!r}, "
            f"state={self.state!r}, queue_depth={self.queue_depth!r})"
        )


@dataclass(frozen=True, slots=True)
class CpuTierRouteStatusChanged(Event):
    """Published when router-visible compute-tier health changes."""

    compute_id: str = ""
    state: str = ""
    queue_depth: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "event_type", "CpuTierRouteStatusChanged")

    def __repr__(self) -> str:
        return (
            f"CpuTierRouteStatusChanged(compute_id={self.compute_id!r}, "
            f"state={self.state!r}, queue_depth={self.queue_depth!r})"
        )


def clarification_requested(
    *,
    execution_id: str,
    task_id: str,
    question_id: str,
    questions: list[str],
    paused_at: str,
) -> ClarificationRequested:
    """Create a clarification request event stamped with the current time.

    Args:
        execution_id: Execution that contains the paused task.
        task_id: Task that raised the clarification request.
        question_id: Persisted question-set identifier.
        questions: Questions that require answers before resume.
        paused_at: ISO-8601 UTC timestamp when the pause was persisted.

    Returns:
        Clarification event ready to publish through the EventBus.
    """
    return ClarificationRequested(
        event_type="clarification.requested",
        timestamp=time.time(),
        execution_id=execution_id,
        task_id=task_id,
        question_id=question_id,
        questions=questions,
        paused_at=paused_at,
    )
