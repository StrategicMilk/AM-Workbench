"""Config self-tuning — automatic profile alignment with Thompson learned values.

After a configurable number of tasks (default 100) of the same type, compares
the current ``InferenceProfile`` parameters with what Thompson Sampling has
learned as optimal.  When any parameter diverges by >20%, the profile is
auto-updated and a decision journal entry is logged.

Pipeline role: runs as a post-task hook, checking whether the static config
has drifted from the learned optimum and correcting it automatically.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


# Minimum tasks before tuning triggers
_DEFAULT_TUNING_THRESHOLD = 100

# Divergence threshold — tune when any param differs by more than this fraction
_DIVERGENCE_THRESHOLD = 0.20


@dataclass(frozen=True, slots=True)
class TuningResult:
    """Outcome of a self-tuning check.

    Attributes:
        task_type: The task type that was checked.
        tuned: Whether any parameter was updated.
        changes: Dict of param_name -> {old, new, divergence_pct}.
        task_count: Number of tasks observed for this type.
    """

    task_type: str
    tuned: bool
    changes: dict[str, dict[str, Any]]
    task_count: int

    def __repr__(self) -> str:
        return f"TuningResult(task_type={self.task_type!r}, tuned={self.tuned}, changes={len(self.changes)})"


class ConfigSelfTuner:
    """Compares inference profiles with Thompson learned values and auto-tunes.

    Tracks per-task-type completion counts and triggers tuning after the
    configured threshold.  When divergence is detected, updates the profile
    and logs a decision journal entry.

    Side effects:
        - Updates InferenceConfigManager profiles when divergent.
        - Writes decision journal entries on every tuning action.
    """

    def __init__(self, tuning_threshold: int = _DEFAULT_TUNING_THRESHOLD) -> None:
        self._threshold = tuning_threshold
        self._lock = threading.Lock()
        # task_type -> count of completed tasks since last tune
        self._task_counts: dict[str, int] = {}

    def record_task_completion(self, task_type: str) -> TuningResult | None:
        """Record a completed task and trigger tuning if threshold is reached.

        Increments the task counter for the given type.  When the counter
        reaches the threshold, calls ``check_and_tune`` and resets the counter.

        Args:
            task_type: The type of task that completed (e.g. ``"coding"``).

        Returns:
            TuningResult if tuning was triggered, None otherwise.
        """
        with self._lock:
            self._task_counts[task_type] = self._task_counts.get(task_type, 0) + 1
            count = self._task_counts[task_type]

            if count < self._threshold:
                return None

            # Reset counter before tuning (avoid re-triggering on exception)
            self._task_counts[task_type] = 0

        return self.check_and_tune(task_type, task_count=count)

    def check_and_tune(
        self,
        task_type: str,
        task_count: int = 0,
    ) -> TuningResult:
        """Compare profile params with Thompson learned values and tune if divergent.

        Reads the current InferenceProfile for ``task_type``, queries Thompson
        strategy arms for learned optimal values, and updates the profile when
        any parameter diverges by more than ``_DIVERGENCE_THRESHOLD``.

        Args:
            task_type: The task type to check and potentially tune.
            task_count: Number of tasks that triggered this check (for logging).

        Returns:
            TuningResult describing what changed (if anything).
        """
        changes: dict[str, dict[str, Any]] = {}

        # Get current profile
        try:
            from vetinari.config.inference_config import get_inference_config

            config_mgr = get_inference_config()
            profile = config_mgr.get_profile(task_type)
        except Exception:
            logger.warning(
                "Cannot load inference profile for %s — skipping self-tuning",
                task_type,
            )
            return TuningResult(task_type=task_type, tuned=False, changes={}, task_count=task_count)

        # Get Thompson learned values for strategy params
        thompson_values = self._get_thompson_learned_values(task_type)
        if not thompson_values:
            return TuningResult(task_type=task_type, tuned=False, changes={}, task_count=task_count)

        changes = self._collect_profile_changes(profile, thompson_values)

        if not changes:
            logger.info(
                "Self-tuning: %s profile is aligned with Thompson values (no changes needed)",
                task_type,
            )
            return TuningResult(task_type=task_type, tuned=False, changes={}, task_count=task_count)

        # Apply changes to the profile
        self._apply_changes(task_type, changes, config_mgr)

        # Log to decision journal
        self._log_tuning_decision(task_type, changes, task_count)

        logger.info(
            "Self-tuning: updated %d parameter(s) for %s profile",
            len(changes),
            task_type,
        )
        return TuningResult(
            task_type=task_type,
            tuned=True,
            changes=changes,
            task_count=task_count,
        )

    def _collect_profile_changes(self, profile: Any, thompson_values: dict[str, Any]) -> dict[str, dict[str, Any]]:
        changes: dict[str, dict[str, Any]] = {}
        if "temperature" in thompson_values:
            learned_temp = float(thompson_values["temperature"])
            divergence = self._compute_divergence(profile.temperature, learned_temp)
            if divergence > _DIVERGENCE_THRESHOLD:
                changes["temperature"] = {
                    "old": profile.temperature,
                    "new": learned_temp,
                    "divergence_pct": round(divergence * 100, 1),
                }
        if "context_window_size" in thompson_values:
            logger.info(
                "Self-tuning: learned context_window_size is advisory; output max_tokens is not changed",
            )
        return changes

    @staticmethod
    def _get_thompson_learned_values(task_type: str) -> dict[str, Any]:
        """Query Thompson strategy selectors for learned optimal values.

        Checks each strategy key (temperature, context_window_size) and returns
        the best arm's value based on the highest Beta distribution mean.

        Args:
            task_type: The task type to query arms for.

        Returns:
            Dict mapping param name to learned optimal value.
        """
        try:
            from vetinari.learning.model_selector import get_thompson_selector

            selector = get_thompson_selector()
        except Exception:
            logger.warning(
                "Thompson selector unavailable — cannot get learned values for %s",
                task_type,
            )
            return {}

        learned: dict[str, Any] = {}

        try:
            from vetinari.learning.thompson_selectors import STRATEGY_VALUE_SPACES

            arms = selector._arms  # Access arms dict for inspection

            for strategy_key, values in STRATEGY_VALUE_SPACES.items():
                best_value = None
                best_mean = -1.0

                for value in values:
                    # Strategy identity is stored on the arm itself. Do not parse
                    # persistence keys; model IDs may legitimately contain colons.
                    strategy_fragment = f":{strategy_key}:{value}"
                    task_fragment = f":{task_type}:"
                    for arm in arms.values():
                        arm_model_id = getattr(arm, "model_id", "")
                        if strategy_fragment not in arm_model_id:
                            continue
                        if task_type and task_fragment not in arm_model_id:
                            continue
                        mean = arm.alpha / (arm.alpha + arm.beta) if (arm.alpha + arm.beta) > 0 else 0.5
                        if mean > best_mean and arm.total_pulls >= 10:
                            best_mean = mean
                            best_value = value

                if best_value is not None:
                    learned[strategy_key] = best_value

        except Exception:
            logger.warning(
                "Could not read Thompson arms for %s — self-tuning skipped",
                task_type,
            )

        return learned

    @staticmethod
    def _compute_divergence(current: float | int, learned: float | int) -> float:
        """Compute fractional divergence between current and learned values.

        Uses the larger value as the denominator to avoid division by zero
        and ensure symmetric comparison.

        Args:
            current: Current profile value.
            learned: Thompson-learned optimal value.

        Returns:
            Fractional divergence (0.0 = identical, 1.0 = 100% different).
        """
        current_f = float(current)
        learned_f = float(learned)
        denominator = max(abs(current_f), abs(learned_f), 1e-9)
        return abs(current_f - learned_f) / denominator

    @staticmethod
    def _apply_changes(
        task_type: str,
        changes: dict[str, dict[str, Any]],
        config_mgr: Any,
    ) -> None:
        """Apply parameter changes to the inference profile.

        Modifies the raw profile dict in the config manager so the changes
        take effect on the next ``get_profile()`` / ``get_effective_params()``
        call.

        Args:
            task_type: The task type whose profile is being updated.
            changes: Dict of param_name -> {old, new, divergence_pct}.
            config_mgr: The InferenceConfigManager instance.
        """
        updates = {param_name: change["new"] for param_name, change in changes.items()}
        if hasattr(config_mgr, "update_profile_parameters"):
            config_mgr.update_profile_parameters(task_type, updates)
            return
        raise TypeError("config manager does not expose update_profile_parameters")

    @staticmethod
    def _log_tuning_decision(
        task_type: str,
        changes: dict[str, dict[str, Any]],
        task_count: int,
    ) -> None:
        """Log a tuning decision to the decision journal.

        Args:
            task_type: The task type that was tuned.
            changes: Dict of parameter changes applied.
            task_count: Number of tasks that triggered the tuning.
        """
        try:
            from vetinari.observability.decision_journal import get_decision_journal
            from vetinari.types import ConfidenceLevel, DecisionType

            journal = get_decision_journal()
            change_summary = ", ".join(
                f"{name}: {c['old']}->{c['new']} ({c['divergence_pct']}% divergent)" for name, c in changes.items()
            )
            journal.log_decision(
                decision_type=DecisionType.PARAMETER_TUNING,
                chosen=f"self-tune {task_type}",
                confidence=ConfidenceLevel.HIGH,
                reasoning=(
                    f"Applied process-local self-tuning for {task_type} after {task_count} tasks: {change_summary}. "
                    f"Thompson learned values diverged >20% from the loaded inference profile."
                ),
                metadata={
                    "task_type": task_type,
                    "task_count": task_count,
                    "changes": changes,
                },
            )
        except Exception:
            logger.warning("Could not log self-tuning decision to journal — tuning still applied")

    def get_task_counts(self) -> dict[str, int]:
        """Return current task counts per type (for diagnostics).

        Returns:
            Dict mapping task_type to current count toward next tuning.
        """
        with self._lock:
            return dict(self._task_counts)


# ── Singleton ────────────────────────────────────────────────────────────────

_tuner: ConfigSelfTuner | None = None
_tuner_lock = threading.Lock()


def get_config_self_tuner() -> ConfigSelfTuner:
    """Return the process-wide ConfigSelfTuner singleton.

    Uses double-checked locking so the common read-path never acquires the lock.

    Returns:
        The singleton ConfigSelfTuner instance.
    """
    global _tuner
    if _tuner is None:
        with _tuner_lock:
            if _tuner is None:
                _tuner = ConfigSelfTuner()
    return _tuner


def reset_config_self_tuner() -> None:
    """Reset the singleton for test isolation."""
    global _tuner
    with _tuner_lock:
        _tuner = None
