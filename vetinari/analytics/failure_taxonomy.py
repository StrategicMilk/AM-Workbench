"""AgentRx failure taxonomy — structured categorization of agent failures.

Nine failure categories for structured root cause analysis:
1. Plan Adherence Failure — agent deviated from the approved plan
2. Hallucination — agent produced factually incorrect output
3. Invalid Tool Invocation — wrong tool, wrong args, or nonexistent tool
4. Misinterpretation of Tool Output — agent misunderstood tool results
5. Intent-Plan Misalignment — plan doesn't match the original goal
6. Under-specified Intent — insufficient information to proceed
7. Intent Not Supported — goal is outside agent capabilities
8. Guardrails Triggered — safety or policy check blocked the action
9. System Failure — infrastructure error (OOM, timeout, adapter crash)

This module is step 4 of the analytics pipeline:
Inference → Orchestration → Quality Gate → **Failure Classification** → Remediation.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from vetinari.exceptions import (
    GuardrailError,
    InferenceError,
    ModelUnavailableError,
    SandboxError,
    SecurityError,
    VetinariTimeoutError,
)
from vetinari.types import AgentRxFailureCategory

logger = logging.getLogger(__name__)


# Maximum number of failure records retained in the bounded deque.
_MAX_RECORDS = 1000

# Seconds in an hour — used when computing trend windows.
_SECONDS_PER_HOUR = 3600.0

# Keywords that indicate an invalid tool call when found in a ValueError message.
_TOOL_KEYWORDS = frozenset({"tool", "function", "invoke", "call"})

# Keywords that indicate a budget or token exhaustion failure inside InferenceError.
_BUDGET_KEYWORDS = frozenset({"budget", "token", "limit", "quota"})

# Keywords that indicate a circuit-breaker or infrastructure failure.
_CIRCUIT_KEYWORDS = frozenset({"circuit", "breaker", "unavailable", "overload"})

# Keywords for semantic failure categories that used to fall through to the
# plan-adherence default.
_HALLUCINATION_KEYWORDS = frozenset({"hallucination", "hallucinated", "fabricated", "false claim", "unsupported fact"})
_INTENT_MISMATCH_KEYWORDS = frozenset({
    "intent mismatch",
    "misaligned intent",
    "wrong goal",
    "does not match user intent",
})
_UNDER_SPECIFIED_KEYWORDS = frozenset({"under-specified", "underspecified", "missing requirement", "ambiguous intent"})
_UNSUPPORTED_INTENT_KEYWORDS = frozenset({
    "unsupported intent",
    "unsupported goal",
    "outside capability",
    "cannot support",
})

# Valid severity levels for FailureRecord.
_VALID_SEVERITIES = frozenset({"warning", "error", "critical"})


# ---------------------------------------------------------------------------
# FailureRecord dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FailureRecord:
    """Immutable record describing a single classified agent failure.

    Args:
        category: The AgentRx failure category assigned by the classifier.
        task_id: Identifier of the task that failed.
        agent_type: String value of the AgentType that was executing.
        description: Human-readable description of what failed.
        timestamp: Wall-clock time (``time.time()``) when the failure occurred.
        context: Optional dict of additional diagnostic data (e.g., exception
            message, tool name, model ID). Must be truly dynamic — use typed
            fields above for structured data.
        severity: One of ``"warning"``, ``"error"``, or ``"critical"``.
    """

    category: AgentRxFailureCategory
    task_id: str
    agent_type: str
    description: str
    timestamp: float
    context: dict[str, Any] = field(default_factory=dict)
    severity: str = "error"  # warning | error | critical

    def __post_init__(self) -> None:
        if self.severity not in _VALID_SEVERITIES:
            raise ValueError(f"Invalid severity {self.severity!r} — must be one of {sorted(_VALID_SEVERITIES)}")

    def __repr__(self) -> str:
        return (
            f"FailureRecord(category={self.category.value!r}, task_id={self.task_id!r}, "
            f"agent_type={self.agent_type!r}, severity={self.severity!r})"
        )


# ---------------------------------------------------------------------------
# FailureClassifier
# ---------------------------------------------------------------------------


class FailureClassifier:
    """Heuristic classifier that maps exceptions to AgentRx failure categories.

    Classification is based on exception type and optional context keys.
    The rules are ordered from most specific to least specific; the first
    matching rule wins.
    """

    def classify(
        self,
        error: Exception,
        context: dict[str, Any],
    ) -> AgentRxFailureCategory:
        """Map an exception and its context to an AgentRx failure category.

        Rules applied in order (first match wins):
        - Explicit context category/failure_type -> matching taxonomy category
        - Hallucination keywords/flags -> HALLUCINATION
        - Intent mismatch/under-specified/unsupported keywords/flags -> intent category
        - ``VetinariTimeoutError`` or ``TimeoutError`` -> SYSTEM_FAILURE
        - ``ModelUnavailableError`` -> SYSTEM_FAILURE
        - ``InferenceError`` with budget/token keywords -> SYSTEM_FAILURE
        - ``InferenceError`` with circuit-breaker keywords -> SYSTEM_FAILURE
        - ``GuardrailError``, ``SandboxError``, ``SecurityError`` -> GUARDRAILS_TRIGGERED
        - ``ValueError`` with tool/function keywords -> INVALID_TOOL_INVOCATION
        - ``KeyError`` with a tool-output context flag -> MISINTERPRETATION_OF_TOOL_OUTPUT
        - Default -> PLAN_ADHERENCE_FAILURE

        Args:
            error: The exception that caused the failure.
            context: Diagnostic context dict; may contain ``"tool_output"``
                to signal a tool-result parsing failure.

        Returns:
            The most appropriate ``AgentRxFailureCategory`` value.
        """
        explicit = str(context.get("category") or context.get("failure_type") or "").strip()
        if explicit:
            try:
                return AgentRxFailureCategory(explicit)
            except ValueError:
                logger.debug("Unknown explicit failure category %r; falling back to classifier rules", explicit)

        msg = str(error).lower()
        text = " ".join(
            str(context.get(key, "")).lower()
            for key in ("description", "analysis", "root_cause", "observed", "expected")
        )
        combined = f"{msg} {text}"

        if context.get("hallucination") is True or any(kw in combined for kw in _HALLUCINATION_KEYWORDS):
            return AgentRxFailureCategory.HALLUCINATION
        if context.get("intent_plan_mismatch") is True or any(kw in combined for kw in _INTENT_MISMATCH_KEYWORDS):
            return AgentRxFailureCategory.INTENT_PLAN_MISALIGNMENT
        if context.get("under_specified_intent") is True or any(kw in combined for kw in _UNDER_SPECIFIED_KEYWORDS):
            return AgentRxFailureCategory.UNDER_SPECIFIED_INTENT
        if context.get("intent_not_supported") is True or any(kw in combined for kw in _UNSUPPORTED_INTENT_KEYWORDS):
            return AgentRxFailureCategory.INTENT_NOT_SUPPORTED

        if isinstance(error, (VetinariTimeoutError, TimeoutError)):
            return AgentRxFailureCategory.SYSTEM_FAILURE

        if isinstance(error, ModelUnavailableError):
            return AgentRxFailureCategory.SYSTEM_FAILURE

        if isinstance(error, InferenceError):
            if any(kw in msg for kw in _BUDGET_KEYWORDS):
                return AgentRxFailureCategory.SYSTEM_FAILURE
            if any(kw in msg for kw in _CIRCUIT_KEYWORDS):
                return AgentRxFailureCategory.SYSTEM_FAILURE

        if isinstance(error, (GuardrailError, SandboxError, SecurityError)):
            return AgentRxFailureCategory.GUARDRAILS_TRIGGERED

        if isinstance(error, ValueError):
            msg = str(error).lower()
            if any(kw in msg for kw in _TOOL_KEYWORDS):
                return AgentRxFailureCategory.INVALID_TOOL_INVOCATION

        if isinstance(error, KeyError) and context.get("tool_output") is not None:
            return AgentRxFailureCategory.MISINTERPRETATION_OF_TOOL_OUTPUT

        # Default: most common agent failure is deviating from the plan.
        return AgentRxFailureCategory.PLAN_ADHERENCE_FAILURE


# ---------------------------------------------------------------------------
# FailureTracker singleton
# ---------------------------------------------------------------------------


class FailureTracker:
    """Thread-safe store for classified agent failure records.

    Maintains a bounded deque of ``FailureRecord`` instances (max 1 000).
    Provides breakdown and trend queries for dashboards and Kaizen loops.

    This class should not be instantiated directly; use
    :func:`get_failure_tracker` to obtain the process-wide singleton.

    Side effects:
        - Uses ``threading.Lock`` to protect concurrent writes/reads.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # Bounded ring buffer — oldest entries evicted automatically.
        self._records: deque[FailureRecord] = deque(maxlen=_MAX_RECORDS)
        self._classifier = FailureClassifier()

    # -- write -----------------------------------------------------------------

    def record(self, record: FailureRecord) -> None:
        """Append a pre-classified failure record to the store.

        Logs the failure at the appropriate level so operators see failures
        in the log stream without querying the store.

        Args:
            record: A fully-constructed ``FailureRecord`` to persist.
        """
        with self._lock:
            self._records.append(record)

        log_fn = logger.warning if record.severity == "warning" else logger.error
        log_fn(
            "Agent failure recorded — category=%s task_id=%s agent=%s: %s",
            record.category.value,
            record.task_id,
            record.agent_type,
            record.description,
        )

    def classify_and_record(
        self,
        error: Exception,
        task_id: str,
        agent_type: str,
        context: dict[str, Any] | None = None,
        severity: str = "error",
    ) -> FailureRecord:
        """Classify an exception and persist the resulting failure record.

        Convenience method that combines :class:`FailureClassifier` and
        :meth:`record` into a single call.

        Args:
            error: The exception that caused the failure.
            task_id: Identifier of the failing task.
            agent_type: String value of the agent type that was executing.
            context: Optional diagnostic data to attach to the record.
            severity: One of ``"warning"``, ``"error"``, or ``"critical"``.

        Returns:
            The newly created and stored ``FailureRecord``.
        """
        ctx: dict[str, Any] = context or {}
        category = self._classifier.classify(error, ctx)
        record = FailureRecord(
            category=category,
            task_id=task_id,
            agent_type=agent_type,
            description=str(error),
            timestamp=time.time(),
            context=ctx,
            severity=severity,
        )
        self.record(record)
        return record

    # -- read ------------------------------------------------------------------

    def get_by_category(
        self,
        category: AgentRxFailureCategory,
    ) -> list[FailureRecord]:
        """Return all stored records matching the given failure category.

        Args:
            category: The ``AgentRxFailureCategory`` to filter by.

        Returns:
            A list of matching ``FailureRecord`` instances in insertion order.
        """
        with self._lock:
            return [r for r in self._records if r.category is category]

    def get_breakdown(self) -> dict[str, int]:
        """Return a per-category count of all stored failure records.

        Returns:
            A dict mapping each ``AgentRxFailureCategory.value`` string to its
            count.  Categories with zero failures are included so callers can
            render a complete table without defensive checks.
        """
        counts: dict[str, int] = {cat.value: 0 for cat in AgentRxFailureCategory}
        with self._lock:
            for record in self._records:
                counts[record.category.value] += 1
        return counts

    def get_trends(self, window_hours: int = 24) -> dict[str, int]:
        """Return per-category failure counts within a recent time window.

        Only records whose ``timestamp`` falls within the last
        ``window_hours`` hours are counted.

        Args:
            window_hours: How far back to look.  Defaults to 24 hours.

        Returns:
            A dict mapping each ``AgentRxFailureCategory.value`` string to its
            count within the window.  All categories are present even when
            their count is zero.
        """
        cutoff = time.time() - window_hours * _SECONDS_PER_HOUR
        counts: dict[str, int] = {cat.value: 0 for cat in AgentRxFailureCategory}
        with self._lock:
            for record in self._records:
                if record.timestamp >= cutoff:
                    counts[record.category.value] += 1
        return counts

    def reset(self) -> None:
        """Clear all stored records.

        Intended for test teardown and controlled resets only.
        """
        with self._lock:
            self._records.clear()


# ---------------------------------------------------------------------------
# Singleton management
# ---------------------------------------------------------------------------

_tracker: FailureTracker | None = None
_tracker_lock = threading.Lock()


def get_failure_tracker() -> FailureTracker:
    """Return the process-wide ``FailureTracker`` singleton.

    Thread-safe via double-checked locking. The instance is created on the
    first call and reused on all subsequent calls.

    Returns:
        The shared ``FailureTracker`` instance.
    """
    global _tracker
    if _tracker is None:
        with _tracker_lock:
            if _tracker is None:
                _tracker = FailureTracker()
    return _tracker


def reset_failure_tracker() -> None:
    """Destroy the singleton so the next call creates a fresh instance.

    Intended for test isolation. The existing instance is cleared before
    being discarded so in-flight threads see an empty store.
    """
    global _tracker
    with _tracker_lock:
        if _tracker is not None:
            _tracker.reset()
        _tracker = None
