"""Context budget tracking — monitors token usage per pipeline stage with dynamic thresholds.

Tracks cumulative token consumption across Foreman, Worker, and Inspector stages.
Triggers warnings, auto-compaction, and hard stops based on the model's actual
context window size rather than fixed limits.

This is a support module for the pipeline handoff points:
Foreman -> Worker -> Inspector each check the budget before passing work forward.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


# ── Threshold defaults ─────────────────────────────────────────────────
# These ratios are intentionally generous: warn early, compact before crisis,
# hard-stop only when the model would silently truncate or refuse.
_DEFAULT_WARN_RATIO: float = 0.70
_DEFAULT_COMPACT_RATIO: float = 0.85
_DEFAULT_HARD_STOP_RATIO: float = 0.95


# ── BudgetThresholds ───────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class BudgetThresholds:
    """Ratio-based thresholds controlling when budget actions fire.

    All three values are fractions of the model's total context_length.
    Keeping them as ratios (not absolute token counts) means this config
    works unchanged across models with wildly different window sizes.

    Attributes:
        warn_ratio: Fraction of context_length at which a warning is logged.
            Default 0.70 (70%).
        compact_ratio: Fraction at which auto-compaction should be triggered.
            Default 0.85 (85%).
        hard_stop_ratio: Fraction at which no new work is accepted.
            Default 0.95 (95%).
    """

    warn_ratio: float = _DEFAULT_WARN_RATIO  # emit warning log
    compact_ratio: float = _DEFAULT_COMPACT_RATIO  # trigger auto-compaction
    hard_stop_ratio: float = _DEFAULT_HARD_STOP_RATIO  # refuse new work

    def __post_init__(self) -> None:
        if not (0.0 < self.warn_ratio < self.compact_ratio < self.hard_stop_ratio <= 1.0):
            raise ValueError(
                f"Thresholds must satisfy 0 < warn ({self.warn_ratio}) "
                f"< compact ({self.compact_ratio}) "
                f"< hard_stop ({self.hard_stop_ratio}) <= 1.0"
            )


# ── BudgetStatus ───────────────────────────────────────────────────────


class BudgetStatus(Enum):
    """Current health of the context budget relative to the model's window.

    Values:
        OK: Usage is below the warning threshold (< 70% by default).
        WARNING: Usage is between warn and compact thresholds (70-85%).
        COMPACTION_NEEDED: Usage is between compact and hard-stop (85-95%).
            The budget owner should compact the context before proceeding.
        EXCEEDED: Usage is above the hard-stop threshold (> 95%).
            No new work should be accepted until context is reduced.
    """

    OK = "ok"
    WARNING = "warning"
    COMPACTION_NEEDED = "compaction_needed"
    EXCEEDED = "exceeded"


# ── StageUsage ─────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class StageUsage:
    """Token usage snapshot for a single pipeline stage.

    Attributes:
        stage: Name of the pipeline stage (e.g. "foreman", "worker", "inspector").
        tokens_used: Cumulative tokens recorded for this stage so far.
        tokens_added: Delta tokens added in the most recent ``record_usage`` call.
            Zero when this is a historical snapshot not tied to a specific update.
        ratio: Fraction of the total context budget consumed by this stage alone.
    """

    stage: str
    tokens_used: int
    tokens_added: int  # delta from the preceding check
    ratio: float  # this stage's share of the total context budget

    def __repr__(self) -> str:
        return (
            f"StageUsage(stage={self.stage!r}, tokens_used={self.tokens_used}, "
            f"tokens_added={self.tokens_added}, ratio={self.ratio:.3f})"
        )


# ── BudgetCheck ───────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class BudgetCheck:
    """Complete budget evaluation result returned by ContextBudget.check().

    Callers inspect ``status`` to decide what action (if any) to take, and
    ``message`` for a human-readable explanation suitable for logging.

    Attributes:
        status: The current budget health category.
        total_tokens: Sum of all tokens recorded across every stage.
        context_limit: The model's actual context window size in tokens.
        usage_ratio: Fraction of context_limit consumed (0.0 - 1.0+).
        remaining_tokens: Tokens left before hitting the hard-stop threshold.
            Can be negative when the budget is exceeded.
        stage_breakdown: Per-stage usage details, sorted by tokens_used descending.
        message: Human-readable summary of the current budget state.
    """

    status: BudgetStatus
    total_tokens: int
    context_limit: int
    usage_ratio: float
    remaining_tokens: int
    stage_breakdown: list[StageUsage]
    message: str

    def __repr__(self) -> str:
        return (
            f"BudgetCheck(status={self.status.value!r}, total_tokens={self.total_tokens}, "
            f"context_limit={self.context_limit}, usage_ratio={self.usage_ratio:.3f})"
        )


# ── ContextBudget ──────────────────────────────────────────────────────


class ContextBudget:
    """Token budget tracker for a single model session across pipeline stages.

    Records cumulative token consumption per stage name and evaluates the
    total against dynamic thresholds derived from the model's actual context
    window size. Unlike a fixed-limit guard, this class scales automatically:
    the same thresholds work for a 32k local model and a 200k cloud model.

    Typical usage at a pipeline handoff::

        budget = create_budget_for_model("qwen2.5-coder-7b")
        budget.record_usage("foreman", tokens_used_by_foreman)
        check = budget.check()
        if check.status == BudgetStatus.COMPACTION_NEEDED:
            compactor.compact(messages, target_tokens=budget.remaining())
        elif check.status == BudgetStatus.EXCEEDED:
            raise RuntimeError("Context budget exceeded — cannot hand off to worker")

    Thread safety: ``record_usage`` and ``reset*`` mutate internal state.
    Callers are responsible for external locking if concurrent writes occur.
    """

    def __init__(
        self,
        context_length: int,
        thresholds: BudgetThresholds | None = None,
    ) -> None:
        """Set up a budget tracker for a model with the given context window.

        Args:
            context_length: The model's actual context window size in tokens.
                Must be >= 1.
            thresholds: Optional custom threshold ratios. Defaults to
                BudgetThresholds() which applies 70/85/95 ratios.

        Raises:
            ValueError: If context_length is less than 1.
        """
        if context_length < 1:
            raise ValueError(f"context_length must be >= 1, got {context_length}")

        self._context_length: int = context_length
        self._thresholds: BudgetThresholds = thresholds or BudgetThresholds()
        # stage_name -> cumulative tokens recorded for that stage
        # Who writes: record_usage(), reset_stage(), reset()
        # Who reads: check(), should_compact(), remaining(), to_dict()
        # Lifecycle: lives for the duration of one pipeline run
        # Lock: none — callers own synchronisation if needed
        self._stage_tokens: dict[str, int] = {}
        self._total_tokens: int = 0

    # ── Public recording API ───────────────────────────────────────────

    def record_usage(self, stage: str, tokens: int) -> None:
        """Accumulate token usage for a pipeline stage.

        Adds ``tokens`` to the running total for ``stage``. Calling this
        multiple times for the same stage is additive — it never resets the
        stage unless ``reset_stage`` is called explicitly.

        Logs a debug line so pipeline runs leave a clear token trail in the
        application log.

        Args:
            stage: Name of the pipeline stage (e.g. "foreman", "worker").
                Any non-empty string is valid; unknown stages are created
                on first use.
            tokens: Number of tokens to add. Must be >= 0.

        Raises:
            ValueError: If tokens is negative or stage is empty.
        """
        if not stage:
            raise ValueError("stage name must be non-empty")
        if tokens < 0:
            raise ValueError(f"tokens must be >= 0, got {tokens}")

        self._stage_tokens[stage] = self._stage_tokens.get(stage, 0) + tokens
        self._total_tokens += tokens

        logger.debug(
            "budget.record_usage: stage=%r +%d tokens (stage_total=%d, grand_total=%d)",
            stage,
            tokens,
            self._stage_tokens[stage],
            self._total_tokens,
        )

    # ── Public evaluation API ──────────────────────────────────────────

    def check(self) -> BudgetCheck:
        """Evaluate total token usage against the model's context window thresholds.

        Computes the current usage ratio and maps it to a BudgetStatus.
        The stage breakdown is sorted by tokens_used descending so the
        heaviest consumer appears first in logs and dashboards.

        Returns:
            A BudgetCheck containing the status, usage metrics, and a
            human-readable message suitable for direct logging.
        """
        ratio = self._total_tokens / self._context_length if self._context_length > 0 else 0.0

        status = self._classify(ratio)

        hard_stop_limit = int(self._context_length * self._thresholds.hard_stop_ratio)
        remaining = hard_stop_limit - self._total_tokens

        breakdown = sorted(
            [
                StageUsage(
                    stage=stage,
                    tokens_used=used,
                    tokens_added=0,  # snapshot — not tied to a specific update
                    ratio=used / self._context_length if self._context_length > 0 else 0.0,
                )
                for stage, used in self._stage_tokens.items()
            ],
            key=lambda s: s.tokens_used,
            reverse=True,
        )

        message = self._build_message(status, ratio, remaining)

        if status == BudgetStatus.WARNING:
            logger.warning(
                "Context budget WARNING: %d/%d tokens used (%.0f%%) — consider compacting soon",
                self._total_tokens,
                self._context_length,
                ratio * 100,
            )
        elif status == BudgetStatus.COMPACTION_NEEDED:
            logger.warning(
                "Context budget COMPACTION NEEDED: %d/%d tokens used (%.0f%%) — compact before next handoff",
                self._total_tokens,
                self._context_length,
                ratio * 100,
            )
        elif status == BudgetStatus.EXCEEDED:
            logger.error(
                "Context budget EXCEEDED: %d/%d tokens used (%.0f%%) — no new work can be accepted",
                self._total_tokens,
                self._context_length,
                ratio * 100,
            )

        return BudgetCheck(
            status=status,
            total_tokens=self._total_tokens,
            context_limit=self._context_length,
            usage_ratio=round(ratio, 4),
            remaining_tokens=remaining,
            stage_breakdown=breakdown,
            message=message,
        )

    def should_compact(self) -> bool:
        """Return True when context usage warrants auto-compaction.

        Compaction is needed when the status is COMPACTION_NEEDED or EXCEEDED.
        Callers should compact then call ``reset()`` or ``reset_stage()`` to
        reflect the reduced token count after compaction.

        Returns:
            True if compaction should be triggered before accepting new work.
        """
        ratio = self._total_tokens / self._context_length if self._context_length > 0 else 0.0
        return ratio >= self._thresholds.compact_ratio

    def remaining(self) -> int:
        """Tokens available before the hard-stop threshold is reached.

        Negative values indicate the budget is already exceeded. This method
        is useful for passing a ``target_tokens`` argument to the compactor.

        Returns:
            Number of tokens remaining before the hard-stop limit.
        """
        hard_stop_limit = int(self._context_length * self._thresholds.hard_stop_ratio)
        return hard_stop_limit - self._total_tokens

    def compaction_target_tokens(self) -> int:
        """Return the post-compaction token target for context reducers.

        This is deliberately based on the warning threshold, not on the current
        hard-stop headroom.  Headroom can be very small once compaction is
        triggered, and using it as a target would over-compress useful context.
        """
        return max(1, int(self._context_length * self._thresholds.warn_ratio))

    # ── Mutation API ───────────────────────────────────────────────────

    def reset(self) -> None:
        """Clear all stage token counts and the running total.

        Call this after a full context compaction so the budget reflects
        the reduced token usage. Does not alter thresholds or context_length.
        """
        self._stage_tokens.clear()
        self._total_tokens = 0
        logger.debug("budget.reset: all stage token counts cleared")

    def reset_stage(self, stage: str) -> None:
        """Clear token tracking for a single pipeline stage.

        Removes the stage's cumulative count from both the stage map and the
        running total. If the stage was never recorded, this is a no-op.

        Args:
            stage: Name of the pipeline stage to clear.
        """
        removed = self._stage_tokens.pop(stage, 0)
        self._total_tokens = max(0, self._total_tokens - removed)
        logger.debug("budget.reset_stage: cleared stage=%r (%d tokens removed)", stage, removed)

    # ── Serialisation ──────────────────────────────────────────────────

    def to_dict(self) -> dict[str, Any]:
        """Return a dashboard-friendly snapshot of the current budget state.

        Suitable for JSON serialisation, API responses, and health-check
        endpoints. Ratios are rounded to four decimal places.

        Returns:
            Dictionary with keys: context_length, total_tokens, usage_ratio,
            remaining_tokens, status, thresholds, stages.
        """
        ratio = self._total_tokens / self._context_length if self._context_length > 0 else 0.0
        status = self._classify(ratio)
        hard_stop_limit = int(self._context_length * self._thresholds.hard_stop_ratio)
        return {
            "context_length": self._context_length,
            "total_tokens": self._total_tokens,
            "usage_ratio": round(ratio, 4),
            "remaining_tokens": hard_stop_limit - self._total_tokens,
            "status": status.value,
            "thresholds": {
                "warn_ratio": self._thresholds.warn_ratio,
                "compact_ratio": self._thresholds.compact_ratio,
                "hard_stop_ratio": self._thresholds.hard_stop_ratio,
            },
            "stages": {
                stage: {"tokens": used, "ratio": round(used / self._context_length, 4)}
                for stage, used in sorted(self._stage_tokens.items(), key=lambda kv: kv[1], reverse=True)
            },
        }

    # ── Private helpers ────────────────────────────────────────────────

    def _classify(self, ratio: float) -> BudgetStatus:
        """Map a usage ratio to a BudgetStatus enum value.

        Args:
            ratio: Current usage fraction (total_tokens / context_length).

        Returns:
            The appropriate BudgetStatus for this ratio.
        """
        if ratio >= self._thresholds.hard_stop_ratio:
            return BudgetStatus.EXCEEDED
        if ratio >= self._thresholds.compact_ratio:
            return BudgetStatus.COMPACTION_NEEDED
        if ratio >= self._thresholds.warn_ratio:
            return BudgetStatus.WARNING
        return BudgetStatus.OK

    @staticmethod
    def _build_message(status: BudgetStatus, ratio: float, remaining: int) -> str:
        """Compose a human-readable status message for a BudgetCheck.

        Args:
            status: The evaluated BudgetStatus.
            ratio: Current usage ratio (0.0-1.0+).
            remaining: Tokens left before hard stop.

        Returns:
            A single-line plain-English status string.
        """
        pct = ratio * 100
        if status == BudgetStatus.OK:
            return f"OK — {pct:.1f}% used, {remaining} tokens remaining before hard stop"
        if status == BudgetStatus.WARNING:
            return f"WARNING — {pct:.1f}% used; compact soon ({remaining} tokens before hard stop)"
        if status == BudgetStatus.COMPACTION_NEEDED:
            return f"COMPACTION NEEDED — {pct:.1f}% used; compact before next handoff ({remaining} tokens left)"
        return f"EXCEEDED — {pct:.1f}% used; no new work accepted ({abs(remaining)} tokens over limit)"


# ── Factory function ───────────────────────────────────────────────────


def create_budget_for_model(
    model_id: str,
    thresholds: BudgetThresholds | None = None,
) -> ContextBudget:
    """Create a ContextBudget sized to the model's known context window.

    Looks up the model's context_length from ``_MODEL_CONTEXT_WINDOWS`` in
    ``vetinari.context.window_manager``. Falls back to the "default" entry
    (32768 tokens) for unrecognised model identifiers.

    Args:
        model_id: Model identifier string (e.g. ``"qwen2.5-coder-7b"``).
        thresholds: Optional custom threshold ratios. Defaults to 70/85/95%.

    Returns:
        A freshly initialised ContextBudget with no recorded usage.
    """
    from vetinari.context.window_manager import _MODEL_CONTEXT_WINDOWS
    from vetinari.testing.context_window import get_effective_window

    # Prefer measured effective window over static lookup table
    effective = get_effective_window(model_id)
    if effective is not None:
        context_length = effective
        logger.debug(
            "create_budget_for_model: model_id=%r using measured effective_window=%d",
            model_id,
            context_length,
        )
    else:
        context_length = _MODEL_CONTEXT_WINDOWS.get(model_id, _MODEL_CONTEXT_WINDOWS["default"])
        logger.debug(
            "create_budget_for_model: model_id=%r using static context_length=%d",
            model_id,
            context_length,
        )
    return ContextBudget(context_length=context_length, thresholds=thresholds)


__all__ = [
    "BudgetCheck",
    "BudgetStatus",
    "BudgetThresholds",
    "ContextBudget",
    "StageUsage",
    "create_budget_for_model",
]
