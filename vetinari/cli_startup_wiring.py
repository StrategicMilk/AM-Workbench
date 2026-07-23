"""Startup wiring helpers re-exported by :mod:`vetinari.cli_startup`."""

from __future__ import annotations

import importlib
import inspect
import logging
from typing import Any

from vetinari.boundary_guards import require_score_in_range
from vetinari.types import StatusEnum

logger = logging.getLogger("vetinari.cli_startup")


# ---------------------------------------------------------------------------
# Skill modules that expose Tool subclasses for auto-registration.
# Written here: _wire_skills_to_registry reads it.
# Read by: _wire_skills_to_registry in this module.
# ---------------------------------------------------------------------------
_SKILL_MODULES: list[str] = [
    "vetinari.tools.file_tool",
    "vetinari.tools.git_tool",
    "vetinari.tools.web_search_tool",
    "vetinari.tools.brave_search_tool",
    "vetinari.tools.tool_registry_integration",
]


def _instantiate_tool_class(tool_class: type[object], module_name: str) -> object | None:
    """Create a Tool instance, using scoped factories for sandboxed tools.

    File and git tools require an explicit project/repository root so agents
    cannot inherit an unsafe process cwd.  Their existing factories provide
    that scope; other Tool subclasses keep the normal no-argument constructor
    path used by the auto-registration scanner.

    Args:
        tool_class: Candidate Tool subclass discovered by module scanning.
        module_name: Module where the class was discovered.

    Returns:
        A Tool-compatible object, or None when a scoped factory reports the
        tool is unavailable in the current runtime.
    """
    if module_name == "vetinari.tools.file_tool" and tool_class.__name__ == "FileOperationsTool":
        from vetinari.tools.tool_registry_integration import _make_file_tool

        return _make_file_tool()
    if module_name == "vetinari.tools.git_tool" and tool_class.__name__ == "GitOperationsTool":
        from vetinari.tools.tool_registry_integration import _make_git_tool

        return _make_git_tool()
    return tool_class()


def _wire_tracing_instrumentation() -> None:
    """Activate Vetinari tracing instrumentation from the shared runtime startup path."""
    try:
        from vetinari.observability.tracing import VetinariInstrumentor

        VetinariInstrumentor().instrument()
        logger.info("Wiring: tracing instrumentation OK")
    except Exception as exc:
        logger.warning("Wiring: tracing instrumentation failed: %s", exc)


def _wire_alert_evaluation() -> None:
    """Run one alert evaluation cycle from startup so registered thresholds are live."""
    try:
        from vetinari.dashboard.alerts import get_alert_engine

        fired = get_alert_engine().evaluate_all()
        logger.info("Wiring: alert evaluation OK (%d alert(s) fired)", len(fired))
    except Exception as exc:
        logger.warning("Wiring: alert evaluation failed: %s", exc)


def _wire_autonomy_and_notifications() -> None:
    """Initialize the autonomy governor, approval queue, and notification channels."""
    try:
        from vetinari.autonomy.wiring import wire_autonomy_and_notifications

        wire_autonomy_and_notifications()
    except Exception as exc:
        logger.warning("Wiring: autonomy/notifications failed: %s", exc)


def _wire_learning_to_dashboard() -> None:
    """Ensure the learning API handlers are importable and ready."""
    try:
        from vetinari.learning.orchestrator import get_learning_orchestrator

        orchestrator = get_learning_orchestrator()
        if not orchestrator.is_running():
            orchestrator.start()
        logger.info("Wiring: learning -> dashboard OK")
    except Exception as exc:
        logger.warning("Wiring: learning -> dashboard failed: %s", exc)


def _wire_drift_to_orchestration() -> None:
    """Connect drift monitor into orchestration cycle.

    Verifies the drift monitor singleton is importable and initialised so that
    orchestration code can call ``get_drift_monitor()`` safely at runtime.
    """
    try:
        from vetinari.drift.monitor import get_drift_monitor

        monitor = get_drift_monitor()
        monitor.bootstrap()
        logger.info("Wiring: drift -> orchestration OK (bootstrap complete)")
    except Exception as exc:
        logger.warning("Wiring: drift -> orchestration failed: %s", exc)


def _wire_analytics_to_dashboard() -> None:
    """Ensure the analytics API handlers are importable and ready."""
    try:
        from vetinari.analytics.wiring import record_periodic_metrics
        from vetinari.dashboard.api import get_dashboard_api

        api = get_dashboard_api()
        api.get_stats()
        record_periodic_metrics(0.0, 0.0, 0)
        logger.info("Wiring: analytics -> dashboard OK")
    except Exception as exc:
        logger.warning("Wiring: analytics -> dashboard failed: %s", exc)


def _wire_security_to_verification() -> None:
    """Ensure SecurityVerifier is present in the verification pipeline.

    The VerificationPipeline already creates a SecurityVerifier for BASIC+
    levels.  This step confirms the wiring is intact; if missing (e.g. NONE
    level) it re-adds the built-in SecurityVerifier.
    """
    try:
        from vetinari.security import get_secret_scanner
        from vetinari.validation import (
            SecurityVerifier as _SecurityVerifier,
        )
        from vetinari.validation import (
            get_verifier_pipeline,
        )

        pipeline = get_verifier_pipeline()
        scanner = get_secret_scanner()

        has_security = any(v.name == "security" for v in pipeline.verifiers)
        if not has_security:
            pipeline.add_verifier(_SecurityVerifier())
            logger.info("Wiring: added missing SecurityVerifier to pipeline")

        # Confirm the scanner is operational
        scanner.scan("test")

        logger.info("Wiring: security -> verification OK")
    except Exception as exc:
        logger.warning("Wiring: security -> verification failed: %s", exc)


def _wire_skills_to_registry() -> None:
    """Auto-register all skill Tool subclasses into the global ToolRegistry.

    Scans each module listed in ``_SKILL_MODULES`` for concrete Tool subclasses
    and registers them if not already present (idempotent).
    """
    try:
        from vetinari.tool_interface import Tool, get_tool_registry

        registry = get_tool_registry()
        registered: list[str] = []

        for mod_name in _SKILL_MODULES:
            try:
                mod = importlib.import_module(mod_name)
                for _attr_name, attr_value in inspect.getmembers(mod, inspect.isclass):
                    if issubclass(attr_value, Tool) and attr_value is not Tool and not inspect.isabstract(attr_value):
                        try:
                            instance = _instantiate_tool_class(attr_value, mod_name)
                            if instance is None:
                                continue
                            if not isinstance(instance, Tool):
                                logger.warning(
                                    "Could not register %s from %s: factory returned %s instead of Tool",
                                    _attr_name,
                                    mod_name,
                                    type(instance).__name__,
                                )
                                continue
                            if registry.get(instance.metadata.name) is None:
                                registry.register(instance)
                                registered.append(instance.metadata.name)
                        except Exception as inst_err:
                            logger.warning(
                                "Could not instantiate %s from %s: %s",
                                _attr_name,
                                mod_name,
                                inst_err,
                            )
            except Exception as mod_err:
                logger.warning("Could not import skill module %s: %s", mod_name, mod_err)

        logger.info("Wiring: skills -> registry OK (%d skills registered)", len(registered))
    except Exception as exc:
        logger.warning("Wiring: skills -> registry failed: %s", exc)


def _wire_durable_recovery() -> None:
    """Resume incomplete executions from durable checkpoints on startup.

    Queries the DurableExecutionEngine's SQLite store for any execution that
    was interrupted (neither completed nor failed) and resumes it.  Runs in
    the startup thread — each recovered execution logs its own outcome.
    """
    try:
        from vetinari.orchestration.two_layer import get_two_layer_orchestrator

        orch = get_two_layer_orchestrator()
        recovered = orch.recover_incomplete_on_startup()
        if recovered:
            logger.info(
                "Wiring: durable recovery OK — resumed %d execution(s)",
                len(recovered),
            )
        else:
            logger.info("Wiring: durable recovery OK — no incomplete executions")
    except Exception as exc:
        logger.warning("Wiring: durable recovery failed: %s", exc)


def _wire_sse_event_cleanup() -> None:
    """Register SSE event log cleanup as a shutdown callback.

    Ensures stale SSE audit log rows (older than 24 hours) are purged
    when the process shuts down, preventing unbounded table growth.
    """
    try:
        from vetinari.shutdown import register_callback
        from vetinari.web.sse_events import cleanup_stale_sse_events

        register_callback("SSE event log cleanup", cleanup_stale_sse_events)
        logger.info("Wiring: SSE event log cleanup -> shutdown OK")
    except Exception as exc:
        logger.warning("Wiring: SSE event log cleanup failed: %s", exc)


def _wire_retraining_subscribers(bus: Any, events: Any) -> int:
    wired = 0
    try:
        from vetinari.training.agent_trainer import get_agent_trainer

        bus.subscribe(events.RetrainingRecommended, get_agent_trainer().record_retraining_signal)
        wired += 1
        logger.debug("EventBus: RetrainingRecommended -> AgentTrainer")
    except Exception as exc:
        logger.warning("EventBus: failed to wire RetrainingRecommended: %s", exc)
    try:
        from vetinari.learning.training_manager import get_training_manager

        tmgr = get_training_manager()

        def _on_retraining_recommended_tm(event: Any) -> None:
            recommendation = tmgr.should_retrain(model_id=event.metric or "default", task_type="general")
            if recommendation.recommended:
                logger.info(
                    "[EventBus] TrainingManager recommends retraining: %s (degradation=%.2f)",
                    recommendation.reason,
                    recommendation.degradation,
                )

        bus.subscribe(events.RetrainingRecommended, _on_retraining_recommended_tm)
        wired += 1
        logger.debug("EventBus: RetrainingRecommended -> TrainingManager.should_retrain()")
    except Exception as exc:
        logger.warning("EventBus: failed to wire drift -> TrainingManager: %s", exc)
    return wired


def _wire_anomaly_alert_subscriber(bus: Any, events: Any) -> int:
    try:
        from vetinari.dashboard.alerts import get_alert_engine

        bus.subscribe(events.AnomalyDetected, get_alert_engine().evaluate_anomaly)
        logger.debug("EventBus: AnomalyDetected -> AlertEngine")
        return 1
    except Exception as exc:
        logger.warning("EventBus: failed to wire AnomalyDetected: %s", exc)
        return 0


def _wire_quality_gate_subscriber(bus: Any, events: Any) -> int:
    try:
        from vetinari.workflow import get_spc_monitor

        spc = get_spc_monitor()

        def _on_quality_gate(event: Any) -> None:
            spc.update("quality_score", event.score)

        bus.subscribe(events.QualityGateResult, _on_quality_gate)
        logger.debug("EventBus: QualityGateResult -> SPCMonitor")
        return 1
    except Exception as exc:
        logger.warning("EventBus: failed to wire QualityGateResult: %s", exc)
        return 0


def _wire_task_completed_learning_subscriber(bus: Any, events: Any) -> int:
    try:
        from vetinari.learning.feedback_loop import get_feedback_loop

        feedback_loop = get_feedback_loop()

        def _on_task_completed(event: Any) -> None:
            quality_score = getattr(event, "quality_score", None)
            if quality_score is None:
                quality_score = 1.0 if event.success else 0.0
            quality_score = require_score_in_range(quality_score, field_name="quality_score")
            feedback_loop.record_outcome(
                task_id=event.task_id,
                task_type=getattr(event, "task_type", "") or event.agent_type or "general",
                quality_score=quality_score,
                model_id=getattr(event, "model_id", "") or "unknown",
                latency_ms=max(0, int(getattr(event, "duration_ms", 0) or 0)),
                success=bool(event.success),
            )
            try:
                from vetinari.learning.training_data import check_training_data_ready

                readiness = check_training_data_ready()
                if readiness.get(StatusEnum.READY.value, False):
                    logger.info(
                        "[EventBus] Training data watermark reached - %d records available",
                        readiness.get("total_records", 0),
                    )
            except Exception:
                logger.warning("Training data readiness check failed", exc_info=True)

        bus.subscribe(events.TaskCompleted, _on_task_completed)
        logger.debug("EventBus: TaskCompleted -> FeedbackLoop + training telemetry")
        return 1
    except Exception as exc:
        logger.warning("EventBus: failed to wire TaskCompleted: %s", exc)
        return 0


def _wire_kaizen_subscribers(bus: Any, events: Any) -> int:
    try:
        from vetinari.structured_logging import log_event as sl_log_event

        def _on_kaizen_proposed(event: Any) -> None:
            sl_log_event(
                "info",
                "vetinari.kaizen",
                "improvement_proposed",
                improvement_id=event.improvement_id,
                metric=event.metric,
                hypothesis=event.hypothesis,
                applied_by=event.applied_by,
            )

        def _on_kaizen_confirmed(event: Any) -> None:
            sl_log_event(
                "info",
                "vetinari.kaizen",
                "improvement_confirmed",
                improvement_id=event.improvement_id,
                metric=event.metric,
                baseline_value=event.baseline_value,
                actual_value=event.actual_value,
                applied_by=event.applied_by,
            )

        def _on_kaizen_reverted(event: Any) -> None:
            sl_log_event(
                "warning",
                "vetinari.kaizen",
                "improvement_reverted",
                improvement_id=event.improvement_id,
                metric=event.metric,
                reason=event.reason,
            )

        bus.subscribe(events.KaizenImprovementProposed, _on_kaizen_proposed)
        bus.subscribe(events.KaizenImprovementConfirmed, _on_kaizen_confirmed)
        bus.subscribe(events.KaizenImprovementReverted, _on_kaizen_reverted)
        logger.debug("EventBus: Kaizen events -> improvement tracking")
        return 3
    except Exception as exc:
        logger.warning("EventBus: failed to wire Kaizen events: %s", exc)
        return 0


def _wire_timing_record_subscriber(bus: Any, events: Any) -> int:
    try:
        from vetinari.analytics.value_stream import get_value_stream_analyzer

        vsm = get_value_stream_analyzer()

        def _on_timing_record(event: Any) -> None:
            vsm.record_event(
                execution_id=event.execution_id,
                task_id=event.task_id,
                agent_type=event.agent_type,
                timing_event=event.timing_event,
                metadata=event.metadata,
            )

        bus.subscribe(events.TaskTimingRecord, _on_timing_record)
        logger.debug("EventBus: TaskTimingRecord -> ValueStreamAnalyzer")
        return 1
    except Exception as exc:
        logger.warning("EventBus: failed to wire TaskTimingRecord: %s", exc)
        return 0


def _wire_quality_drift_subscriber(bus: Any, events: Any) -> int:
    try:

        def _on_quality_drift(event: Any) -> None:
            logger.warning(
                "Quality drift detected: task_type=%s, detectors=%s, observations=%d",
                event.task_type or "all",
                ", ".join(event.triggered_detectors),
                event.observation_count,
            )
            try:
                from vetinari.learning.quality_scorer import get_quality_scorer

                scorer = get_quality_scorer()
                old_interval = scorer._calibration_interval
                scorer._calibration_interval = max(2, old_interval // 2)
                logger.info(
                    "Quality drift response: calibration interval %d -> %d for faster LLM checks",
                    old_interval,
                    scorer._calibration_interval,
                )
            except Exception:
                logger.warning("Could not adjust calibration frequency after drift detection")
            try:
                from vetinari.web.shared import _push_sse_event

                _push_sse_event(
                    "_system",
                    "quality_drift",
                    {
                        "task_type": event.task_type or "all",
                        "detectors": event.triggered_detectors,
                        "observation_count": event.observation_count,
                    },
                )
            except Exception:
                logger.warning("Could not push SSE event for quality drift notification")

        bus.subscribe(events.QualityDriftDetected, _on_quality_drift)
        logger.debug("EventBus: QualityDriftDetected -> calibration + SSE")
        return 1
    except Exception as exc:
        logger.warning("EventBus: failed to wire QualityDriftDetected: %s", exc)
        return 0


def _wire_task_completed_anomaly_subscriber(bus: Any, events: Any) -> int:
    try:
        from vetinari.analytics.anomaly import get_anomaly_detector

        anomaly_detector = get_anomaly_detector()

        def _on_task_completed_anomaly(event: Any) -> None:
            if event.duration_ms > 0:
                result = anomaly_detector.detect("task.duration_ms", event.duration_ms)
                if result.is_anomaly:
                    logger.warning(
                        "[AnomalyDetector] Task %s duration anomaly: %.1fms (%s)",
                        event.task_id,
                        event.duration_ms,
                        result.reason,
                    )

        bus.subscribe(events.TaskCompleted, _on_task_completed_anomaly)
        logger.debug("EventBus: TaskCompleted -> AnomalyDetector (latency)")
        return 1
    except Exception as exc:
        logger.warning("EventBus: failed to wire AnomalyDetector: %s", exc)
        return 0


def _wire_event_subscribers() -> None:
    """Register domain-specific EventBus subscribers at startup."""
    try:
        events = importlib.import_module("vetinari.events")
        bus = events.get_event_bus()
        wire_steps = (
            _wire_retraining_subscribers,
            _wire_anomaly_alert_subscriber,
            _wire_quality_gate_subscriber,
            _wire_task_completed_learning_subscriber,
            _wire_kaizen_subscribers,
            _wire_timing_record_subscriber,
            _wire_quality_drift_subscriber,
            _wire_task_completed_anomaly_subscriber,
        )
        wired = sum(step(bus, events) for step in wire_steps)
        logger.info("Wiring: EventBus subscribers OK - %d subscriber(s) registered", wired)
    except Exception as exc:
        logger.warning("Wiring: EventBus subscribers failed: %s", exc)


def _wire_telemetry_persistence() -> None:
    """Start the TelemetryPersistence background flush loop at startup.

    TelemetryPersistence batches telemetry records in memory and flushes
    them to SQLite periodically. Without calling start(), the flush loop
    never begins and records accumulate without being persisted.

    Also restores the most recent snapshot into the in-memory collector so
    that counters survive process restarts.
    """
    try:
        from vetinari.telemetry import get_telemetry_collector

        get_telemetry_collector().restore_from_snapshot()
        logger.info("Wiring: TelemetryCollector snapshot restore complete")
    except Exception as exc:
        logger.warning("Wiring: TelemetryCollector snapshot restore failed (non-fatal): %s", exc)

    try:
        from vetinari.analytics.telemetry_persistence import get_telemetry_persistence

        get_telemetry_persistence().start()
        logger.info("Wiring: TelemetryPersistence -> started OK")
    except Exception as exc:
        logger.warning("Wiring: TelemetryPersistence start failed: %s", exc)
