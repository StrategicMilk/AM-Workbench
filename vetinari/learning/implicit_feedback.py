"""Implicit feedback collector — learns from user actions without asking.

Tracks three signals per pipeline output: accepted (user proceeds without
changes), edited (user modifies — capture the diff as training data), and
regenerated (user requests a new output — negative signal). Feeds signals
into the quality scorer for score adjustment and into the context graph's
USER quadrant for preference learning.

Only asks explicit questions when implicit behavior contradicts the
Inspector's quality assessment (e.g. user edits output Inspector rated highly).
"""

from __future__ import annotations

import logging
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from vetinari.learning.feedback_store import FeedbackStore, get_feedback_store, reset_feedback_store_for_test
from vetinari.types import FeedbackAction

logger = logging.getLogger(__name__)


# -- Data types ---------------------------------------------------------------


@dataclass
class FeedbackSignal:
    """A single implicit feedback event from user behavior.

    Args:
        signal_id: Unique identifier for this signal.
        task_id: The task whose output received feedback.
        model_id: The model that produced the output.
        task_type: Category of the task (e.g. "code", "docs").
        action: What the user did (accepted, edited, regenerated).
        edit_diff: For EDITED actions, the diff between original and user version.
        inspector_score: The Inspector's quality assessment (if available).
        timestamp: When the action occurred.
        metadata: Additional context (e.g. time-to-action, edit size).
    """

    signal_id: str = field(default_factory=lambda: str(uuid.uuid4())[:12])
    task_id: str = ""
    model_id: str = ""
    task_type: str = ""
    action: FeedbackAction = FeedbackAction.ACCEPTED
    edit_diff: str | None = None
    inspector_score: float | None = None
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    metadata: dict[str, Any] = field(default_factory=dict)

    def __repr__(self) -> str:
        return (
            f"FeedbackSignal(id={self.signal_id!r}, task={self.task_id!r}, "
            f"action={self.action.value!r}, model={self.model_id!r})"
        )


@dataclass
class FeedbackSummary:
    """Aggregated feedback statistics for a model+task_type combination.

    Args:
        model_id: The model these stats describe.
        task_type: The task type these stats describe.
        accept_count: Number of accepted outputs.
        edit_count: Number of edited outputs.
        regenerate_count: Number of regenerated outputs.
        acceptance_rate: Fraction of outputs accepted without changes.
        common_edit_patterns: Recurring patterns in edit diffs.
    """

    model_id: str
    task_type: str
    accept_count: int = 0
    edit_count: int = 0
    regenerate_count: int = 0
    acceptance_rate: float = 0.0
    common_edit_patterns: list[str] = field(default_factory=list)

    def __repr__(self) -> str:
        total = self.accept_count + self.edit_count + self.regenerate_count
        return (
            f"FeedbackSummary(model={self.model_id!r}, task_type={self.task_type!r}, "
            f"total={total}, acceptance_rate={self.acceptance_rate:.0%})"
        )


# -- Implicit feedback collector ----------------------------------------------


# Lazy getters to avoid circular imports
_context_graph_fn = None
_context_graph_available = True
_REHYDRATE_LIMIT = 10000


def _get_context_graph():
    """Return the ContextGraph singleton, importing once on first call."""
    global _context_graph_fn, _context_graph_available
    if not _context_graph_available:
        return None
    if _context_graph_fn is not None:
        return _context_graph_fn()
    try:
        from vetinari.awareness.context_graph import get_context_graph

        _context_graph_fn = get_context_graph
        return _context_graph_fn()
    except ImportError:
        logger.warning("Context graph not available — implicit feedback will not update user preferences")
        _context_graph_available = False
        return None


class ImplicitFeedbackCollector:
    """Collects and aggregates implicit feedback signals from user behavior.

    Thread-safe. Signals are stored in memory with periodic summarization.
    Feeds into the context graph USER quadrant and quality scoring adjustments.

    Side effects:
        - Updates context graph USER quadrant via record_user_signal().
        - Logs contradictions between user actions and Inspector scores.
    """

    _CONTRADICTION_THRESHOLD = 0.3  # Score gap that triggers a contradiction alert

    def __init__(self, store: FeedbackStore | None = None) -> None:
        self._lock = threading.Lock()
        self._signals: list[FeedbackSignal] = []
        self._stats: dict[tuple[str, str], dict[str, int]] = {}  # (model_id, task_type) → counts
        self._store = store if store is not None else get_feedback_store()
        self._rehydrate_signals_from_store()

    def record(
        self,
        task_id: str,
        model_id: str,
        task_type: str,
        action: FeedbackAction | str,
        edit_diff: str | None = None,
        inspector_score: float | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> FeedbackSignal:
        """Record an implicit feedback signal from user behavior.

        Args:
            task_id: The task whose output received feedback.
            model_id: The model that produced the output.
            task_type: Category of the task.
            action: What the user did (accepted, edited, regenerated).
            edit_diff: For edits, the diff between original and user version.
            inspector_score: The Inspector's quality assessment, if available.
            metadata: Additional context.

        Returns:
            The created FeedbackSignal.
        """
        normalized_action = action if isinstance(action, FeedbackAction) else FeedbackAction(action)
        metadata_dict = dict(metadata) if metadata is not None else {}
        signal = FeedbackSignal(
            task_id=task_id,
            model_id=model_id,
            task_type=task_type,
            action=normalized_action,
            edit_diff=edit_diff,
            inspector_score=inspector_score,
            metadata=metadata_dict,
        )
        signal_dict = self._signal_to_dict(signal)

        with self._lock:
            self._store.append_signal(signal_dict)
            self._signals.append(signal)
            key = (model_id, task_type)
            if key not in self._stats:
                self._stats[key] = {"accepted": 0, "edited": 0, "regenerated": 0}
            self._stats[key][normalized_action.value] += 1

        # Push signal to context graph USER quadrant
        self._update_context_graph(signal)

        # Check for contradictions between user action and Inspector score
        self._check_contradiction(signal)

        logger.info(
            "Implicit feedback: task=%s model=%s action=%s",
            task_id,
            model_id,
            normalized_action.value,
        )
        return signal

    def _rehydrate_signals_from_store(self) -> None:
        """Restore recent signals from the durable store into the in-memory cache."""
        rows = self._store.list_signals(limit=_REHYDRATE_LIMIT)
        signals = [self._signal_from_dict(row) for row in reversed(rows)]
        with self._lock:
            self._signals = signals
            self._stats = {}
            for signal in signals:
                key = (signal.model_id, signal.task_type)
                if key not in self._stats:
                    self._stats[key] = {"accepted": 0, "edited": 0, "regenerated": 0}
                self._stats[key][signal.action.value] += 1

    @staticmethod
    def _signal_to_dict(signal: FeedbackSignal) -> dict[str, Any]:
        return {
            "signal_id": signal.signal_id,
            "task_id": signal.task_id,
            "model_id": signal.model_id,
            "task_type": signal.task_type,
            "action": signal.action.value,
            "edit_diff": signal.edit_diff,
            "inspector_score": signal.inspector_score,
            "timestamp": signal.timestamp,
            "metadata": dict(signal.metadata),
        }

    @staticmethod
    def _signal_from_dict(row: dict[str, Any]) -> FeedbackSignal:
        return FeedbackSignal(
            signal_id=str(row.get("signal_id", "")),
            task_id=str(row.get("task_id", "")),
            model_id=str(row.get("model_id", "")),
            task_type=str(row.get("task_type", "")),
            action=FeedbackAction(str(row.get("action", FeedbackAction.ACCEPTED.value))),
            edit_diff=row.get("edit_diff"),
            inspector_score=row.get("inspector_score"),
            timestamp=str(row.get("timestamp", datetime.now(timezone.utc).isoformat())),
            metadata=dict(row.get("metadata") or {}),
        )

    def get_summary(self, model_id: str, task_type: str) -> FeedbackSummary:
        """Get aggregated feedback stats for a model+task_type combination.

        Args:
            model_id: Model identifier.
            task_type: Task type string.

        Returns:
            FeedbackSummary with counts and acceptance rate.
        """
        with self._lock:
            key = (model_id, task_type)
            counts = self._stats.get(key, {"accepted": 0, "edited": 0, "regenerated": 0})
            total = sum(counts.values())
            acceptance_rate = counts["accepted"] / total if total > 0 else 0.0

        return FeedbackSummary(
            model_id=model_id,
            task_type=task_type,
            accept_count=counts["accepted"],
            edit_count=counts["edited"],
            regenerate_count=counts["regenerated"],
            acceptance_rate=acceptance_rate,
        )

    def get_summary_for_routing(self, model_id: str, task_type: object) -> FeedbackSummary:
        """Return a routing-safe FeedbackSummary for enum-or-string task types.

        Args:
            model_id: Model identifier used for routing or lookup.
            task_type: Task type value consumed by get_summary_for_routing().

        Returns:
            Resolved summary for routing value.
        """
        task_type_str = task_type.value if hasattr(task_type, "value") else str(task_type)
        try:
            return self.get_summary(model_id, task_type_str)
        except Exception:
            logger.warning(
                "Implicit feedback summary lookup failed for %s/%s; returning empty summary",
                model_id,
                task_type_str,
                exc_info=True,
            )
            return FeedbackSummary(model_id=model_id, task_type=task_type_str)

    def get_signals(self, task_id: str | None = None, limit: int = 50) -> list[FeedbackSignal]:
        """Retrieve recent feedback signals with optional filtering.

        Args:
            task_id: Filter by task ID (None returns all).
            limit: Maximum signals to return.

        Returns:
            List of FeedbackSignal instances, newest first.
        """
        with self._lock:
            signals = list(reversed(self._signals))
        if task_id is not None:
            signals = [s for s in signals if s.task_id == task_id]
        return signals[:limit]

    def should_ask_explicit_question(self, task_id: str) -> bool:
        """Determine if an explicit question should be asked based on contradictions.

        Only triggers when implicit behavior contradicts Inspector assessment.
        E.g., user edits output that Inspector rated highly (>0.8).

        Args:
            task_id: The task to check.

        Returns:
            True if a contradiction warrants an explicit question.
        """
        with self._lock:
            task_signals = [s for s in self._signals if s.task_id == task_id]

        for signal in task_signals:
            if signal.inspector_score is None:
                continue
            # User edited/regenerated but Inspector scored high
            if signal.action in (FeedbackAction.EDITED, FeedbackAction.REGENERATED) and signal.inspector_score > (
                1.0 - self._CONTRADICTION_THRESHOLD
            ):
                return True
            # User accepted but Inspector scored low
            if signal.action == FeedbackAction.ACCEPTED and signal.inspector_score < self._CONTRADICTION_THRESHOLD:
                return True
        return False

    def _update_context_graph(self, signal: FeedbackSignal) -> None:
        """Push feedback signal into the context graph USER quadrant.

        Args:
            signal: The feedback signal to propagate.
        """
        graph = _get_context_graph()
        if graph is None:
            return

        key = (signal.model_id, signal.task_type)
        with self._lock:
            counts = self._stats.get(key, {"accepted": 0, "edited": 0, "regenerated": 0})
            total = sum(counts.values())
            acceptance_rate = counts["accepted"] / total if total > 0 else 0.0

        # Update USER quadrant with latest acceptance rate for this model+task
        graph.record_user_signal(
            key=f"acceptance_rate_{signal.model_id}_{signal.task_type}",
            value=acceptance_rate,
            source="implicit_feedback",
            confidence=min(1.0, total / 20.0),  # Confidence grows with sample size
        )

        # Track edit patterns as user preferences
        if signal.action == FeedbackAction.EDITED and signal.edit_diff:
            graph.record_user_signal(
                key=f"last_edit_type_{signal.task_type}",
                value=signal.edit_diff[:200],
                source="implicit_feedback",
                confidence=0.6,
            )

    def _check_contradiction(self, signal: FeedbackSignal) -> None:
        """Log when user behavior contradicts Inspector quality assessment.

        Args:
            signal: The feedback signal to check.
        """
        if signal.inspector_score is None:
            return

        is_contradiction = False
        if signal.action in (FeedbackAction.EDITED, FeedbackAction.REGENERATED) and signal.inspector_score > (
            1.0 - self._CONTRADICTION_THRESHOLD
        ):
            is_contradiction = True
            logger.warning(
                "Contradiction: user %s output for task %s, but Inspector scored %.2f — "
                "Inspector may be over-scoring %s tasks",
                signal.action.value,
                signal.task_id,
                signal.inspector_score,
                signal.task_type,
            )
        elif signal.action == FeedbackAction.ACCEPTED and signal.inspector_score < self._CONTRADICTION_THRESHOLD:
            is_contradiction = True
            logger.warning(
                "Contradiction: user accepted output for task %s, but Inspector scored %.2f — "
                "Inspector may be under-scoring %s tasks",
                signal.task_id,
                signal.inspector_score,
                signal.task_type,
            )

        if is_contradiction:
            graph = _get_context_graph()
            if graph is not None:
                graph.record_relationship(
                    key=f"inspector_user_contradiction_{signal.task_type}",
                    value={
                        "task_id": signal.task_id,
                        "action": signal.action.value,
                        "inspector_score": signal.inspector_score,
                    },
                    source="implicit_feedback",
                    confidence=0.7,
                )


# -- Singleton ----------------------------------------------------------------

_instance: ImplicitFeedbackCollector | None = None
_singleton_lock = threading.Lock()


def get_implicit_feedback_collector() -> ImplicitFeedbackCollector:
    """Get or create the global ImplicitFeedbackCollector singleton.

    Returns:
        The singleton ImplicitFeedbackCollector instance.
    """
    global _instance
    if _instance is None:
        with _singleton_lock:
            if _instance is None:
                _instance = ImplicitFeedbackCollector()
    return _instance


def reset_implicit_feedback_collector() -> None:
    """Reset the singleton for testing.

    Returns:
        None.
    """
    global _instance
    with _singleton_lock:
        _instance = None
    reset_feedback_store_for_test(clear_default_store=True)
