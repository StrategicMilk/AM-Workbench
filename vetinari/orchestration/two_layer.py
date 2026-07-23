"""Two-Layer Orchestrator — thin facade combining all pipeline mixins.

Implements the assembly-line pattern:
  1. INPUT ANALYSIS   — classify and assess complexity
  2. PLAN GENERATION  — LLM-powered task decomposition
  3. TASK DECOMP      — recursive breakdown to atomic tasks
  4. MODEL ASSIGNMENT — intelligent model routing per task
  5. PARALLEL EXEC    — DAG-scheduled execution
  6. OUTPUT REVIEW    — consistency and quality check
  7. FINAL ASSEMBLY   — synthesize final output

Split layout:
    pipeline_engine.py       — PipelineExecutionEngine: generate_and_execute, _execute_pipeline, _emit
    pipeline_helpers.py      — PipelineSupportServices: agent routing, model selection, memory, analysis
    pipeline_stages.py       — PipelineStageRunner: execution stages 5-8 + telemetry
    pipeline_agent_graph.py  — PipelineAgentGraphRunner: execute_with_agent_graph
    pipeline_quality.py      — PipelineQualityController: prevention gate, review, assembly, corrections
    pipeline_rework.py       — PipelineReworkController: quality rejection, rework decisions
    two_layer.py              — FactoryPipelineOrchestrator (this file): __init__, Andon, singleton
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any

from vetinari.adapters.adapter_cache import get_local_inference_adapter
from vetinari.orchestration.durable_execution import DurableExecutionEngine
from vetinari.orchestration.execution_graph import ExecutionGraph
from vetinari.orchestration.express_path import ExpressPathExecution
from vetinari.orchestration.pipeline_agent_graph import PipelineAgentGraphRunner
from vetinari.orchestration.pipeline_engine import PipelineExecutionEngine
from vetinari.orchestration.pipeline_events import (
    NullEventHandler,
    PipelineEventHandler,
)
from vetinari.orchestration.pipeline_helpers import PipelineSupportServices
from vetinari.orchestration.pipeline_quality import PipelineQualityController
from vetinari.orchestration.pipeline_rework import ReworkDecision
from vetinari.orchestration.pipeline_stages import PipelineStageRunner
from vetinari.orchestration.plan_generator import PlanGenerator
from vetinari.orchestration.request_routing import (
    RequestQueue,
)
from vetinari.orchestration.variant_system import VariantManager, get_variant_manager
from vetinari.structured_logging import CorrelationContext
from vetinari.types import AgentType
from vetinari.workflow import AndonSignal, AndonSystem, get_andon_system

logger = logging.getLogger(__name__)


# Re-export for tests that patch tl.ReworkDecision
__all__ = [
    "FactoryPipelineOrchestrator",
    "ReworkDecision",
    "TwoLayerOrchestrator",
    "get_two_layer_orchestrator",
    "init_two_layer_orchestrator",
]


class FactoryPipelineOrchestrator(
    PipelineExecutionEngine,
    PipelineSupportServices,
    PipelineQualityController,
    PipelineStageRunner,
    PipelineAgentGraphRunner,
    ExpressPathExecution,
):
    """Complete Foreman -> Worker -> Inspector factory-pipeline orchestrator.

    Combines:
    - Layer 1: Graph-Based Planning (PlanGenerator)
    - Layer 2: Durable Execution (DurableExecutionEngine)

    All pipeline stages are implemented in the pipeline_* mixin modules.
    This class owns the constructor, Andon integration, and simple
    delegation properties.
    """

    # v0.5.0: 3 factory-pipeline agents + string aliases redirected
    _AGENT_MODULE_MAP = {
        AgentType.FOREMAN.value: ("vetinari.agents", "get_foreman_agent"),
        AgentType.WORKER.value: ("vetinari.agents", "get_worker_agent"),
        AgentType.INSPECTOR.value: ("vetinari.agents", "get_inspector_agent"),
    }

    def __init__(
        self,
        checkpoint_dir: str | None = None,
        max_concurrent: int = 4,
        model_router: Any = None,
        agent_context: dict[str, Any] | None = None,
        enable_correction_loop: bool = True,
        correction_loop_max_rounds: int = 3,
        variant_manager: VariantManager | None = None,
        event_handler: PipelineEventHandler | None = None,
    ):
        # Side effects:
        #   - Registers _on_andon_trigger with AndonSystem (fires on quality alerts)
        #   - Creates background AsyncExecutor thread pool for parallel task waves
        #   - Creates ArchitectExecutorPipeline if available (2-stage planning)
        self._event_handler: PipelineEventHandler = event_handler or NullEventHandler()
        self._variant_manager: VariantManager = variant_manager or get_variant_manager()
        self.plan_generator = PlanGenerator(model_router)
        self.execution_engine = DurableExecutionEngine(
            checkpoint_dir=checkpoint_dir,
            max_concurrent=max_concurrent,
        )
        self.model_router = model_router
        self.agent_context: dict[str, Any] = agent_context or {}
        self._agents: dict[str, Any] = {}
        # Bug #15a: gate correction loop on VariantConfig.enable_verification so
        # LOW variant (enable_verification=False) never runs the correction loop
        # even when the caller passes enable_correction_loop=True.
        _variant_enables_verification = self._variant_manager.get_config().enable_verification
        self.enable_correction_loop: bool = enable_correction_loop and _variant_enables_verification
        self.correction_loop_max_rounds: int = correction_loop_max_rounds
        self._request_queue = RequestQueue(max_concurrent=max_concurrent)
        self._adapter = get_local_inference_adapter()
        self._andon = get_andon_system()
        self._andon.register_callback(self._on_andon_trigger)
        self._paused: bool = False
        # CorrelationContext factory stored for use in pipeline methods so each
        # orchestrate() call can wrap its full span in a single trace_id.
        self._correlation_context_cls = CorrelationContext

        # ── AsyncExecutor for wave-based parallel task execution ──
        self._async_executor = None
        try:
            from vetinari.async_support import AsyncExecutor
            from vetinari.constants import SANDBOX_TIMEOUT

            self._async_executor = AsyncExecutor(
                task_timeout=SANDBOX_TIMEOUT,
                fail_fast=False,
            )
            logger.info("AsyncExecutor wired for wave-based parallel execution")
        except (ImportError, AttributeError):
            logger.warning("AsyncExecutor unavailable — using sequential execution")

        # Architect-Executor pipeline (2-stage planning strategy)
        self._architect_pipeline = None
        try:
            from vetinari.orchestration.architect_executor import ArchitectExecutorPipeline

            self._architect_pipeline = ArchitectExecutorPipeline(
                config=agent_context.get("architect_config") if agent_context else None,
            )
            logger.info("ArchitectExecutorPipeline registered as execution strategy")
        except (ImportError, AttributeError):
            logger.warning("ArchitectExecutorPipeline unavailable")

        logger.info("FactoryPipelineOrchestrator initialized (assembly-line mode)")

    # ── Andon integration ────────────────────────────────────────────────────

    def shutdown(self) -> None:
        """Deregister this orchestrator's callbacks and release execution resources.

        Call this before replacing the singleton with a new instance so the
        shared :class:`~vetinari.workflow.AndonSystem` does not accumulate
        stale callbacks from discarded orchestrators.

        Side effects:
          - Deregisters ``_on_andon_trigger`` from the shared AndonSystem.
          - Shuts down ``_async_executor`` if one was initialised (best-effort,
            ``wait=False``).
        """
        self._andon.deregister_callback(self._on_andon_trigger)
        if self._async_executor is not None:
            try:
                # AsyncExecutor exposes shutdown(); fall back to pool if missing.
                if hasattr(self._async_executor, "shutdown"):
                    self._async_executor.shutdown(wait=False)  # noqa: leak-rule-3 -- orchestrator teardown is best-effort fast shutdown; blocking on a stuck async task could hang Andon halt sequence.
                elif hasattr(self._async_executor, "_pool") and hasattr(self._async_executor._pool, "shutdown"):
                    self._async_executor._pool.shutdown(wait=False)  # noqa: leak-rule-3 -- fallback path when AsyncExecutor lacks shutdown(); same rationale as above — fast teardown over blocking.
            except Exception:
                logger.warning(
                    "AsyncExecutor shutdown failed during FactoryPipelineOrchestrator teardown"
                    " — resources may not be fully released"
                )

    def _on_andon_trigger(self, signal: AndonSignal) -> None:
        """Handle Andon signal — halt on critical/emergency, warn on warning.

        Args:
            signal: The AndonSignal that was raised.
        """
        if signal.severity in ("critical", "emergency"):
            logger.critical("ANDON HALT: %s — %s", signal.source, signal.message)
            self._halt_pipeline(signal)
        elif signal.severity == "warning":
            logger.warning(
                "ANDON WARNING: %s — %s (continuing with caution)",
                signal.source,
                signal.message,
            )

    def _halt_pipeline(self, signal: AndonSignal) -> None:
        """Stop pipeline execution and save state for later resume.

        Args:
            signal: The AndonSignal that triggered the halt.
        """
        self._paused = True
        logger.critical("Pipeline halted by Andon: %s", signal.message)

    def is_paused(self) -> bool:
        """Return True if the pipeline is paused due to an Andon trigger.

        Returns:
            True when the pipeline is paused and should not execute.
        """
        return getattr(self, "_paused", False) or getattr(self, "_andon", get_andon_system()).is_paused()

    def resume(self) -> bool:
        """Resume pipeline after Andon halt.

        Acknowledges all active Andon signals and clears the local pause flag.

        Returns:
            True if the pipeline was paused and is now resumed, False otherwise.
        """
        if not self._paused and not self._andon.is_paused():
            logger.info("Pipeline is not paused — nothing to resume")
            return False
        for i, signal in enumerate(self._andon.get_all_signals()):
            if not signal.acknowledged:
                self._andon.acknowledge(i)
        self._paused = False
        logger.info("Pipeline resumed after Andon halt")
        return True

    @property
    def andon(self) -> AndonSystem:
        """Access the Andon system for signal management.

        Returns:
            The AndonSystem instance owned by this orchestrator.
        """
        return self._andon

    # ── Convenience delegation to execution engine ───────────────────────────

    def recover_incomplete_on_startup(self) -> list[dict[str, Any]]:
        """Check for incomplete checkpoints and resume them on application startup.

        Delegates to ``DurableExecutionEngine.recover_incomplete_executions``
        using the default task handler. Intended to be called once during startup.

        Returns:
            List of per-execution result dicts, empty when nothing needs recovery.
        """
        handler = self._make_default_handler()
        return self.execution_engine.recover_incomplete_executions(task_handler=handler)

    def generate_plan_only(
        self,
        goal: str,
        constraints: dict[str, Any] | None = None,
    ) -> ExecutionGraph:
        """Generate a plan without executing it.

        Args:
            goal: The goal to plan for.
            constraints: Optional constraints on the plan.

        Returns:
            An ExecutionGraph representing the planned tasks.
        """
        return self.plan_generator.generate_plan(
            goal,
            constraints,
            max_depth=self._variant_manager.get_config().max_planning_depth,
        )

    def execute_plan(
        self,
        graph: ExecutionGraph,
        task_handler: Callable | None = None,
    ) -> dict[str, Any]:
        """Execute an existing plan via the durable execution engine.

        Args:
            graph: An already-generated ExecutionGraph to run.
            task_handler: Optional task handler override.

        Returns:
            Execution result dict from the durable engine.
        """
        return self.execution_engine.execute_plan(graph, task_handler)

    def recover_plan(self, plan_id: str) -> dict[str, Any]:
        """Recover and continue a plan from checkpoint.

        Args:
            plan_id: Identifier of the plan checkpoint to recover.

        Returns:
            Execution result dict from the recovered execution.
        """
        return self.execution_engine.recover_execution(plan_id)

    def get_plan_status(self, plan_id: str) -> dict[str, Any] | None:
        """Get status of a plan by its identifier.

        Args:
            plan_id: Identifier of the plan to query.

        Returns:
            Status dict, or None if no plan with that ID exists.
        """
        return self.execution_engine.get_execution_status(plan_id)

    def list_checkpoints(self) -> list[str]:
        """List all available plan checkpoints.

        Returns:
            List of checkpoint identifiers that can be recovered.
        """
        return self.execution_engine.list_checkpoints()


# ---------------------------------------------------------------------------
# Global singleton
# ---------------------------------------------------------------------------

_two_layer_orchestrator: FactoryPipelineOrchestrator | None = None
_two_layer_orchestrator_lock = threading.Lock()


def get_two_layer_orchestrator() -> FactoryPipelineOrchestrator:
    """Return the module-level singleton FactoryPipelineOrchestrator.

    Creates it with default settings on first call. Subsequent calls return the
    same instance — never create a new orchestrator per task.

    Returns:
        The shared FactoryPipelineOrchestrator singleton.
    """
    global _two_layer_orchestrator
    if _two_layer_orchestrator is None:
        with _two_layer_orchestrator_lock:
            if _two_layer_orchestrator is None:
                _two_layer_orchestrator = FactoryPipelineOrchestrator()
    return _two_layer_orchestrator


def init_two_layer_orchestrator(checkpoint_dir: str | None = None, **kwargs) -> FactoryPipelineOrchestrator:
    """Initialize a new two-layer orchestrator, replacing the singleton.

    Shuts down the previous singleton (if any) before constructing the new one
    so that Andon callbacks from the old instance are deregistered and its
    execution resources are released.  Useful for tests or when a custom
    checkpoint directory is required.

    Args:
        checkpoint_dir: Directory for durable execution checkpoints.
        **kwargs: Additional constructor arguments passed to FactoryPipelineOrchestrator.

    Returns:
        A freshly created FactoryPipelineOrchestrator that replaces the singleton.
    """
    global _two_layer_orchestrator
    with _two_layer_orchestrator_lock:
        old = _two_layer_orchestrator
        if old is not None:
            try:
                old.shutdown()
            except Exception:
                logger.warning(
                    "Previous FactoryPipelineOrchestrator shutdown failed during reinit — proceeding with new instance",
                    exc_info=True,
                )
        _two_layer_orchestrator = FactoryPipelineOrchestrator(checkpoint_dir=checkpoint_dir, **kwargs)
    return _two_layer_orchestrator


# Backwards-compatible public alias. The primary implementation class is named
# for the ADR-0061 Foreman -> Worker -> Inspector factory pipeline.
TwoLayerOrchestrator = FactoryPipelineOrchestrator
