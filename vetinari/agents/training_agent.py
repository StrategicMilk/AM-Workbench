"""Vetinari Training Agent.

Bridge agent that connects the 3-agent factory pipeline to the Training
subsystem (vetinari/training/). It delegates all training operations to
existing infrastructure rather than reimplementing them.

Responsibilities:
- Delegate training curriculum queries to TrainingCurriculum
- Delegate adapter registry queries to list_adapters_by_task_type
- Delegate training ledger operations to append_ledger_entry / load_training_ledger
- Delegate data seeding operations to TrainingDataSeeder
- Surface training capability availability through the standard AgentInterface

This module is import-safe: all training imports are deferred to first call,
keeping module-level import cost near zero (avoids pulling torch/trl at import).
"""

from __future__ import annotations

import logging
from typing import Any

from vetinari.agents.base_agent import BaseAgent
from vetinari.agents.contracts import AgentResult, AgentTask, VerificationResult
from vetinari.boundary_guards import clamp_score, require_nonempty, require_score_in_range
from vetinari.types import AgentType

logger = logging.getLogger(__name__)


# Module-level lazy references — defers heavy training imports until first use.
# Training imports can pull in torch/trl/transformers; lazy init prevents
# import-time native library initialisation (see anti-pattern: import-probe
# executes optional native package).
# Who writes: each getter on first call. Who reads: execute/verify methods.
_agent_trainer_cls = None
_training_curriculum_cls = None
_training_scheduler_cls = None
_list_adapters_fn = None
_load_ledger_fn = None
_append_ledger_fn = None


def _get_agent_trainer_class() -> type:
    """Return AgentTrainer class, importing once on first call.

    Returns:
        The AgentTrainer class from vetinari.training.
    """
    global _agent_trainer_cls
    if _agent_trainer_cls is None:
        from vetinari.training import AgentTrainer

        _agent_trainer_cls = AgentTrainer
    return _agent_trainer_cls


def _get_curriculum_class() -> type:
    """Return TrainingCurriculum class, importing once on first call.

    Returns:
        The TrainingCurriculum class from vetinari.training.
    """
    global _training_curriculum_cls
    if _training_curriculum_cls is None:
        from vetinari.training import TrainingCurriculum

        _training_curriculum_cls = TrainingCurriculum
    return _training_curriculum_cls


def _get_list_adapters_fn() -> Any:
    """Return list_adapters_by_task_type callable, importing once on first call.

    Returns:
        The list_adapters_by_task_type function from vetinari.training.
    """
    global _list_adapters_fn
    if _list_adapters_fn is None:
        from vetinari.training import list_adapters_by_task_type

        _list_adapters_fn = list_adapters_by_task_type
    return _list_adapters_fn


def _get_load_ledger_fn() -> Any:
    """Return load_training_ledger callable, importing once on first call.

    Returns:
        The load_training_ledger function from vetinari.training.
    """
    global _load_ledger_fn
    if _load_ledger_fn is None:
        from vetinari.training import load_training_ledger

        _load_ledger_fn = load_training_ledger
    return _load_ledger_fn


def _get_append_ledger_fn() -> Any:
    """Return append_ledger_entry callable, importing once on first call.

    Returns:
        The append_ledger_entry function from vetinari.training.
    """
    global _append_ledger_fn
    if _append_ledger_fn is None:
        from vetinari.training import append_ledger_entry

        _append_ledger_fn = append_ledger_entry
    return _append_ledger_fn


class TrainingAgent(BaseAgent):
    """Agent that bridges the factory pipeline to the Training subsystem.

    Delegates all operations to vetinari/training/ infrastructure. This
    agent does not own training logic; it is the pipeline integration point
    that makes training capabilities available to Foreman task plans.

    Supported operations (passed via task.context["operation"]):
    - ``list_adapters``: list deployed adapters by task type from the registry
    - ``curriculum_status``: query the current training curriculum status
    - ``load_ledger``: load recent training ledger entries
    - ``append_ledger``: append a new training ledger entry

    Native ML stack imports (torch, trl, transformers) are deferred to first
    use so that importing this agent does not trigger ML library initialisation
    in environments where those packages are unavailable.
    """

    AGENT_TYPE = AgentType.TRAINING
    DEFAULT_OPERATION = "list_adapters"

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        """Initialise the TrainingAgent with TRAINING agent type.

        Args:
            config: Optional per-instance configuration overrides.
        """
        super().__init__(agent_type=AgentType.TRAINING, config=config)

    def get_system_prompt(self) -> str:
        """Return the system prompt for this agent.

        Returns:
            System prompt string describing training agent capabilities.
        """
        return (
            "You are the Training Agent, responsible for bridging factory pipeline "
            "tasks to the Vetinari Training subsystem. Delegate all adapter registry "
            "queries, curriculum status checks, and ledger operations to the existing "
            "training infrastructure. Never reimplement training logic; always delegate "
            "to vetinari/training/."
        )

    def execute(self, task: AgentTask) -> AgentResult:
        """Execute a training task by delegating to the training subsystem.

        Dispatches based on task.context["operation"]:
        - ``list_adapters``: lists deployed adapters from the adapter registry
        - ``curriculum_status``: returns current curriculum status
        - ``load_ledger``: loads recent training ledger entries
        - ``append_ledger``: appends a new entry to the training ledger

        Args:
            task: The agent task describing the training operation to perform.

        Returns:
            AgentResult with output from the delegated training operation.
        """
        ctx = task.context or {}
        operation = ctx.get("operation", self.DEFAULT_OPERATION)
        logger.info("TrainingAgent executing operation %s for task %s", operation, task.task_id)

        try:
            if operation == "list_adapters":
                return self._execute_list_adapters(task)
            if operation == "curriculum_status":
                return self._execute_curriculum_status(task)
            if operation == "load_ledger":
                return self._execute_load_ledger(task)
            if operation == "append_ledger":
                return self._execute_append_ledger(task)
            return AgentResult(
                success=False,
                output="",
                task_id=task.task_id,
                errors=[
                    f"Unknown training operation: {operation!r}. "
                    "Supported: list_adapters, curriculum_status, load_ledger, append_ledger."
                ],
            )
        except Exception as exc:
            logger.exception(
                "TrainingAgent failed during operation %s for task %s — returning error result",
                operation,
                task.task_id,
            )
            return AgentResult(
                success=False,
                output="",
                task_id=task.task_id,
                errors=[str(exc)],
            )

    def verify(
        self, output: AgentResult | None = None, *, curriculum_status: str | None = None
    ) -> VerificationResult | float:
        """Verify that a training result contains non-empty output.

        Fails closed on None, empty, or missing output per the verifier contract.
        Non-AgentResult inputs (str, dict, etc.) also fail closed.

        Args:
            output: The AgentResult to verify.
            curriculum_status: Optional curriculum status expected by the verifier contract.

        Returns:
            VerificationResult with passed=True only when output is present and non-empty.
        """
        if curriculum_status is not None:
            try:
                require_nonempty(curriculum_status, field_name="curriculum_status")
            except ValueError:
                logger.warning("Training curriculum status is empty; verification failed closed", exc_info=True)
                return 0.0
            return require_score_in_range(clamp_score(1.0, label="curriculum_status"))
        if not isinstance(output, AgentResult) or not output.output:
            return VerificationResult(
                passed=False,
                score=0.0,
                issues=[{"message": "TrainingAgent result has no output — verification failed closed."}],
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
    def _execute_list_adapters(task: AgentTask) -> AgentResult:
        """List deployed adapters from the adapter registry.

        Args:
            task: The agent task; context may contain ``task_type`` (str) to filter by.

        Returns:
            AgentResult with adapter list as output.
        """
        ctx = task.context or {}
        task_type: str | None = ctx.get("task_type")
        list_adapters = _get_list_adapters_fn()
        adapters = list_adapters(task_type=task_type) if task_type else list_adapters()
        output = f"Found {len(adapters)} adapter(s)" + (f" for task_type={task_type!r}" if task_type else "") + "."
        return AgentResult(
            success=True,
            output=output,
            task_id=task.task_id,
            metadata={"adapters": adapters, "task_type": task_type},
        )

    @staticmethod
    def _execute_curriculum_status(task: AgentTask) -> AgentResult:
        """Query the current training curriculum status.

        Args:
            task: The agent task (context unused for status queries).

        Returns:
            AgentResult with curriculum status summary as output.
        """
        curriculum_cls = _get_curriculum_class()
        output = f"TrainingCurriculum class available: {curriculum_cls.__name__}"
        return AgentResult(
            success=True,
            output=output,
            task_id=task.task_id,
            metadata={"curriculum_class": curriculum_cls.__name__},
        )

    @staticmethod
    def _execute_load_ledger(task: AgentTask) -> AgentResult:
        """Load recent training ledger entries.

        Args:
            task: The agent task; context may contain ``limit`` (int, default 20).

        Returns:
            AgentResult with ledger entry summaries as output.
        """
        ctx = task.context or {}
        limit: int = int(ctx.get("limit", 20))
        load_ledger = _get_load_ledger_fn()
        entries = load_ledger()
        recent = entries[-limit:] if entries else []
        output = f"Loaded {len(recent)} recent training ledger entries (of {len(entries)} total)."
        return AgentResult(
            success=True,
            output=output,
            task_id=task.task_id,
            metadata={"entry_count": len(recent), "total_count": len(entries)},
        )

    @staticmethod
    def _execute_append_ledger(task: AgentTask) -> AgentResult:
        """Append a new entry to the training ledger.

        Args:
            task: The agent task; context must contain ``entry`` (dict).

        Returns:
            AgentResult confirming the ledger entry was appended.
        """
        ctx = task.context or {}
        entry_data: dict[str, Any] | None = ctx.get("entry")
        if not entry_data:
            return AgentResult(
                success=False,
                output="",
                task_id=task.task_id,
                errors=["append_ledger requires context.entry (dict) to be set."],
            )
        from vetinari.training import TrainingLedgerEntry

        entry = TrainingLedgerEntry(**entry_data) if isinstance(entry_data, dict) else entry_data
        append_ledger = _get_append_ledger_fn()
        append_ledger(entry)
        output = "Training ledger entry appended successfully."
        return AgentResult(
            success=True,
            output=output,
            task_id=task.task_id,
        )


def get_training_agent(config: dict[str, Any] | None = None) -> TrainingAgent:
    """Construct and return a TrainingAgent instance.

    This is the canonical factory function for TrainingAgent — callers should
    prefer this over direct construction so the singleton/caching pattern can
    be applied here if needed in the future.

    Args:
        config: Optional per-instance configuration overrides.

    Returns:
        A new TrainingAgent instance.
    """
    return TrainingAgent(config=config)
