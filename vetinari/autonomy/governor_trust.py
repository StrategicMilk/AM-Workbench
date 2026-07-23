"""Progressive trust and promotion logic for the autonomy governor."""

from __future__ import annotations

import dataclasses
import logging
import threading
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from vetinari.autonomy.governor_levels import _demote_one_level, _promote_one_level
from vetinari.autonomy.governor_models import ActionPolicy, PendingPromotion, PromotionSuggestion
from vetinari.types import AutonomyLevel

logger = logging.getLogger(__name__)

_DEMOTION_CONSECUTIVE_FAILURES = 3
_VETO_WINDOW_HOURS = 1


class _GovernorTrustMixin:
    """Progressive trust records, promotion suggestions, and veto handling."""

    if TYPE_CHECKING:
        from vetinari.autonomy.governor_models import (
            ActionPolicy,
            PendingPromotion,
            TrustRecord,
        )
        from vetinari.types import AutonomyLevel

        _lock: threading.Lock
        _policies: dict[str, ActionPolicy]
        _trust_records: dict[str, TrustRecord]
        _pending_promotions: dict[str, PendingPromotion]
        _vetoed_actions: set[str]
        _default_level: AutonomyLevel

        def get_policy(self, action_type: str) -> ActionPolicy: ...

    def record_outcome(self, action_type: str, *, success: bool) -> None:
        """Record the outcome of an autonomous action for trust tracking.

        Args:
            action_type: The action type that was executed.
            success: Whether the action completed successfully.
        """
        promote_after_update = False
        with self._lock:
            record = self._trust_records[action_type]
            if success:
                updated = dataclasses.replace(
                    record,
                    total_actions=record.total_actions + 1,
                    successful_actions=record.successful_actions + 1,
                    consecutive_failures=0,
                )
                self._trust_records[action_type] = updated
                if updated.eligible_for_promotion:
                    promote_after_update = True
            else:
                new_failures = record.consecutive_failures + 1
                updated = dataclasses.replace(
                    record,
                    total_actions=record.total_actions + 1,
                    consecutive_failures=new_failures,
                )
                self._trust_records[action_type] = updated
                policy = self.get_policy(action_type)
                if (
                    policy.rollback_on_regression and new_failures >= 1
                ) or new_failures >= _DEMOTION_CONSECUTIVE_FAILURES:
                    self._auto_demote(action_type)
        if promote_after_update:
            self.auto_promote(action_type)

    def auto_promote(self, action_type: str) -> PendingPromotion | None:
        """Create a pending auto-promotion for an action type.

        Args:
            action_type: The action type to propose for promotion.

        Returns:
            The created PendingPromotion, or None when already pending or maxed.
        """
        with self._lock:
            return self._auto_promote_unlocked(action_type)

    def _auto_promote_unlocked(self, action_type: str) -> PendingPromotion | None:
        if action_type in self._vetoed_actions:
            logger.info("Auto-promotion for %s blocked by veto", action_type)
            return None
        if action_type in self._pending_promotions:
            return None

        policy = self.get_policy(action_type)
        promoted = self._promote_one_level(policy.level)
        if promoted == policy.level:
            return None

        now = datetime.now(timezone.utc)
        deadline = now + timedelta(hours=_VETO_WINDOW_HOURS)
        pending = PendingPromotion(
            action_type=action_type,
            current_level=policy.level,
            new_level=promoted,
            proposed_at=now.isoformat(),
            veto_deadline=deadline.isoformat(),
        )
        self._pending_promotions[action_type] = pending
        logger.info(
            "Auto-promotion proposed for %s: %s -> %s (veto deadline %s)",
            action_type,
            policy.level.value,
            promoted.value,
            deadline.isoformat(),
        )
        return pending

    def get_pending_promotions(self) -> dict[str, PendingPromotion]:
        """Return a copy of all pending auto-promotions.

        Returns:
            Dict mapping action_type to PendingPromotion.
        """
        with self._lock:
            return dict(self._pending_promotions)

    def _auto_demote(self, action_type: str) -> None:
        """Immediately drop an action type one autonomy level."""
        policy = self.get_policy(action_type)
        current = policy.level
        demoted = self._demote_one_level(current)
        if demoted == current:
            return

        self._policies[action_type] = ActionPolicy(
            level=demoted,
            max_change_pct=policy.max_change_pct,
            rollback_on_regression=policy.rollback_on_regression,
        )
        self._trust_records[action_type] = dataclasses.replace(
            self._trust_records[action_type],
            was_demoted=True,
            consecutive_failures=0,
        )
        logger.warning(
            "Auto-demoted action %s from %s to %s after %d consecutive failures",
            action_type,
            current.value,
            demoted.value,
            _DEMOTION_CONSECUTIVE_FAILURES,
        )

    @staticmethod
    def _demote_one_level(level: AutonomyLevel) -> AutonomyLevel:
        """Return the autonomy level one step below the given level."""
        return _demote_one_level(level)

    @staticmethod
    def _promote_one_level(level: AutonomyLevel) -> AutonomyLevel:
        """Return the autonomy level one step above the given level."""
        return _promote_one_level(level)

    def suggest_promotions(self) -> list[PromotionSuggestion]:
        """Return promotion suggestions for action types that meet criteria.

        Returns:
            List of promotion suggestions for eligible action types.
        """
        suggestions: list[PromotionSuggestion] = []
        with self._lock:
            for action_type, record in self._trust_records.items():
                if not record.eligible_for_promotion:
                    continue
                if action_type in self._vetoed_actions:
                    continue
                policy = self.get_policy(action_type)
                promoted = self._promote_one_level(policy.level)
                if promoted == policy.level:
                    continue
                suggestions.append(
                    PromotionSuggestion(
                        action_type=action_type,
                        current_level=policy.level,
                        suggested_level=promoted,
                        success_rate=record.success_rate,
                        total_actions=record.total_actions,
                    )
                )
        return suggestions

    def apply_promotion(self, action_type: str) -> bool:
        """Apply a human-confirmed promotion for an action type.

        Args:
            action_type: The action type to promote.

        Returns:
            True if promotion was applied, False if not eligible, vetoed, or maxed.
        """
        with self._lock:
            if action_type in self._vetoed_actions:
                logger.info("Promotion for %s blocked by veto", action_type)
                return False
            record = self._trust_records.get(action_type)
            if record is None or not record.eligible_for_promotion:
                return False
            policy = self.get_policy(action_type)
            promoted = self._promote_one_level(policy.level)
            if promoted == policy.level:
                return False
            self._policies[action_type] = ActionPolicy(
                level=promoted,
                max_change_pct=policy.max_change_pct,
                rollback_on_regression=policy.rollback_on_regression,
            )
            self._trust_records[action_type] = dataclasses.replace(
                record,
                was_demoted=False,
                total_actions=0,
                successful_actions=0,
                consecutive_failures=0,
            )
            logger.info(
                "Promoted action %s from %s to %s (human-confirmed)",
                action_type,
                policy.level.value,
                promoted.value,
            )
            return True

    def get_trust_status(self) -> dict[str, dict[str, Any]]:
        """Return trust tracking data for all action types.

        Returns:
            Dict mapping action_type to trust metrics.
        """
        with self._lock:
            return {
                action_type: {
                    "total_actions": record.total_actions,
                    "successful_actions": record.successful_actions,
                    "success_rate": round(record.success_rate, 3),
                    "consecutive_failures": record.consecutive_failures,
                    "eligible_for_promotion": record.eligible_for_promotion,
                    "current_level": self.get_policy(action_type).level.value,
                }
                for action_type, record in self._trust_records.items()
            }

    def check_pending_promotions(self) -> list[str]:
        """Check pending promotions and apply those whose veto window has expired.

        Returns:
            List of action types where promotions were applied.
        """
        now = datetime.now(timezone.utc)
        applied: list[str] = []

        with self._lock:
            expired = [
                action_type
                for action_type, pending in self._pending_promotions.items()
                if datetime.fromisoformat(pending.veto_deadline) <= now
            ]

            for action_type in expired:
                pending = self._pending_promotions.pop(action_type)
                old_policy = self.get_policy(action_type)
                self._policies[action_type] = ActionPolicy(
                    level=pending.new_level,
                    max_change_pct=old_policy.max_change_pct,
                    rollback_on_regression=old_policy.rollback_on_regression,
                )
                applied.append(action_type)
                logger.info(
                    "Auto-promotion applied for %s: %s -> %s (veto window expired)",
                    action_type,
                    pending.current_level.value,
                    pending.new_level.value,
                )

        return applied

    def veto_promotion(self, action_type: str) -> bool:
        """Veto promotion for an action type.

        Args:
            action_type: The action type to veto.

        Returns:
            True, because applying a veto always succeeds.
        """
        with self._lock:
            self._pending_promotions.pop(action_type, None)
            self._vetoed_actions.add(action_type)

        logger.info("Vetoed promotion for action type %s", action_type)
        return True

    def clear_veto(self, action_type: str) -> bool:
        """Remove a promotion veto for an action type.

        Args:
            action_type: The action type to un-veto.

        Returns:
            True if a veto was cleared, False if no veto existed.
        """
        with self._lock:
            if action_type in self._vetoed_actions:
                self._vetoed_actions.discard(action_type)
                return True
            return False

    def get_vetoed_actions(self) -> frozenset[str]:
        """Return the set of action types currently vetoed from promotion.

        Returns:
            Frozen set of action type strings with active vetoes.
        """
        with self._lock:
            return frozenset(self._vetoed_actions)
