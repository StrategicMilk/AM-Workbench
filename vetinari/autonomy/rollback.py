"""Rollback registry — tracks autonomous actions for quality-regression rollback.

Every approved autonomous action is logged with a quality baseline. If quality
regresses >5% within 24 hours, the action type is automatically demoted and
the action marked as rolled back. Manual undo is available via the API.

Pipeline role: safety net for the autonomy governor — catches actions that
passed the confidence gate but degraded quality after execution.
"""

from __future__ import annotations

import logging
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)


# Absolute quality drop that triggers automatic rollback and demotion
_REGRESSION_THRESHOLD = 0.05  # 5% absolute drop

# How far back to look when checking for quality regression
_REGRESSION_WINDOW_HOURS = 24


# -- ActionRecord --------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ActionRecord:
    """Immutable record of a single autonomous action logged for rollback tracking.

    Captures the quality baseline at execution time so later quality checks can
    detect regression. ``quality_after`` and ``rolled_back`` are set via
    ``dataclasses.replace()`` because the dataclass is frozen.

    Attributes:
        action_id: Unique identifier for this action (``undo_<12-hex>`` format).
        action_type: The kind of action (e.g. ``"parameter_tuning"``).
        timestamp: When the action was logged (ISO 8601 UTC).
        reversible_data: Opaque metadata describing what can be undone.
        quality_before: Quality score measured immediately before execution.
        quality_after: Quality score measured after execution; None until checked.
        rolled_back: True if this action has been undone or marked for reversal.
    """

    action_id: str
    action_type: str
    timestamp: str
    reversible_data: dict[str, Any]
    quality_before: float
    quality_after: float | None = None
    rolled_back: bool = False

    def __repr__(self) -> str:
        return "ActionRecord(...)"


# -- RollbackRegistry ----------------------------------------------------------


class RollbackRegistry:
    """In-memory registry that tracks autonomous actions for quality-regression rollback.

    Stores one ActionRecord per logged action. Quality checks compare the
    stored baseline against a current score; if the drop exceeds
    ``_REGRESSION_THRESHOLD``, affected actions are marked rolled back and the
    governor auto-demotes that action type.

    All public methods are thread-safe via a single ``threading.Lock``.
    """

    def __init__(self) -> None:
        # Maps action_id -> ActionRecord; protected by _lock for all mutations
        self._actions: dict[str, ActionRecord] = {}
        self._lock = threading.Lock()

    def log_autonomous_action(
        self,
        action_type: str,
        reversible_data: dict[str, Any],
        quality_before: float,
    ) -> str:
        """Create and store an ActionRecord for an approved autonomous action.

        Should be called immediately after the governor approves an action,
        before the action executes, so the quality baseline is accurate.

        Args:
            action_type: The kind of action being executed (e.g. ``"prompt_optimization"``).
            reversible_data: Opaque dict describing how this action can be undone.
            quality_before: Quality score measured before the action runs.

        Returns:
            The unique action_id for this record (``undo_<12-hex>`` format).
        """
        action_id = f"undo_{uuid.uuid4().hex[:12]}"
        timestamp = datetime.now(timezone.utc).isoformat()
        record = ActionRecord(
            action_id=action_id,
            action_type=action_type,
            timestamp=timestamp,
            reversible_data=reversible_data,
            quality_before=quality_before,
        )
        with self._lock:
            self._actions[action_id] = record

        logger.info(
            "Logged autonomous action %s (type=%s, quality_before=%.3f)",
            action_id,
            action_type,
            quality_before,
        )
        return action_id

    def update_quality_after(self, action_id: str, quality_after: float) -> bool:
        """Record the post-execution quality score for a logged action.

        Should be called after the action completes and a quality measurement
        is available. This populates the data that ``check_quality_regression``
        uses for comparison.

        Args:
            action_id: The action whose quality result is being recorded.
            quality_after: Quality score measured after the action executed.

        Returns:
            True if the record was found and updated, False if action_id is unknown.
        """
        with self._lock:
            record = self._actions.get(action_id)
            if record is None:
                logger.warning(
                    "update_quality_after called for unknown action %s — record not found",
                    action_id,
                )
                return False
            self._actions[action_id] = _replace_record(record, quality_after=quality_after)

        logger.info(
            "Updated quality_after for action %s: %.3f",
            action_id,
            quality_after,
        )
        return True

    def check_quality_regression(
        self,
        action_type: str,
        current_quality: float,
    ) -> list[str]:
        """Detect and respond to quality regression for a given action type.

        Scans all non-rolled-back actions of ``action_type`` from the past
        ``_REGRESSION_WINDOW_HOURS`` hours. If any action shows
        ``quality_before - current_quality > _REGRESSION_THRESHOLD`` (a >5%
        absolute drop), those actions are marked rolled back and the governor
        auto-demotes the action type.

        Args:
            action_type: The action type to evaluate for regression.
            current_quality: The quality score measured right now.

        Returns:
            List of action_ids that were marked rolled back in this call.
            Empty if no regression was detected.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(hours=_REGRESSION_WINDOW_HOURS)
        rolled_back_ids: list[str] = []

        with self._lock:
            candidates = [
                r
                for r in self._actions.values()
                if (
                    r.action_type == action_type and not r.rolled_back and datetime.fromisoformat(r.timestamp) >= cutoff
                )
            ]

            for record in candidates:
                delta = record.quality_before - current_quality
                if delta > _REGRESSION_THRESHOLD:
                    self._actions[record.action_id] = _replace_record(record, rolled_back=True)
                    rolled_back_ids.append(record.action_id)
                    logger.warning(
                        "Quality regression detected for action_type=%s: "
                        "quality_before=%.3f current=%.3f delta=%.3f — "
                        "marking action %s rolled back",
                        action_type,
                        record.quality_before,
                        current_quality,
                        delta,
                        record.action_id,
                    )

        if rolled_back_ids:
            self.auto_rollback(action_type, len(rolled_back_ids))

        return rolled_back_ids

    def auto_rollback(self, action_type: str, rollback_count: int) -> None:
        """Demote an action type via the governor after quality regression.

        Called automatically by ``check_quality_regression`` when rolled-back
        actions are detected. Separated as a public method so callers can
        trigger demotion directly when they detect regression through other
        means (e.g. external monitoring).

        Args:
            action_type: The action type to demote.
            rollback_count: Number of actions that triggered the rollback (for logging).
        """
        # Late import to avoid circular dependency at module load time
        from vetinari.autonomy.governor import get_governor

        get_governor()._auto_demote(action_type)
        logger.warning(
            "Auto-demoted action_type=%s after %d regression rollback(s)",
            action_type,
            rollback_count,
        )

    def undo_action(self, action_id: str) -> ActionRecord | None:
        """Manually mark a specific action as rolled back.

        Used by the API to support human-initiated undo. Does not call the
        governor — manual undo does not trigger demotion.

        Args:
            action_id: The action to mark as undone.

        Returns:
            The updated ActionRecord with ``rolled_back=True``, or None if
            action_id is not found.
        """
        with self._lock:
            record = self._actions.get(action_id)
            if record is None:
                return None
            updated = _replace_record(record, rolled_back=True)
            self._actions[action_id] = updated

        logger.info("Manually rolled back action %s (type=%s)", action_id, record.action_type)
        return updated

    def get_action(self, action_id: str) -> ActionRecord | None:
        """Return the ActionRecord for a given action_id, or None if not found.

        Args:
            action_id: The action to retrieve.

        Returns:
            ActionRecord if found, None otherwise.
        """
        with self._lock:
            return self._actions.get(action_id)

    def get_recent_actions(self, hours: int = 24) -> list[ActionRecord]:
        """Return all actions logged within the past N hours, newest first.

        Args:
            hours: How many hours back to look. Defaults to 24.

        Returns:
            List of ActionRecord objects ordered by timestamp descending.
        """
        if hours <= 0:
            return []
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        with self._lock:
            recent = [r for r in self._actions.values() if datetime.fromisoformat(r.timestamp) >= cutoff]

        # Preserve "most recently logged wins" when timestamps are identical by
        # iterating in reverse insertion order before the stable timestamp sort.
        recent.reverse()
        recent.sort(key=lambda r: r.timestamp, reverse=True)
        return recent


# -- Private helpers -----------------------------------------------------------


def _replace_record(record: ActionRecord, **changes: Any) -> ActionRecord:
    """Return a copy of an ActionRecord with the specified fields replaced.

    Works around ``frozen=True`` by using keyword-argument unpacking. Only
    mutable fields (``quality_after``, ``rolled_back``) should be passed here.

    Args:
        record: The frozen ActionRecord to copy.
        **changes: Field name / new value pairs to apply.

    Returns:
        A new ActionRecord with the changes applied and all other fields
        copied from the original.
    """
    return ActionRecord(
        action_id=record.action_id,
        action_type=record.action_type,
        timestamp=record.timestamp,
        reversible_data=record.reversible_data,
        quality_before=record.quality_before,
        quality_after=changes.get("quality_after", record.quality_after),
        rolled_back=changes.get("rolled_back", record.rolled_back),
    )


# -- Singleton -----------------------------------------------------------------

_registry: RollbackRegistry | None = None
_registry_lock = threading.Lock()


def get_rollback_registry() -> RollbackRegistry:
    """Get or create the singleton RollbackRegistry.

    Uses double-checked locking to avoid creating multiple instances under
    concurrent first-call conditions.

    Returns:
        The singleton RollbackRegistry instance.
    """
    global _registry
    if _registry is None:
        with _registry_lock:
            if _registry is None:
                _registry = RollbackRegistry()
    return _registry
