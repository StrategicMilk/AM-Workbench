"""Litestar lifespan hooks - startup and shutdown for background services."""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import AsyncGenerator
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import dataclass
from importlib.util import find_spec
from typing import Any, cast

from vetinari.boundary_guards import account_evidence_drop, assert_dependency_success

logger = logging.getLogger(__name__)


@dataclass
class _LifespanResources:
    health_task: Any | None = None
    scheduler: Any | None = None
    training_scheduler: Any | None = None
    learning_orchestrator: Any | None = None
    freshness_future: Future[Any] | None = None
    freshness_pool: ThreadPoolExecutor | None = None

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}("
            f"health_task={self.health_task!r}, "
            f"scheduler={self.scheduler!r}, "
            f"training_scheduler={self.training_scheduler!r}, "
            f"learning_orchestrator={self.learning_orchestrator!r}"
            ")"
        )


def _module_is_available(module_name: str) -> bool:
    """Return True when a startup dependency is discoverable without importing it."""
    if module_name in sys.modules:
        return sys.modules[module_name] is not None
    try:
        return find_spec(module_name) is not None
    except (ModuleNotFoundError, ValueError):
        logger.warning("Exception handled by  module is available fallback", exc_info=True)
        return False


def _record_startup_degradation(app: Any, key: str, reason: str) -> None:
    """Record startup degradations on app.state when the host exposes it."""
    state = getattr(app, "state", None)
    if state is None:
        return
    degradations = getattr(state, "startup_degradations", None)
    if not isinstance(degradations, dict):
        degradations = {}
        state.startup_degradations = degradations
    degradations[key] = reason


@asynccontextmanager
async def vetinari_lifespan(_app: Any) -> AsyncGenerator[None, None]:
    """Manage Vetinari background services during the server's lifetime."""
    resources = _startup_lifespan_services(_app)
    try:
        yield
    finally:
        _shutdown_lifespan_services(resources)


def _startup_lifespan_services(app: Any) -> _LifespanResources:
    logger.info("Lifespan: starting background services")
    resources = _LifespanResources()
    _wire_subsystems()
    _reseed_typed_sse_sequence()
    resources.health_task = _start_health_monitor()
    resources.scheduler = _start_periodic_scheduler(app)
    resources.training_scheduler = _start_training_scheduler()
    _purge_privacy_retention()
    resources.learning_orchestrator = _start_learning_orchestrator(app)
    resources.freshness_future, resources.freshness_pool = _start_model_freshness_check()
    return resources


def _wire_subsystems() -> None:
    try:
        from vetinari.cli_startup import _wire_subsystems as wire

        wire()
    except Exception as exc:
        logger.warning("Lifespan: subsystem wiring failed (non-fatal): %s", exc)


def _reseed_typed_sse_sequence() -> None:
    try:
        from vetinari.web.sse_events import reseed_sse_event_sequence_from_store

        reseed_sse_event_sequence_from_store()
    except Exception as exc:
        logger.warning("Lifespan: typed SSE sequence reseed skipped: %s", exc)


def _start_health_monitor() -> Any | None:
    try:
        from vetinari.system.health_monitor import start_health_monitor

        return start_health_monitor()
    except Exception as exc:
        logger.warning("Lifespan: health monitor not started: %s", exc)
        return None


def _start_periodic_scheduler(app: Any | None = None) -> Any | None:
    try:
        from apscheduler.schedulers.background import BackgroundScheduler

        scheduler = BackgroundScheduler(daemon=True)
        _add_kaizen_jobs(scheduler)
        _add_drift_job(scheduler, app)
        _add_temperature_job(scheduler, app)
        _add_privacy_retention_job(scheduler)
        scheduler.start()
        logger.info("Background scheduler started with %d periodic jobs", len(scheduler.get_jobs()))
        return scheduler
    except Exception as exc:
        logger.warning("APScheduler setup failed - periodic tasks will not run: %s", exc)
        return None


def _add_kaizen_jobs(scheduler: Any) -> None:
    try:
        if not _module_is_available("vetinari.kaizen.wiring"):
            raise ModuleNotFoundError("vetinari.kaizen.wiring")
        from vetinari.kaizen.wiring import scheduled_pdca_check, scheduled_regression_check

        _add_interval_job(scheduler, scheduled_pdca_check, hours=24, job_id="pdca_check")
        _add_interval_job(scheduler, scheduled_regression_check, hours=24, job_id="regression_check")
    except ModuleNotFoundError:
        logger.warning("Kaizen wiring not available - PDCA/regression scheduling skipped")


def _add_drift_job(scheduler: Any, app: Any | None = None) -> None:
    try:
        if not _module_is_available("vetinari.drift.wiring"):
            raise ModuleNotFoundError("vetinari.drift.wiring")
        from vetinari.drift.wiring import schedule_drift_audit

        _add_interval_job(scheduler, schedule_drift_audit, hours=6, job_id="drift_audit")
    except ModuleNotFoundError:
        _record_startup_degradation(app, "drift_audit", "vetinari.drift.wiring unavailable")
        logger.warning("Drift wiring not available - drift audit scheduling skipped")


def _add_temperature_job(scheduler: Any, app: Any | None = None) -> None:
    try:
        if not _module_is_available("vetinari.models.model_profiler_data"):
            raise ModuleNotFoundError("vetinari.models.model_profiler_data")
        from vetinari.models.model_profiler_data import update_learned_temperatures

        _add_interval_job(scheduler, update_learned_temperatures, hours=6, job_id="temperature_learning")
    except ModuleNotFoundError:
        _record_startup_degradation(app, "temperature_learning", "vetinari.models.model_profiler_data unavailable")
        logger.warning("Temperature learning not available - scheduling skipped")


def _add_privacy_retention_job(scheduler: Any) -> None:
    try:
        if not _module_is_available("vetinari.security.retention"):
            raise ModuleNotFoundError("vetinari.security.retention")
        from vetinari.security.retention import purge_privacy_retention_stores_30d

        _add_interval_job(scheduler, purge_privacy_retention_stores_30d, hours=24, job_id="privacy_retention_30d")
    except ModuleNotFoundError:
        logger.warning("Privacy retention helper not available - scheduling skipped")


def _add_interval_job(scheduler: Any, fn: Any, *, hours: int, job_id: str) -> None:
    scheduler.add_job(fn, "interval", hours=hours, id=job_id, misfire_grace_time=3600)


def _start_training_scheduler() -> Any | None:
    try:
        from vetinari.training.api_runtime import _get_scheduler

        training_scheduler = _get_scheduler()
        if training_scheduler is not None:
            training_scheduler.start()
            logger.info("TrainingScheduler started - idle-time learning enabled")
        return training_scheduler
    except Exception as exc:
        logger.warning("Lifespan: TrainingScheduler not started - idle-time training disabled: %s", exc)
        return None


def _purge_privacy_retention() -> None:
    try:
        from vetinari.security.retention import purge_privacy_retention_stores_30d

        purged = purge_privacy_retention_stores_30d()
        if sum(purged.values()):
            logger.info("Lifespan: privacy retention purge counts: %s", purged)
    except Exception as exc:
        logger.warning("Lifespan: privacy retention purge skipped: %s", exc)


def _start_learning_orchestrator(app: Any) -> Any | None:
    try:
        from vetinari.orchestration.variant_system import get_variant_manager

        variant_config = get_variant_manager().get_config()
        if not variant_config.enable_self_improvement:
            _record_learning_disabled(app, variant_config)
            return None
        from vetinari.learning.orchestrator import get_learning_orchestrator

        orchestrator = get_learning_orchestrator()
        orchestrator.start()
        logger.info("LearningOrchestrator started - self-improvement loop active")
        return orchestrator
    except Exception as exc:
        logger.warning("Lifespan: LearningOrchestrator not started - self-improvement disabled: %s", exc)
        return None


def _record_learning_disabled(app: Any, variant_config: Any) -> None:
    _record_startup_degradation(
        app,
        "learning_orchestrator",
        "enable_self_improvement is false for the active variant",
    )
    logger.info(
        "LearningOrchestrator skipped - enable_self_improvement=False for variant '%s'",
        getattr(variant_config, "level", "unknown"),
    )


def _start_model_freshness_check() -> tuple[Future[Any] | None, ThreadPoolExecutor | None]:
    try:
        from vetinari.models.model_scout import ModelFreshnessChecker

        checker = ModelFreshnessChecker()
        if not checker.should_check():
            return None, None
        pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="model-freshness")
        return pool.submit(_run_model_freshness_check, checker), pool
    except Exception as exc:
        logger.warning("Lifespan: model freshness check skipped: %s", exc)
        return None, None


def _run_model_freshness_check(checker: Any) -> None:
    try:
        upgrades = checker.check_for_upgrades()
        if upgrades:
            logger.info("Model freshness check: %d upgrade(s) available", len(upgrades))
            for upgrade in upgrades[:3]:
                logger.info(
                    "  Upgrade available: %s (score=%.2f, replaces %s)",
                    upgrade.candidate_name,
                    upgrade.overall_score,
                    upgrade.current_model_id,
                )
    except Exception as exc:
        logger.warning("Background model freshness check failed: %s", exc)


def _shutdown_lifespan_services(resources: _LifespanResources) -> None:
    logger.info("Lifespan: shutting down background services")
    _shutdown_freshness(resources)
    _shutdown_runtime_workers(resources)
    _shutdown_event_and_batches()
    _shutdown_runtime_resources()
    _stop_learning_services(resources)
    _run_registered_shutdown_callbacks()
    logger.info("Lifespan: shutdown complete")


def _shutdown_freshness(resources: _LifespanResources) -> None:
    if resources.freshness_future is not None:
        try:
            resources.freshness_future.result(timeout=5)
        except Exception:
            logger.warning("Lifespan: freshness check did not complete in time - shutdown will continue")
    if resources.freshness_pool is not None:
        resources.freshness_pool.shutdown(wait=False)  # noqa: leak-rule-3 -- freshness_future was already result()'d with a 5s timeout above; the worker thread is already done or will exit shortly. Blocking here would double the timeout budget.


def _shutdown_runtime_workers(resources: _LifespanResources) -> None:
    if resources.health_task is not None:
        _stop_health_monitor()
    _stop_model_warmups()
    _unload_gguf_models()
    _drain_watch_queue()
    _shutdown_notifications()


def _stop_health_monitor() -> None:
    try:
        from vetinari.system.health_monitor import stop_health_monitor

        stop_health_monitor()
    except Exception as exc:
        logger.warning("Lifespan: health monitor stop failed: %s", exc)


def _stop_model_warmups() -> None:
    try:
        from vetinari.models.model_pool import stop_all_model_warmups

        stop_all_model_warmups()
    except Exception as exc:
        logger.warning("Lifespan: model warm-up stop skipped: %s", exc)


def _unload_gguf_models() -> None:
    try:
        from vetinari.adapters.llama_cpp_model_cache import LlamaCppModelCache

        unload_all = getattr(LlamaCppModelCache, "unload_all", None)
        if callable(unload_all):
            try:
                unload_all()
            except Exception:
                logger.error("Lifespan: GGUF cache unload failed", exc_info=True)
                account_evidence_drop(
                    logger=logger,
                    evidence_ref="gguf_model_cache_unload",
                    reason="gguf_unload_failure",
                )
                raise
            assert_dependency_success(True, dependency_id="gguf_model_cache_unload")
    except Exception as exc:
        logger.warning("Lifespan: model unload skipped: %s", exc)


def _drain_watch_queue() -> None:
    try:
        from vetinari.watch import WatchMode

        get_instance = getattr(WatchMode, "get_instance", None)
        watch_mode = get_instance() if callable(get_instance) else None
        if watch_mode is not None:
            cast("Any", watch_mode).drain_queue()
    except Exception as exc:
        logger.warning("Lifespan: watch queue drain skipped: %s", exc)


def _shutdown_notifications() -> None:
    try:
        from vetinari.notifications.manager import reset_notification_manager

        reset_notification_manager()
    except Exception as exc:
        logger.warning("Lifespan: notification manager shutdown skipped: %s", exc)


def _shutdown_event_and_batches() -> None:
    _shutdown_event_bus()
    _reset_batch_processor()
    _export_prometheus_if_requested()
    _reset_telemetry_persistence()


def _shutdown_event_bus() -> None:
    try:
        from vetinari.events import get_event_bus

        bus = get_event_bus()
        if bus is not None:
            bus.shutdown()
    except Exception as exc:
        logger.warning("Lifespan: event bus shutdown skipped: %s", exc)


def _reset_batch_processor() -> None:
    try:
        from vetinari.adapters.batch_processor import reset_batch_processor

        reset_batch_processor()
    except Exception as exc:
        logger.warning("Lifespan: batch processor drain skipped: %s", exc)


def _export_prometheus_if_requested() -> None:
    prometheus_export_path = os.environ.get("VETINARI_PROMETHEUS_EXPORT_PATH")
    if not prometheus_export_path:
        return
    try:
        from vetinari.telemetry import get_telemetry_collector

        if not get_telemetry_collector().export_prometheus(prometheus_export_path):
            logger.error("Lifespan: Prometheus export failed for %s", prometheus_export_path)
    except Exception as exc:
        logger.error("Lifespan: Prometheus export failed for %s: %s", prometheus_export_path, exc)


def _reset_telemetry_persistence() -> None:
    try:
        from vetinari.analytics.telemetry_persistence import reset_telemetry_persistence

        reset_telemetry_persistence()
    except Exception as exc:
        logger.warning("Lifespan: telemetry persistence stop skipped: %s", exc)


def _shutdown_runtime_resources() -> None:
    _reset_worker_mcp_bridge()
    _reset_cascade_router()
    _reset_llm_guard_scanner()
    _clear_constraint_violations()


def _reset_worker_mcp_bridge() -> None:
    try:
        from vetinari.mcp.worker_bridge import reset_worker_mcp_bridge

        reset_worker_mcp_bridge()
        logger.info("Lifespan: Worker MCP bridge shut down")
    except Exception as exc:
        logger.warning("Lifespan: Worker MCP bridge shutdown failed -- subprocesses may linger: %s", exc)


def _reset_cascade_router() -> None:
    try:
        from vetinari.cascade_router import reset_cascade_router

        reset_cascade_router()
    except Exception as exc:
        logger.warning("Lifespan: cascade router reset skipped: %s", exc)


def _reset_llm_guard_scanner() -> None:
    try:
        from vetinari.safety.llm_guard_scanner import reset_llm_guard_scanner

        reset_llm_guard_scanner()
    except Exception as exc:
        logger.warning("Lifespan: LLM Guard scanner reset skipped: %s", exc)


def _clear_constraint_violations() -> None:
    try:
        from vetinari.constraints.registry import get_constraint_registry

        get_constraint_registry().clear_violations()
    except Exception as exc:
        logger.warning("Lifespan: constraint violation clear skipped: %s", exc)


def _stop_learning_services(resources: _LifespanResources) -> None:
    if resources.learning_orchestrator is not None:
        try:
            resources.learning_orchestrator.stop()
            logger.info("LearningOrchestrator stopped")
        except Exception as exc:
            logger.warning("Lifespan: LearningOrchestrator stop did not complete cleanly: %s", exc)
    _stop_training_scheduler(resources.training_scheduler)
    _stop_background_scheduler(resources.scheduler)


def _stop_training_scheduler(training_scheduler: Any | None) -> None:
    if training_scheduler is None:
        return
    try:
        training_scheduler.stop()
        logger.info("TrainingScheduler stopped")
    except Exception as exc:
        logger.warning("Lifespan: TrainingScheduler stop did not complete cleanly: %s", exc)


def _stop_background_scheduler(scheduler: Any | None) -> None:
    if scheduler is None:
        return
    try:
        scheduler.shutdown(wait=False)  # noqa: leak-rule-3 -- scheduler is APScheduler-style, not a concurrent.futures.Executor; wait=False is its documented fast-stop mode.
        logger.info("Background scheduler stopped")
    except Exception as exc:
        logger.warning("Background scheduler shutdown did not complete cleanly: %s", exc)


def _run_registered_shutdown_callbacks() -> None:
    try:
        from vetinari.shutdown import shutdown as run_registered_shutdown

        run_registered_shutdown()
    except Exception as exc:
        logger.warning("Lifespan: registered shutdown callbacks did not complete cleanly: %s", exc)
