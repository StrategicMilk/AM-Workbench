"""Tiered remediation engine for pipeline failure recovery.

Diagnoses failures and executes escalating remediation plans to restore
system health with minimal disruption.

This is step 5b of the pipeline:
Intake -> Planning -> Execution -> Quality Gate -> **Remediation** -> Assembly.

When a failure is detected (OOM, hang, quality degradation, disk full,
thermal throttling), the engine diagnoses the failure mode, builds a
plan of escalating actions, and executes them through a circuit breaker
to prevent remediation loops.
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from vetinari.exceptions import RemediationBypassError
from vetinari.resilience.circuit_breaker import CircuitBreaker, CircuitBreakerConfig
from vetinari.system.remediation_actions import (
    _alert_operator as _alert_operator,
)
from vetinari.system.remediation_actions import (
    _cancel_and_retry as _cancel_and_retry,
)
from vetinari.system.remediation_actions import (
    _clear_caches as _clear_caches,
)
from vetinari.system.remediation_actions import (
    _pause_and_cooldown as _pause_and_cooldown,
)
from vetinari.system.remediation_actions import (
    _pause_pipeline as _pause_pipeline,
)
from vetinari.system.remediation_actions import (
    _pause_training as _pause_training,
)
from vetinari.system.remediation_actions import (
    _reduce_batch_size as _reduce_batch_size,
)
from vetinari.system.remediation_actions import (
    _reduce_context_size as _reduce_context_size,
)
from vetinari.system.remediation_actions import (
    _retry_with_refinement as _retry_with_refinement,
)
from vetinari.system.remediation_actions import (
    _switch_model as _switch_model,
)
from vetinari.system.remediation_actions import (
    _switch_to_smaller_model as _switch_to_smaller_model,
)

logger = logging.getLogger(__name__)


# ── Enums ─────────────────────────────────────────────────────────────


class FailureMode(Enum):
    """Known failure modes that the remediation engine can diagnose and handle."""

    OOM = "oom"  # Out of memory — model too large or context too long
    HANG = "hang"  # Agent execution stalled — no progress for timeout period
    QUALITY_DROP = "quality_drop"  # Output quality below acceptable threshold
    DISK_FULL = "disk_full"  # Insufficient disk space for model or training artifacts
    THERMAL = "thermal"  # GPU thermal throttling reducing performance


class RemediationTier(Enum):
    """Escalation tiers from least to most disruptive."""

    AUTO_FIX = "auto_fix"  # Attempt automatic resolution (clear cache, reduce batch)
    ALERT = "alert"  # Notify operator, continue with degraded operation
    PAUSE = "pause"  # Pause affected pipeline, wait for intervention
    SHUTDOWN = "shutdown"  # Graceful shutdown of affected subsystem


# Tier ordering for comparison — lower index = less disruptive.
_TIER_ORDER: list[RemediationTier] = [
    RemediationTier.AUTO_FIX,
    RemediationTier.ALERT,
    RemediationTier.PAUSE,
    RemediationTier.SHUTDOWN,
]


# ── Dataclasses ───────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class RemediationAction:
    """A single remediation step within a plan.

    Attributes:
        description: Human-readable description of what the action does.
        tier: The escalation tier this action belongs to.
        action_fn: Optional callable that performs the action; returns True
            on success, False on failure. If None the action is treated as
            an informational record only.
    """

    description: str
    tier: RemediationTier
    action_fn: Callable[[], bool] | None = None  # Returns True on success


@dataclass
class RemediationPlan:
    """Diagnosis and remediation plan for a detected failure.

    Attributes:
        failure_mode: The classified failure that triggered this plan.
        diagnosis: Plain-English description of what was diagnosed.
        actions: Ordered list of remediation actions, least to most disruptive.
        max_tier: Highest escalation tier present in the action list.
    """

    failure_mode: FailureMode
    diagnosis: str
    actions: list[RemediationAction]
    max_tier: RemediationTier

    def __repr__(self) -> str:
        """Show identifying fields without dumping the full action list."""
        return (
            f"RemediationPlan(failure_mode={self.failure_mode!r},"
            f" max_tier={self.max_tier!r},"
            f" actions={len(self.actions)})"
        )


@dataclass
class RemediationResult:
    """Outcome of executing a remediation plan.

    Attributes:
        success: True if at least one action resolved the failure.
        failure_mode: The failure mode that was addressed.
        tier_reached: The highest tier executed during remediation.
        actions_taken: Descriptions of every action that was attempted.
        error: Error message if remediation itself raised an exception.
    """

    success: bool
    failure_mode: FailureMode
    tier_reached: RemediationTier
    actions_taken: list[str] = field(default_factory=list)
    error: str | None = None

    def __repr__(self) -> str:
        return "RemediationResult(...)"


# ── Action implementations ────────────────────────────────────────────


class RemediationEngine:
    """Diagnoses failures and executes tiered remediation plans.

    Uses a circuit breaker to prevent remediation loops — if remediation
    itself fails repeatedly, the engine stops trying and escalates to
    the highest tier.

    Side effects:
        - Creates a CircuitBreaker("remediation") on init.
        - Maintains an in-memory deque of up to 100 RemediationResult records.
    """

    def __init__(self) -> None:
        self._breaker = CircuitBreaker(
            "remediation",
            CircuitBreakerConfig(failure_threshold=3, recovery_timeout=60.0),
        )
        # Bounded history — oldest entries are discarded automatically.
        self._remediation_history: deque[RemediationResult] = deque(maxlen=100)

    def diagnose(
        self,
        failure_mode: FailureMode,
        context: dict[str, Any] | None = None,
    ) -> RemediationPlan:
        """Create a remediation plan for the given failure mode.

        Args:
            failure_mode: Failure mode value consumed by diagnose().
            context: Context value consumed by diagnose().

        Returns:
            Value produced for the caller.
        """
        ctx_info = f" (context={context})" if context else ""
        logger.info("Diagnosing failure mode '%s'%s", failure_mode.value, ctx_info)
        actions: list[RemediationAction]
        if failure_mode == FailureMode.OOM:
            actions = [
                RemediationAction("Reduce inference context size", RemediationTier.AUTO_FIX, _reduce_context_size),
                RemediationAction("Switch to smaller model", RemediationTier.AUTO_FIX, _switch_to_smaller_model),
                RemediationAction(
                    "Alert operator: OOM unresolved", RemediationTier.ALERT, _alert_operator(failure_mode)
                ),
                RemediationAction("Pause pipeline: OOM critical", RemediationTier.PAUSE, _pause_pipeline(failure_mode)),
            ]
            diagnosis = "Inference ran out of memory — context too large or model exceeds available VRAM"
        elif failure_mode == FailureMode.HANG:
            actions = [
                RemediationAction("Cancel stalled task and retry", RemediationTier.AUTO_FIX, _cancel_and_retry),
                RemediationAction(
                    "Alert operator: hang unresolved", RemediationTier.ALERT, _alert_operator(failure_mode)
                ),
                RemediationAction(
                    "Pause pipeline: persistent hang", RemediationTier.PAUSE, _pause_pipeline(failure_mode)
                ),
            ]
            diagnosis = "Agent execution stalled — no progress detected within the timeout window"
        elif failure_mode == FailureMode.QUALITY_DROP:
            actions = [
                RemediationAction("Retry with refined prompt", RemediationTier.AUTO_FIX, _retry_with_refinement),
                RemediationAction("Switch to higher-quality model", RemediationTier.AUTO_FIX, _switch_model),
                RemediationAction(
                    "Alert operator: quality below threshold", RemediationTier.ALERT, _alert_operator(failure_mode)
                ),
            ]
            diagnosis = "Output quality score fell below the acceptable threshold for this task type"
        elif failure_mode == FailureMode.DISK_FULL:
            actions = [
                RemediationAction("Clear non-essential caches", RemediationTier.AUTO_FIX, _clear_caches),
                RemediationAction(
                    "Alert operator: disk space low", RemediationTier.ALERT, _alert_operator(failure_mode)
                ),
                RemediationAction("Pause training to stop artifact writes", RemediationTier.PAUSE, _pause_training),
            ]
            diagnosis = "Insufficient disk space — model or training artifacts cannot be written"
        else:  # FailureMode.THERMAL
            actions = [
                RemediationAction("Reduce inference batch size", RemediationTier.AUTO_FIX, _reduce_batch_size),
                RemediationAction(
                    "Alert operator: thermal throttling active", RemediationTier.ALERT, _alert_operator(failure_mode)
                ),
                RemediationAction("Pause inference for GPU cooldown", RemediationTier.PAUSE, _pause_and_cooldown),
            ]
            diagnosis = "GPU thermal throttling detected — sustained high temperature is degrading performance"
        max_tier = max(actions, key=lambda a: _TIER_ORDER.index(a.tier)).tier
        return RemediationPlan(
            failure_mode=failure_mode,
            diagnosis=diagnosis,
            actions=actions,
            max_tier=max_tier,
        )

    def execute_remediation(self, plan: RemediationPlan) -> RemediationResult:
        """Execute all actions in the remediation plan, then aggregate results.

        Returns:
            Value produced for the caller.
        """
        if not self._breaker.allow_request():
            logger.error(
                "Remediation circuit breaker is OPEN for failure mode '%s' - too many consecutive remediation failures; escalating to max tier",
                plan.failure_mode.value,
            )
            return self._record_remediation_result(
                plan,
                RemediationResult(
                    success=False,
                    failure_mode=plan.failure_mode,
                    tier_reached=plan.max_tier,
                    error="Remediation circuit breaker open - repeated remediation failures",
                ),
            )
        actions_taken: list[str] = []
        tier_reached = plan.actions[0].tier if plan.actions else RemediationTier.AUTO_FIX
        for action in plan.actions:
            tier_reached = action.tier
            actions_taken.append(action.description)
            logger.info("Executing remediation action [%s]: %s", action.tier.value, action.description)
            if action.action_fn is None:
                logger.debug("Action '%s' has no callable - recorded as informational", action.description)
                continue
            if self._run_remediation_action(action):
                logger.info(
                    "Remediation action '%s' succeeded at tier '%s' - stopping escalation",
                    action.description,
                    action.tier.value,
                )
                self._breaker.record_success()
                return self._record_remediation_result(
                    plan,
                    RemediationResult(
                        success=True,
                        failure_mode=plan.failure_mode,
                        tier_reached=tier_reached,
                        actions_taken=actions_taken,
                    ),
                )
            logger.warning(
                "Remediation action '%s' failed at tier '%s' - escalating to next tier",
                action.description,
                action.tier.value,
            )
        logger.error(
            "All remediation actions exhausted for failure mode '%s' - highest tier reached: %s",
            plan.failure_mode.value,
            tier_reached.value,
        )
        self._breaker.record_failure()
        return self._record_remediation_result(
            plan,
            RemediationResult(
                success=False,
                failure_mode=plan.failure_mode,
                tier_reached=tier_reached,
                actions_taken=actions_taken,
                error=f"all {len(actions_taken)} remediation action(s) exhausted without success",
            ),
        )

    @staticmethod
    def _run_remediation_action(action: RemediationAction) -> bool:
        try:
            return bool(action.action_fn())
        except Exception as exc:
            logger.warning(
                "Remediation action '%s' raised an exception - treating as failure, escalating: %s",
                action.description,
                exc,
            )
            return False

    def _record_remediation_result(self, plan: RemediationPlan, result: RemediationResult) -> RemediationResult:
        self._remediation_history.append(result)
        self._log_outcome_to_registry(plan, result)
        return result

    @staticmethod
    def _log_outcome_to_registry(
        plan: RemediationPlan,
        result: RemediationResult,
    ) -> None:
        """Log remediation outcome to the failure registry for trend tracking.

        Records each action taken along with its success/failure status so
        that per-(failure_mode, action) statistics can be computed.

        Args:
            plan: The remediation plan that was executed.
            result: The outcome of executing the plan.
        """
        try:
            from vetinari.analytics.failure_registry import get_failure_registry

            registry = get_failure_registry()
            for action_desc in result.actions_taken:
                registry.log_remediation_outcome(
                    failure_mode=plan.failure_mode.value,
                    action_description=action_desc,
                    success=result.success,
                )
        except Exception:
            logger.warning("Could not log remediation outcome to failure registry — stats may be incomplete")

    def get_history(self) -> list[RemediationResult]:
        """Return all recorded remediation outcomes, oldest first.

        Returns:
            Snapshot list of up to 100 RemediationResult records.
        """
        return list(self._remediation_history)

    def get_stats(self) -> dict[str, Any]:
        """Return aggregate statistics about remediation activity.

        Returns:
            Dictionary with keys: ``total``, ``success_rate``,
            ``most_common_failure_mode``, and ``breaker_state``.
        """
        history = list(self._remediation_history)
        total = len(history)

        if total == 0:
            return {
                "total": 0,
                "success_rate": 0.0,
                "most_common_failure_mode": None,
                "breaker_state": self._breaker.state.value,
            }

        successes = sum(1 for r in history if r.success)
        mode_counts: dict[str, int] = {}
        for r in history:
            mode_counts[r.failure_mode.value] = mode_counts.get(r.failure_mode.value, 0) + 1
        most_common = max(mode_counts, key=lambda k: mode_counts[k])

        return {
            "total": total,
            "success_rate": round(successes / total, 3),
            "most_common_failure_mode": most_common,
            "breaker_state": self._breaker.state.value,
        }

    def remediate(
        self,
        action: str,
        log_only: bool = False,
        override_authorized: bool = False,
    ) -> None:
        """Execute or log a remediation action with bypass guard.

        When ``log_only`` is ``True`` the caller intends to record the action
        without actually executing it.  This is only permitted when
        ``override_authorized`` is also ``True``; otherwise the call raises
        ``RemediationBypassError`` to prevent silent no-ops that look like
        successful remediations (LLM06 / excessive-agency mitigation).

        Args:
            action: Human-readable description of the remediation action to
                perform, used for logging and audit trails.
            log_only: When ``True``, skip execution and only log the intent.
                Requires ``override_authorized=True``.
            override_authorized: Explicit opt-in to log-only bypass.  Set to
                ``True`` only when the caller has been granted explicit
                authority to skip execution (e.g. dry-run mode).

        Raises:
            RemediationBypassError: When ``log_only=True`` and
                ``override_authorized=False``, to prevent unapproved bypass.
        """
        if log_only and not override_authorized:
            raise RemediationBypassError(
                f"remediation action {action!r} requested log_only=True without "
                "override_authorized=True; set override_authorized=True to permit "
                "log-only bypass of remediation execution"
            )
        if log_only:
            logger.info("Remediation action logged (log-only, override authorized): %s", action)
        else:
            logger.info("Executing remediation action: %s", action)


# ── Singleton ─────────────────────────────────────────────────────────

# Module-level singleton. Written once by get_remediation_engine() under
# _engine_lock; read by every subsequent caller without holding the lock.
_engine: RemediationEngine | None = None
_engine_lock = threading.Lock()


def get_remediation_engine() -> RemediationEngine:
    """Return the process-wide RemediationEngine singleton.

    Uses double-checked locking so that the common read-path (engine
    already created) never acquires the lock.

    Returns:
        The singleton RemediationEngine instance.
    """
    global _engine
    if _engine is None:
        with _engine_lock:
            if _engine is None:
                _engine = RemediationEngine()
    return _engine
