"""Pipeline integration for context management — checks budget and compacts at stage boundaries.

Called between pipeline stages to ensure context stays within model limits.
This is the glue between the context subsystem (budget, compaction, restoration)
and the orchestration pipeline.
"""

from __future__ import annotations

import logging

from vetinari.context.budget import BudgetCheck, BudgetStatus, ContextBudget, create_budget_for_model
from vetinari.context.compaction import ContextCompactor, get_compactor
from vetinari.context.restoration import ContextRestorer, RestorationContext, get_restorer
from vetinari.context.window_manager import WindowConversationMessage

logger = logging.getLogger(__name__)


# ── ContextBudgetExceeded ─────────────────────────────────────────────


class ContextBudgetExceeded(Exception):
    """Raised when context usage exceeds the hard stop threshold."""


# ── PipelineContextManager ────────────────────────────────────────────


class PipelineContextManager:
    """Manages context budget across pipeline stages, compacting and restoring as needed.

    Owns a ContextBudget, a ContextCompactor, and a ContextRestorer for a
    single pipeline run. Pipeline code calls ``check_budget`` at stage
    boundaries; this class decides whether to pass through, warn, compact,
    or hard-stop based on the running token total versus the model's context
    window.
    """

    def __init__(self, model_id: str) -> None:
        """Set up budget tracker, compactor, and restorer for a pipeline run.

        Args:
            model_id: The model identifier to size the budget for.
        """
        self._budget: ContextBudget = create_budget_for_model(model_id)
        self._compactor: ContextCompactor = get_compactor()
        self._restorer: ContextRestorer = get_restorer()
        self._model_id: str = model_id

    def check_budget(
        self,
        stage: str,
        messages: list[WindowConversationMessage],
        restoration_context: RestorationContext | None = None,
    ) -> tuple[list[WindowConversationMessage], BudgetCheck]:
        """Check context budget at a stage boundary. Compact and restore if needed.

        This is the single method pipeline code calls between stages. It
        records the current token total for the stage, classifies the budget
        health, and takes action:

        - OK / WARNING: pass through unchanged (WARNING is logged).
        - COMPACTION_NEEDED: compact the message list to the remaining budget,
          optionally restore critical context, then re-check.
        - EXCEEDED: raise ``ContextBudgetExceeded`` so the pipeline can abort
          cleanly rather than silently truncating.

        Args:
            stage: Current pipeline stage name for tracking (e.g. ``"execution"``).
            messages: Current conversation messages.
            restoration_context: Context to restore after compaction (agent
                instructions, task, recent output, etc.). May be ``None`` when
                the caller has no structured context to inject.

        Returns:
            Tuple of (possibly-compacted messages, budget check result).

        Raises:
            ContextBudgetExceeded: When usage exceeds the hard stop threshold.
        """
        total_tokens = sum(m.token_count for m in messages)
        self._budget.record_usage(stage, total_tokens)

        check = self._budget.check()

        if check.status == BudgetStatus.OK:
            return messages, check

        if check.status == BudgetStatus.WARNING:
            logger.warning(
                "Context budget at %.0f%% after stage '%s' — approaching limit",
                check.usage_ratio * 100,
                stage,
            )
            return messages, check

        if check.status == BudgetStatus.EXCEEDED:
            logger.error(
                "Context budget exceeded (%.0f%%) at stage '%s' — hard stop",
                check.usage_ratio * 100,
                stage,
            )
            raise ContextBudgetExceeded(f"Context budget exceeded at stage '{stage}': {check.usage_ratio:.0%} used")

        # COMPACTION_NEEDED: compact then restore
        logger.info(
            "Context budget at %.0f%% after stage '%s' — compacting",
            check.usage_ratio * 100,
            stage,
        )
        target_tokens = self._budget.compaction_target_tokens()
        messages, _compaction_result = self._compactor.compact(
            messages,
            target_tokens=target_tokens,
            task_id="",
            stage=stage,
            model_id=self._model_id,
        )

        if restoration_context is not None:
            messages, _restoration_result = self._restorer.restore(messages, restoration_context)

        # Reset and re-record after compaction so subsequent checks are accurate
        new_total = sum(m.token_count for m in messages)
        self._budget.reset()
        self._budget.record_usage(stage, new_total)

        return messages, self._budget.check()

    @property
    def budget(self) -> ContextBudget:
        """The underlying ContextBudget for this pipeline run."""
        return self._budget


# ── Factory ───────────────────────────────────────────────────────────


def create_pipeline_context_manager(model_id: str) -> PipelineContextManager:
    """Create a PipelineContextManager for a pipeline run.

    Args:
        model_id: The model identifier used to size the context budget.

    Returns:
        A fresh PipelineContextManager with no recorded usage.
    """
    return PipelineContextManager(model_id=model_id)


__all__ = [
    "ContextBudgetExceeded",
    "PipelineContextManager",
    "create_pipeline_context_manager",
]
