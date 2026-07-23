"""Pipeline engine entry helpers for event emission and queue admission."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any, Protocol, cast

from vetinari.orchestration.pipeline_events import EventSeverity, PipelineEvent, PipelineStage
from vetinari.orchestration.request_routing import (
    PRIORITY_CUSTOM,
    PRIORITY_EXPRESS,
    PRIORITY_STANDARD,
    QueueFullError,
    RequestQueue,
)

logger = logging.getLogger(__name__)


_STAGE_TO_TIMING: dict[PipelineStage, str] = {
    PipelineStage.INTAKE: "task_queued",
    PipelineStage.PLAN_GEN: "task_dispatched",
    PipelineStage.EXECUTION: "task_dispatched",
    PipelineStage.REVIEW: "task_completed",
    PipelineStage.ASSEMBLY: "task_completed",
}


class _PipelineEntryOwner(Protocol):
    """Host contract required by _PipelineEngineEntryPoint."""

    _event_handler: Any
    _request_queue: RequestQueue

    def _execute_pipeline(
        self,
        goal: str,
        constraints: dict[str, Any] | None,
        context: dict[str, Any],
        stages: dict[str, Any],
        start_time: float,
        correlation_context: Any | None,
        pipeline_span: Any | None,
        intake_tier: Any | None,
        intake_features: Any | None,
        task_handler: Callable[..., Any] | None,
        project_id: str | None,
        model_id: str | None,
    ) -> dict[str, Any]:
        """Execute the full pipeline after queue admission."""


class _PipelineEngineEntryPoint:
    """Provide pipeline event emission and request queue admission."""

    def _emit(
        self,
        stage: PipelineStage,
        event_type: str,
        data: dict[str, Any] | None = None,
        severity: EventSeverity = EventSeverity.INFO,
    ) -> None:
        """Emit a pipeline event to the registered event handler."""
        try:
            owner = cast(_PipelineEntryOwner, self)
            event_data = dict(data) if data is not None else {}
            trace_id = getattr(self, "_current_trace_id", None)
            if trace_id and "trace_id" not in event_data:
                event_data["trace_id"] = trace_id
            owner._event_handler.on_event(
                PipelineEvent(stage=stage, event_type=event_type, data=event_data, severity=severity)
            )
        except Exception:
            logger.warning("Event emission failed for %s/%s", stage.value, event_type, exc_info=True)
        self._record_pipeline_timing(stage, event_type, data)

    @staticmethod
    def _record_pipeline_timing(
        stage: PipelineStage,
        event_type: str,
        data: dict[str, Any] | None,
    ) -> None:
        """Record value-stream timing for stages mapped to analytics events."""
        timing_event = _STAGE_TO_TIMING.get(stage, "")
        if not timing_event:
            return
        try:
            from vetinari.analytics.wiring import record_pipeline_event

            event_payload = data if data is not None else {}
            execution_id = str(event_payload.get("exec_id", "")) or "unknown"
            record_pipeline_event(
                execution_id=execution_id,
                task_id=str(event_payload.get("task_id", "")),
                agent_type=str(event_payload.get("agent_type", "")),
                timing_event=timing_event,
                metadata={"stage": stage.value, "event_type": event_type},
            )
        except Exception as exc:
            logger.warning("Pipeline event recording skipped for stage %s: %s", stage, exc)

    @staticmethod
    def _enter_correlation_context() -> Any | None:
        """Enter structured logging correlation context when available."""
        try:
            from vetinari.structured_logging import CorrelationContext

            correlation_context = CorrelationContext()
            correlation_context.__enter__()
            return correlation_context
        except (ImportError, AttributeError):
            logger.warning("Failed to initialize CorrelationContext for pipeline", exc_info=True)
            return None

    @staticmethod
    def _start_pipeline_span(goal: str, model_id: str | None) -> Any | None:
        """Start an optional GenAI tracing span for the pipeline.

        Redacts emails, secrets, provider URLs, and host paths from the goal
        text before it lands on the observability span so PII never leaves
        process boundaries via OTel exporters or trace storage.
        """
        try:
            from vetinari.observability.otel_genai import get_genai_tracer
            from vetinari.security.redaction import redact_text

            pipeline_span = get_genai_tracer().start_agent_span(
                agent_name="pipeline",
                operation="orchestrate",
                model=model_id if model_id is not None else "",
            )
            pipeline_span.attributes["goal"] = redact_text(goal)[:200]
            return pipeline_span
        except (ImportError, AttributeError):
            logger.warning("GenAI tracer unavailable for pipeline span")
            return None

    @staticmethod
    def _classify_intake(
        goal: str,
        context: dict[str, Any],
        stages: dict[str, Any],
    ) -> tuple[Any | None, Any | None]:
        """Classify request intake and populate context/stage metadata."""
        try:
            from vetinari.orchestration.intake import get_request_intake

            intake_tier, intake_features = get_request_intake().classify_with_features(goal, context)
            context["intake_tier"] = intake_tier.value
            context["intake_confidence"] = intake_features.confidence
            context["intake_pattern_key"] = intake_features.pattern_key
            stages["intake"] = {
                "tier": intake_tier.value,
                "confidence": intake_features.confidence,
                "word_count": intake_features.word_count,
                "cross_cutting": intake_features.cross_cutting_keywords,
            }
            logger.info(
                "[Pipeline] Stage 0: Intake classified as %s (confidence=%.2f)",
                intake_tier.value,
                intake_features.confidence,
            )
            return intake_tier, intake_features
        except Exception:
            logger.warning("Intake classification unavailable, proceeding with full pipeline", exc_info=True)
            return None, None

    @staticmethod
    def _queue_priority_for_tier(intake_tier: Any | None) -> int:
        """Map an optional intake tier to queue priority."""
        if intake_tier is None:
            return PRIORITY_STANDARD
        try:
            from vetinari.orchestration.intake import Tier

            tier_priority_map = {
                Tier.EXPRESS: PRIORITY_EXPRESS,
                Tier.STANDARD: PRIORITY_STANDARD,
                Tier.CUSTOM: PRIORITY_CUSTOM,
            }
            return tier_priority_map.get(intake_tier, PRIORITY_STANDARD)
        except Exception:
            logger.warning("Intake tier priority mapping failed", exc_info=True)
            return PRIORITY_STANDARD

    def _admit_request(
        self,
        goal: str,
        context: dict[str, Any],
        intake_tier: Any | None,
    ) -> tuple[str | None, dict[str, Any] | None]:
        """Enqueue and dequeue a request, returning an immediate response if not executable now."""
        owner = cast(_PipelineEntryOwner, self)
        if not hasattr(owner, "_request_queue"):
            owner._request_queue = RequestQueue()
        request_queue = owner._request_queue
        try:
            exec_id = request_queue.enqueue(goal, context, priority=self._queue_priority_for_tier(intake_tier))
        except QueueFullError:
            logger.warning("[Pipeline] Backpressure: request rejected (queue full)")
            return None, {
                "status": "rejected",
                "error": "too_many_requests",
                "message": "Server is at capacity. Please retry later.",
                "http_status": 429,
            }

        context["_exec_id"] = exec_id
        if request_queue.dequeue() is not None:
            return exec_id, None
        logger.info("[Pipeline] Request %s queued (at concurrency limit)", exec_id)
        return exec_id, {
            "status": "queued",
            "exec_id": exec_id,
            "queue_depth": request_queue.depth,
            "active_count": request_queue.active_count,
        }

    def generate_and_execute(
        self,
        goal: str,
        constraints: dict[str, Any] | None = None,
        task_handler: Callable[..., Any] | None = None,
        context: dict[str, Any] | None = None,
        project_id: str | None = None,
        model_id: str | None = None,
    ) -> dict[str, Any]:
        """Run the assembly-line pipeline for one user goal.

        Args:
            goal: Goal value consumed by generate_and_execute().
            constraints: Constraints value consumed by generate_and_execute().
            task_handler: Task handler value consumed by generate_and_execute().
            context: Context value consumed by generate_and_execute().
            project_id: Project identifier that scopes the operation.
            model_id: Model identifier used for routing or lookup.

        Returns:
            Value produced for the caller.
        """
        stages: dict[str, Any] = {}
        start_time = time.time()
        context = context or {}
        correlation_context = self._enter_correlation_context()
        pipeline_span = self._start_pipeline_span(goal, model_id)
        intake_tier, intake_features = self._classify_intake(goal, context, stages)
        exec_id, immediate_response = self._admit_request(goal, context, intake_tier)
        if immediate_response is not None:
            return immediate_response

        owner = cast(_PipelineEntryOwner, self)
        try:
            return owner._execute_pipeline(
                goal,
                constraints,
                context,
                stages,
                start_time,
                correlation_context,
                pipeline_span,
                intake_tier,
                intake_features,
                task_handler,
                project_id,
                model_id,
            )
        finally:
            owner._request_queue.complete(cast(str, exec_id))
