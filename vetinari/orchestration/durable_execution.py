"""Durable Execution Engine — Layer 2 of the Two-Layer Orchestration System.

Checkpoint-based execution for long-running plans.

When to use this module:
    Use ``DurableExecutionEngine`` when a plan must survive crashes and be
    resumable.  Every task transition is written to SQLite before it happens,
    enabling deterministic replay on restart.  This is the right execution path
    for plans that span minutes or hours, or that must not repeat work already
    completed.

Pipeline role: Plan → **DurableExecution** (checkpoint) → Verify → Learn.
Compare with ``pipeline_engine.py`` (in-memory, no persistence) and
``async_executor.py`` (async wrapper for wave-based plans).

Database types and the SQLite wrapper live in ``durable_db`` and are
re-exported here so existing callers do not need to change their imports.
"""

from __future__ import annotations

import concurrent.futures
import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from pathlib import Path

import vetinari.resilience as resilience
from vetinari.constants import _PROJECT_ROOT
from vetinari.orchestration.checkpoint_store import Checkpoint, CheckpointStore, ExecutionEvent
from vetinari.orchestration.durable_db import CheckpointSnapshot, ExecutionEventRecord, _DatabaseManager
from vetinari.orchestration.durable_execution_adapter import _DurableExecutionAdapterSupport
from vetinari.orchestration.durable_execution_recovery_mixin import _DurableExecutionRecoveryMixin
from vetinari.orchestration.durable_execution_runtime import _DurableExecutionRuntimeMixin
from vetinari.orchestration.execution_graph import ExecutionGraph
from vetinari.orchestration.graph_types import CycleDetector, HumanCheckpoint
from vetinari.receipts.store import WorkReceiptStore
from vetinari.workbench.durable_workflow_adapter import (
    _ADAPTER_REGISTRY_LOCK,
    DurableWorkflowAdapter,
    WorkflowAdapterError,
)

logger = logging.getLogger(__name__)


_CIRCUIT_BREAKER_CLS = getattr(resilience, "CircuitBreaker", None)


__all__ = [
    "Checkpoint",
    "CheckpointSnapshot",
    "DurableExecutionEngine",
    "ExecutionEvent",
    "ExecutionEventRecord",
]


class DurableExecutionEngine(
    _DurableExecutionRuntimeMixin,
    _DurableExecutionRecoveryMixin,
    _DurableExecutionAdapterSupport,
):
    """Durable execution engine inspired by Temporal.

    Features:
    - State persistence with SQLite + WAL (crash-safe, atomic)
    - Retry policies with exponential backoff and jitter
    - Event sourcing via execution_events table
    - Crash recovery via checkpoint resume
    - Deterministic replay
    - Pause/resume for user clarification questions
    - Circuit breaker to prevent cascading failures
    - Cycle detection to prevent infinite retry loops
    """

    def __init__(
        self,
        checkpoint_dir: str | None = None,
        max_concurrent: int = 4,
        default_timeout: float = 300.0,
    ) -> None:
        self.checkpoint_dir = Path(checkpoint_dir) if checkpoint_dir else _PROJECT_ROOT / "vetinari_checkpoints"
        self.checkpoint_dir.mkdir(parents=True, exist_ok=True)

        self.max_concurrent = max_concurrent
        self.default_timeout = default_timeout

        # SQLite database for crash-safe checkpointing. When checkpoint_dir
        # is provided (e.g. in tests), use a standalone db file in that
        # directory. Otherwise (production), pass None to delegate to the
        # unified vetinari.database module (ADR-0072).
        db_path = (self.checkpoint_dir / "execution_state.db") if checkpoint_dir else None
        self._db = _DatabaseManager(db_path)

        # Checkpoint store facade — higher-level persistence operations that
        # delegate to the same SQLite database as _db.  Used by the recovery
        # functions to call named methods (save_event, load_checkpoint_graph_json,
        # find_incomplete_ids, etc.) rather than raw SQL strings.
        self._checkpoint_store = CheckpointStore(checkpoint_dir=self.checkpoint_dir if checkpoint_dir else None)

        # Active executions indexed by plan_id
        self._active_executions: dict[str, ExecutionGraph] = {}
        self._execution_lock = threading.Lock()

        # Cycle detection — prevents infinite retry/rework loops (max 10 executions per task)
        self._cycle_detector = CycleDetector(max_iterations=10)

        # Human checkpoint registry — tasks added here require explicit approval
        # before their results propagate to downstream dependents.
        self._human_checkpoint = HumanCheckpoint()

        # Circuit breaker — prevents cascading failures when models are unavailable.
        # Logged at ERROR when unavailable because cascading-failure protection is
        # a security-relevant safety property — operators must know it is absent.
        self._circuit_breaker = None
        self._circuit_breaker_degraded = False  # True when CB import failed; results carry degraded marker
        if _CIRCUIT_BREAKER_CLS is not None:
            self._circuit_breaker = _CIRCUIT_BREAKER_CLS("durable_execution")
        else:
            self._circuit_breaker_degraded = True
            logger.error(
                "Circuit breaker unavailable for durable execution — "
                "tasks will proceed without cascading-failure protection; "
                "results will carry '_circuit_breaker_degraded=True'"
            )

        # In-memory event history (last 1000 events) for fast access
        self._event_history: deque[ExecutionEvent] = deque(maxlen=1000)

        # Task handlers keyed by task_type; "default" is the fallback
        self._task_handlers: dict[str, Callable] = {}
        self._workflow_adapter: DurableWorkflowAdapter | None = None
        self._receipt_store = WorkReceiptStore()

        # Lifecycle callbacks — optional, called on each task state change
        self._on_task_start: Callable | None = None
        self._on_task_complete: Callable | None = None
        self._on_task_fail: Callable | None = None

        # Heartbeat tracking — detect stuck tasks that stop reporting progress
        self._heartbeats: dict[str, float] = {}  # task_id -> last heartbeat time
        self._heartbeat_timeout = default_timeout

        # Shared thread pool — created once, reused across all layer executions
        # to avoid the overhead of spawning and destroying workers per layer.
        self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=self.max_concurrent)

        logger.info(
            "DurableExecutionEngine initialized (checkpoint_dir=%s, backend=sqlite+wal)",
            self.checkpoint_dir,
        )

    def record_heartbeat(self, task_id: str) -> None:
        """Record that a task is still making progress.

        Call this periodically during long-running operations (e.g. LLM
        inference) to prevent the task from being considered stuck.

        Args:
            task_id: The task reporting activity.
        """
        self._heartbeats[task_id] = time.time()

    def is_task_stuck(self, task_id: str) -> bool:
        """Check if a task has missed its heartbeat deadline.

        Args:
            task_id: The task to check.

        Returns:
            True if the task has not sent a heartbeat within the timeout
            window. False if no heartbeat has been registered yet (task
            has not started).
        """
        last_beat = self._heartbeats.get(task_id)
        if last_beat is None:
            return False  # No heartbeat registered yet — task hasn't started
        return (time.time() - last_beat) > self._heartbeat_timeout

    def register_handler(self, task_type: str, handler: Callable) -> None:
        """Register a callable to handle tasks of a specific type.

        Args:
            task_type: The task type string to route to this handler.
            handler: Callable that accepts an ExecutionTaskNode and returns
                a result dict.
        """
        self._task_handlers[task_type] = handler
        logger.debug("Registered handler for task type: %s", task_type)

    def set_callbacks(
        self,
        on_task_start: Callable | None = None,
        on_task_complete: Callable | None = None,
        on_task_fail: Callable | None = None,
    ) -> None:
        """Set lifecycle callbacks for task state transitions.

        Args:
            on_task_start: Called when a task transitions to RUNNING.
            on_task_complete: Called when a task transitions to COMPLETED.
            on_task_fail: Called when a task exhausts all retries and fails.
        """
        self._on_task_start = on_task_start
        self._on_task_complete = on_task_complete
        self._on_task_fail = on_task_fail

    def register_workflow_adapter(self, adapter: DurableWorkflowAdapter, *, force: bool = False) -> None:
        """Register the durable workflow adapter for this engine instance.

        Mutates only ``self._workflow_adapter`` under ``_ADAPTER_REGISTRY_LOCK``.
        The same adapter can be registered repeatedly; a different adapter is
        rejected unless the caller explicitly passes ``force=True``.

        Raises:
            WorkflowAdapterError: If a different adapter is already registered
                and ``force`` is false.
        """
        with _ADAPTER_REGISTRY_LOCK:
            if self._workflow_adapter is None or self._workflow_adapter is adapter:
                self._workflow_adapter = adapter
                return
            if force:
                logger.warning("workflow adapter replaced via force=True (engine_id=%s)", id(self))
                self._workflow_adapter = adapter
                return
            raise WorkflowAdapterError(
                f"adapter already registered (engine_id={id(self)}); pass force=True to replace",
            )

    def create_execution(self, graph: ExecutionGraph) -> str:
        """Register a new execution and write its initial checkpoint.

        Args:
            graph: The execution graph to register.

        Returns:
            The plan_id that identifies this execution, used to load
            checkpoints or query status later.
        """
        plan_id = graph.plan_id
        with self._execution_lock:
            self._active_executions[plan_id] = graph
        self._save_checkpoint(plan_id, graph)
        logger.info("Created execution for plan: %s", plan_id)
        return plan_id
