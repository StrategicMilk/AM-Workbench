"""Tuning Coordinator — mediates between FeedbackLoop, MetaAdapter, and AutoTuner.

Prevents conflicting parameter adjustments by applying priority ordering:
FeedbackLoop > AutoTuner > MetaAdapter. Each system proposes changes;
the coordinator validates consistency before applying.

Decision: centralized priority-based coordination between FeedbackLoop,
AutoTuner, and MetaAdapter. (Prior ADR-0084 citation here was drift; ADR-0084
is the vLLM/NIMs optional-backend decision. No dedicated ADR exists for this
tuning-coordinator priority design; flagged during 2026-04-24 governance audit.)
"""

from __future__ import annotations

import logging
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from vetinari.boundary_guards import account_evidence_drop
from vetinari.security.redaction import redact_value
from vetinari.utils import privacy_receipt

logger = logging.getLogger(__name__)


# -- Module-level state --
# _coordinator: singleton instance, created on first call to get_tuning_coordinator().
# Written by: get_tuning_coordinator(). Read by: all callers.
# Protected by: _coordinator_lock (double-checked locking).
_coordinator: TuningCoordinator | None = None
_coordinator_lock = threading.Lock()

# Source -> priority mapping (lower number = higher priority).
# FeedbackLoop wins because it directly measures output quality.
# AutoTuner is operational (resource/latency signals). MetaAdapter is strategic (long-horizon).
_SOURCE_PRIORITY: dict[str, int] = {
    "feedback_loop": 1,
    "auto_tuner": 2,
    "meta_adapter": 3,
}


@dataclass(frozen=True, slots=True)
class ParameterChange:
    """A proposed parameter change from a tuning subsystem.

    Attributes:
        source: Subsystem proposing the change (feedback_loop, auto_tuner, meta_adapter).
        parameter: Dotted parameter name, e.g. "model_weight:qwen3:coding".
        old_value: Value before the change.
        new_value: Proposed new value.
        reasoning: Human-readable explanation of why this change is proposed.
        priority: Numeric priority derived from source (1=highest).
        timestamp: ISO-8601 UTC timestamp of when the change was proposed.
    """

    source: str
    parameter: str
    old_value: Any
    new_value: Any
    reasoning: str
    priority: int  # 1=highest (feedback_loop), 2=auto_tuner, 3=meta_adapter
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def __repr__(self) -> str:
        return f"ParameterChange(source={self.source!r}, parameter={self.parameter!r}, priority={self.priority!r})"


class TuningCoordinator:
    """Mediates between tuning subsystems to prevent conflicting parameter adjustments.

    Applies priority-based conflict resolution: when two subsystems propose
    changes to the same parameter within a coordination window, the
    higher-priority source wins and the lower-priority change is rejected.

    The coordination window (60 seconds by default) defines how long a
    parameter is considered "locked" after a change is applied. Changes
    outside this window are always accepted.

    Side effects:
      - Logs all accepted and rejected changes at INFO level.
      - Maintains a bounded change history (max 500 entries) for audit.
    """

    # How long (seconds) a parameter is held after a change before new
    # proposals from lower-priority sources are accepted.
    COORDINATION_WINDOW_SECONDS: int = 60

    def __init__(self) -> None:
        # _lock protects _pending, _history, and _active_params.
        self._lock = threading.Lock()
        self._pending: list[ParameterChange] = []
        # _history: append-only audit log, trimmed to last 500 entries.
        self._history: list[ParameterChange] = []
        # _active_params: parameter name -> most recently applied change.
        # Used to detect conflicts within the coordination window.
        self._active_params: dict[str, ParameterChange] = {}

    def propose(
        self,
        source: str,
        parameter: str,
        old_value: Any,
        new_value: Any,
        reasoning: str,
    ) -> bool:
        """Propose a parameter change and return whether it was accepted.

        If a higher-priority source changed the same parameter within the
        coordination window, the proposal is rejected and False is returned.
        If the incoming change has higher priority than an active change, it
        overrides the active change and True is returned.

        Args:
            source: Subsystem proposing the change. Must be one of
                "feedback_loop", "auto_tuner", or "meta_adapter". Unknown
                sources receive priority 99 (lowest).
            parameter: Parameter being changed, e.g. "model_weight:qwen3:coding".
            old_value: Current value before this change.
            new_value: Proposed replacement value.
            reasoning: Why this change is being proposed.

        Returns:
            True if the change was accepted and should be applied by the caller.
            False if a higher-priority change is active and this one is rejected.
        """
        priority = _SOURCE_PRIORITY.get(source, 99)
        change = ParameterChange(
            source=source,
            parameter=parameter,
            old_value=old_value,
            new_value=new_value,
            reasoning=reasoning,
            priority=priority,
        )

        with self._lock:
            existing = self._active_params.get(parameter)
            if existing is not None and not self._can_accept_conflict(change, existing):
                return False
            self._accept_change(change)
            return True

    def _can_accept_conflict(self, change: ParameterChange, existing: ParameterChange) -> bool:
        try:
            existing_time = datetime.fromisoformat(existing.timestamp)
            elapsed = (datetime.now(timezone.utc) - existing_time).total_seconds()
        except (ValueError, TypeError) as exc:
            logger.warning(
                "[TuningCoordinator] Could not parse timestamp on existing change for %r - rejecting new %s change: %s",
                change.parameter,
                change.source,
                exc,
            )
            return False
        if elapsed >= self.COORDINATION_WINDOW_SECONDS:
            return True
        if change.priority < existing.priority:
            logger.info(
                "[TuningCoordinator] Overriding active %s change to %r with higher-priority %s change",
                existing.source,
                change.parameter,
                change.source,
            )
            return True
        logger.info(
            "[TuningCoordinator] Rejected %s change to %r - conflicting %s change (priority %d) applied %.0fs ago",
            change.source,
            change.parameter,
            existing.source,
            existing.priority,
            elapsed,
        )
        return False

    def _accept_change(self, change: ParameterChange) -> None:
        self._active_params[change.parameter] = change
        self._history.append(change)
        if len(self._history) > 500:
            dropped = self._history[:-500]
            for item in dropped:
                account_evidence_drop(item, "tuning_coordinator", logger=logger)
            self._history = self._history[-500:]
        logger.info(
            "[TuningCoordinator] Accepted %s change: %r %s -> %s | %s",
            change.source,
            change.parameter,
            change.old_value,
            change.new_value,
            change.reasoning,
        )

    def propose_after_governance(
        self,
        governance_decision: object,
        *,
        source: str,
        parameter: str,
        old_value: Any,
        new_value: Any,
        reasoning: str,
    ) -> bool:
        """Propose a runtime default change only after self-improvement approval.

        Returns:
            bool value produced by propose_after_governance().
        """
        from vetinari.workbench.self_improvement import is_default_change_approved

        if not is_default_change_approved(governance_decision):
            logger.info(
                "[TuningCoordinator] Rejected %s change to %r - self-improvement governance did not approve it",
                source,
                parameter,
            )
            return False
        return self.propose(
            source=source,
            parameter=parameter,
            old_value=old_value,
            new_value=new_value,
            reasoning=reasoning,
        )

    def get_history(self, limit: int = 50) -> list[dict[str, Any]]:
        """Return recent coordination history, most recent first.

        Args:
            limit: Maximum number of entries to return. Clamped to [1, 500].

        Returns:
            List of ParameterChange dicts ordered most-recent-first.
        """
        limit = max(1, min(limit, 500))
        with self._lock:
            return [_privacy_safe_change_dict(c) for c in reversed(self._history[-limit:])]

    def get_active_params(self) -> dict[str, dict[str, Any]]:
        """Return currently active parameter overrides.

        An "active" override is the most recently accepted change for each
        parameter, regardless of whether it is still within the coordination
        window.

        Returns:
            Dict mapping parameter name to the most recent ParameterChange dict.
        """
        with self._lock:
            return {k: _privacy_safe_change_dict(v) for k, v in self._active_params.items()}


def get_tuning_coordinator() -> TuningCoordinator:
    """Return the singleton TuningCoordinator instance (thread-safe).

    Uses double-checked locking so the instance is created at most once
    even under concurrent first-call pressure.

    Returns:
        The shared TuningCoordinator instance.
    """
    global _coordinator
    if _coordinator is None:
        with _coordinator_lock:
            if _coordinator is None:
                _coordinator = TuningCoordinator()
    return _coordinator


def _privacy_safe_change_dict(change: ParameterChange) -> dict[str, Any]:
    row = asdict(change)
    row["old_value"] = redact_value(row.get("old_value"))
    row["new_value"] = redact_value(row.get("new_value"))
    row["reasoning"] = redact_value(row.get("reasoning"))
    row["privacy_receipt"] = privacy_receipt(
        privacy_class="operational",
        retention_days=30,
        source="tuning_coordinator.history",
        redaction_applied=True,
    )
    return row
