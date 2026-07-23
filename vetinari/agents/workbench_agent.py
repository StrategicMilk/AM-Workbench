"""Vetinari Workbench Agent.

Bridge agent that connects the 3-agent factory pipeline to the Workbench
subsystem (vetinari/workbench/). It delegates all workbench operations to
existing infrastructure rather than reimplementing them.

Responsibilities:
- Run, inspect, and manage WorkbenchSpine sessions
- Query run history and status
- Delegate to WorkbenchSpine for durable metadata spine operations
- Surface workbench capability availability through the standard AgentInterface

This module is import-safe: WorkbenchSpine construction is deferred to the
first call that needs it, keeping module-level import cost near zero.
"""

from __future__ import annotations

import logging
from typing import Any

from vetinari.agents.base_agent import BaseAgent
from vetinari.agents.contracts import AgentResult, AgentTask, VerificationResult
from vetinari.types import AgentType

logger = logging.getLogger(__name__)


# Module-level lazy references — avoids heavy workbench imports at import time.
# Who writes: each getter on first call. Who reads: execute/verify methods.
_workbench_spine_cls = None
_get_workbench_spine_fn = None


def _get_workbench_spine_class() -> type:
    """Return WorkbenchSpine class, importing once on first call.

    Returns:
        The WorkbenchSpine class from vetinari.workbench.
    """
    global _workbench_spine_cls
    if _workbench_spine_cls is None:
        from vetinari.workbench import WorkbenchSpine

        _workbench_spine_cls = WorkbenchSpine
    return _workbench_spine_cls


def _get_spine_getter() -> Any:
    """Return the get_workbench_spine callable, importing once on first call.

    Returns:
        The get_workbench_spine function from vetinari.workbench.
    """
    global _get_workbench_spine_fn
    if _get_workbench_spine_fn is None:
        from vetinari.workbench import get_workbench_spine

        _get_workbench_spine_fn = get_workbench_spine
    return _get_workbench_spine_fn


class WorkbenchAgent(BaseAgent):
    """Agent that bridges the factory pipeline to the Workbench subsystem.

    Delegates all operations to vetinari/workbench/ infrastructure. This
    agent does not own workbench logic; it is the pipeline integration point
    that makes workbench capabilities available to Foreman task plans.

    Supported operations (passed via task.context["operation"]):
    - ``status``: query the current workbench spine run status
    - ``list_runs``: list recent workbench runs from the spine
    - ``get_run``: retrieve a specific run record from the spine

    All write operations go through WorkbenchSpine to ensure durable, typed
    metadata is recorded on disk with correct provenance.
    """

    AGENT_TYPE = AgentType.WORKBENCH
    DEFAULT_OPERATION = "status"

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        """Initialise the WorkbenchAgent with WORKBENCH agent type.

        Args:
            config: Optional per-instance configuration overrides.
        """
        super().__init__(agent_type=AgentType.WORKBENCH, config=config)

    def get_system_prompt(self) -> str:
        """Return the system prompt for this agent.

        Returns:
            System prompt string describing workbench agent capabilities.
        """
        return (
            "You are the Workbench Agent, responsible for bridging factory pipeline "
            "tasks to the Vetinari Workbench subsystem. Delegate all run management, "
            "session status queries, and spine operations to the WorkbenchSpine. "
            "Never reimplement workbench logic; always delegate to existing infrastructure."
        )

    def execute(self, task: AgentTask) -> AgentResult:
        """Execute a workbench task by delegating to WorkbenchSpine.

        Dispatches based on task.context["operation"]:
        - ``status``: queries the spine for current run status
        - ``list_runs``: returns recent runs from the spine
        - ``get_run``: retrieves a specific run by run_id

        Args:
            task: The agent task describing the workbench operation to perform.

        Returns:
            AgentResult with output from the delegated workbench operation.
        """
        ctx = task.context or {}
        operation = ctx.get("operation", self.DEFAULT_OPERATION)
        logger.info("WorkbenchAgent executing operation %s for task %s", operation, task.task_id)

        try:
            if operation == "status":
                return self._execute_status(task)
            if operation == "list_runs":
                return self._execute_list_runs(task)
            if operation == "get_run":
                return self._execute_get_run(task)
            return AgentResult(
                success=False,
                output="",
                task_id=task.task_id,
                errors=[f"Unknown workbench operation: {operation!r}. Supported: status, list_runs, get_run."],
            )
        except Exception as exc:
            logger.exception(
                "WorkbenchAgent failed during operation %s for task %s — returning error result",
                operation,
                task.task_id,
            )
            return AgentResult(
                success=False,
                output="",
                task_id=task.task_id,
                errors=[str(exc)],
            )

    def verify(self, output: AgentResult) -> VerificationResult:
        """Verify that a workbench result contains non-empty output.

        Fails closed on None, empty, or missing output per the verifier contract.
        Non-AgentResult inputs (str, dict, etc.) also fail closed.

        Args:
            output: The AgentResult to verify.

        Returns:
            VerificationResult with passed=True only when output is present and non-empty.
        """
        if not isinstance(output, AgentResult) or not output.output:
            return VerificationResult(
                passed=False,
                score=0.0,
                issues=[{"message": "WorkbenchAgent result has no output — verification failed closed."}],
            )
        return VerificationResult(
            passed=True,
            score=1.0,
            issues=[],
        )

    # ------------------------------------------------------------------
    # Private delegation helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _execute_status(task: AgentTask) -> AgentResult:
        """Query workbench spine status.

        Args:
            task: The agent task (context unused for status queries).

        Returns:
            AgentResult with a status summary string as output.
        """
        get_spine = _get_spine_getter()
        spine = get_spine()
        status_summary = f"WorkbenchSpine is operational. Class: {type(spine).__name__}"
        return AgentResult(
            success=True,
            output=status_summary,
            task_id=task.task_id,
        )

    @staticmethod
    def _execute_list_runs(task: AgentTask) -> AgentResult:
        """List recent runs from the workbench spine.

        Args:
            task: The agent task; context may contain ``limit`` (int, default 10).

        Returns:
            AgentResult with a list of recent run summaries as output.
        """
        ctx = task.context or {}
        limit: int = int(ctx.get("limit", 10))
        get_spine = _get_spine_getter()
        spine = get_spine()
        runs = []
        if hasattr(spine, "list_runs"):
            runs = spine.list_runs(limit=limit)
        output = f"Listed {len(runs)} recent workbench runs (limit={limit})."
        return AgentResult(
            success=True,
            output=output,
            task_id=task.task_id,
            metadata={"runs": runs, "limit": limit},
        )

    @staticmethod
    def _execute_get_run(task: AgentTask) -> AgentResult:
        """Retrieve a specific workbench run by run_id.

        Args:
            task: The agent task; context must contain ``run_id`` (str).

        Returns:
            AgentResult with the run record as output, or an error if not found.
        """
        ctx = task.context or {}
        run_id: str | None = ctx.get("run_id")
        if not run_id:
            return AgentResult(
                success=False,
                output="",
                task_id=task.task_id,
                errors=["get_run requires context.run_id to be set."],
            )
        get_spine = _get_spine_getter()
        spine = get_spine()
        run = None
        if hasattr(spine, "get_run"):
            run = spine.get_run(run_id)
        if run is None:
            return AgentResult(
                success=False,
                output="",
                task_id=task.task_id,
                errors=[f"WorkbenchSpine: run_id {run_id!r} not found."],
            )
        return AgentResult(
            success=True,
            output=str(run),
            task_id=task.task_id,
            metadata={"run": run},
        )


def get_workbench_agent(config: dict[str, Any] | None = None) -> WorkbenchAgent:
    """Construct and return a WorkbenchAgent instance.

    This is the canonical factory function for WorkbenchAgent — callers should
    prefer this over direct construction so the singleton/caching pattern can
    be applied here if needed in the future.

    Args:
        config: Optional per-instance configuration overrides.

    Returns:
        A new WorkbenchAgent instance.
    """
    return WorkbenchAgent(config=config)
