"""Auto-Tuner - Vetinari Self-Improvement Subsystem.

Monitors SLA compliance and anomaly patterns, then automatically adjusts
system configuration to maintain performance targets.
"""

from __future__ import annotations

import json
import logging
import threading
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from vetinari.constants import (
    AUTO_TUNER_ANOMALY_THRESHOLD,
    AUTO_TUNER_DEFAULT_CONCURRENT,
    AUTO_TUNER_MAX_CONCURRENT,
    AUTO_TUNER_MIN_CONCURRENT,
    AUTO_TUNER_MIN_QUALITY_THRESHOLD,
    AUTO_TUNER_RETRY_BACKOFF_CAP,
    get_user_dir,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class TuningAction:
    """A recorded auto-tuning adjustment."""

    timestamp: str
    trigger: str  # What caused the tuning
    parameter: str  # What was changed
    old_value: Any
    new_value: Any
    rationale: str
    auto_applied: bool

    def __repr__(self) -> str:
        return (
            f"TuningAction(parameter={self.parameter!r}, trigger={self.trigger!r}, auto_applied={self.auto_applied!r})"
        )


class AutoTuner:
    """SLA-driven automatic system parameter adjustment.

    Monitors:
    - SLA compliance from the SLATracker
    - Anomaly detection from the AnomalyDetector
    - Cost trends from the CostTracker
    - Forecast thresholds from the Forecaster

    Adjusts (automatically or with approval):
    - Concurrency limits
    - Model routing weights
    - Anomaly detection thresholds
    - Retry parameters
    """

    MAX_AUTO_CONCURRENT = AUTO_TUNER_MAX_CONCURRENT
    MIN_AUTO_CONCURRENT = AUTO_TUNER_MIN_CONCURRENT
    _DEFAULTS: dict[str, Any] = {
        "max_concurrent": AUTO_TUNER_DEFAULT_CONCURRENT,
        "anomaly_threshold": AUTO_TUNER_ANOMALY_THRESHOLD,
        "retry_backoff_cap": AUTO_TUNER_RETRY_BACKOFF_CAP,
        "min_quality_threshold": AUTO_TUNER_MIN_QUALITY_THRESHOLD,
    }

    # -- Inference parameter tuning bounds and step sizes ----------------------
    _INFERENCE_PARAM_BOUNDS: dict[str, dict[str, float]] = {
        "temperature": {"min": 0.0, "max": 1.5, "step": 0.05},
        "min_p": {"min": 0.0, "max": 0.3, "step": 0.01},
        "repeat_penalty": {"min": 1.0, "max": 1.5, "step": 0.05},
        "max_tokens": {"min": 512, "max": 8192, "step": 256},
        "top_p": {"min": 0.5, "max": 1.0, "step": 0.05},
        "mirostat_tau": {"min": 2.0, "max": 8.0, "step": 0.5},
        "frequency_penalty": {"min": 0.0, "max": 1.0, "step": 0.1},
    }

    def __init__(self):
        # Resolve config path at construction time via get_user_dir() so the
        # instance always points to the correct user state directory.
        self._config_path: Path = get_user_dir() / "auto_tuner_config.json"
        self._actions: deque[TuningAction] = deque(maxlen=1000)
        self._lock = threading.Lock()
        self._current_config: dict[str, Any] = self._load_config()

    def _load_config(self) -> dict[str, Any]:
        """Load persisted config or return defaults."""
        config = dict(self._DEFAULTS)
        try:
            if self._config_path.exists():
                with self._config_path.open(encoding="utf-8") as f:
                    saved = json.load(f)
                config.update(saved)
                logger.info("[AutoTuner] Loaded persisted config from %s", self._config_path)
        except Exception as e:
            logger.warning("[AutoTuner] Failed to load config, using defaults: %s", e)
        return config

    def _persist_config(self) -> None:
        """Save current config to disk."""
        try:
            self._config_path.parent.mkdir(parents=True, exist_ok=True)
            with self._config_path.open("w", encoding="utf-8") as f:
                json.dump(self._current_config, f, indent=2)
        except Exception as e:
            logger.warning("[AutoTuner] Failed to persist config: %s", e)

    def run_cycle(self) -> list[TuningAction]:
        """Run one auto-tuning cycle.

        Checks all monitored metrics and applies necessary adjustments.

        Returns:
            List of TuningActions applied in this cycle.
        """
        # Keep the check/mutate/persist sequence atomic, matching the
        # lock-around-mutate pattern used by vetinari.events.
        with self._lock:
            applied: list[TuningAction] = []

            # Check SLA compliance
            applied.extend(self._tune_from_sla())

            # Check anomaly patterns
            applied.extend(self._tune_from_anomalies())

            # Check cost trends
            applied.extend(self._tune_from_costs())

            # Tune inference parameters from quality scores and Thompson data
            applied.extend(self._tune_inference_params())

            if applied:
                logger.info("[AutoTuner] Applied %s tuning actions", len(applied))
                self._persist_config()
            return applied

    def _tune_from_sla(self) -> list[TuningAction]:
        """Adjust concurrency based on SLA compliance."""
        actions: list[TuningAction] = []
        try:
            from vetinari.analytics.sla import get_sla_tracker

            tracker = get_sla_tracker()
            raw_reports = tracker.get_all_reports() if hasattr(tracker, "get_all_reports") else []
            # get_all_reports() returns List[SLAReport]; convert to dict for iteration
            if isinstance(raw_reports, list):
                reports = {r.slo.name if hasattr(r, "slo") else str(i): r for i, r in enumerate(raw_reports)}
            else:
                reports = raw_reports  # dict path

            for slo_name, report in reports.items():
                report_dict = report.to_dict() if hasattr(report, "to_dict") else report
                if not report_dict.get("is_compliant", True):
                    # SLA breach -- reduce concurrency to slow down
                    current = self._current_config["max_concurrent"]
                    if current > self.MIN_AUTO_CONCURRENT:
                        new_val = max(self.MIN_AUTO_CONCURRENT, current - 1)
                        action = self._apply(
                            "SLA breach: " + slo_name,
                            "max_concurrent",
                            current,
                            new_val,
                            f"SLA '{slo_name}' breached -- reducing concurrency",
                            auto=True,
                        )
                        actions.append(action)
                elif report_dict.get("compliance_pct", 100) > 99:
                    # Excellent compliance -- try increasing concurrency
                    current = self._current_config["max_concurrent"]
                    if current < self.MAX_AUTO_CONCURRENT:
                        new_val = min(self.MAX_AUTO_CONCURRENT, current + 1)
                        action = self._apply(
                            "SLA excellent: " + slo_name,
                            "max_concurrent",
                            current,
                            new_val,
                            f"SLA '{slo_name}' excellent -- increasing concurrency",
                            auto=True,
                        )
                        actions.append(action)
        except Exception as e:
            logger.warning("SLA tuning failed: %s", e)
        return actions

    def _tune_from_anomalies(self, *, now: datetime | None = None) -> list[TuningAction]:
        """Adjust anomaly thresholds based on false-positive rate."""
        actions: list[TuningAction] = []
        try:
            from vetinari.analytics.anomaly import get_anomaly_detector

            detector = get_anomaly_detector()
            history = getattr(detector, "_anomaly_history", [])
            now_ts = (now or datetime.now(timezone.utc)).timestamp()

            # If too many recent anomalies, threshold is too sensitive
            recent = [
                a
                for a in history
                if isinstance(a, dict)
                and (now_ts - datetime.fromisoformat(a.get("detected_at", "2000-01-01")).timestamp()) < 3600
            ]

            current_thresh = self._current_config["anomaly_threshold"]
            if len(recent) > 10 and current_thresh < 5.0:
                new_thresh = round(current_thresh + 0.5, 1)
                action = self._apply(
                    "High anomaly rate",
                    "anomaly_threshold",
                    current_thresh,
                    new_thresh,
                    f"High false-positive rate ({len(recent)} in 1h) -- relaxing threshold",
                    auto=True,
                )
                actions.append(action)
            elif len(recent) == 0 and current_thresh > 2.0:
                new_thresh = round(current_thresh - 0.5, 1)
                action = self._apply(
                    "Low anomaly rate",
                    "anomaly_threshold",
                    current_thresh,
                    new_thresh,
                    "No recent anomalies -- tightening threshold",
                    auto=True,
                )
                actions.append(action)
        except Exception as e:
            logger.warning("Anomaly tuning failed: %s", e)
        return actions

    def _tune_from_costs(self) -> list[TuningAction]:
        """Warn if costs are trending upward."""
        actions: list[TuningAction] = []
        try:
            from vetinari.analytics.forecasting import get_forecaster

            forecaster = get_forecaster()
            will_exceed = forecaster.will_exceed("cost_per_hour", threshold=1.0, horizon=24)
            if will_exceed:
                action = self._apply(
                    "Cost forecast",
                    "min_quality_threshold",
                    self._current_config["min_quality_threshold"],
                    min(0.9, self._current_config["min_quality_threshold"] + 0.05),
                    "Cost forecast exceeds $1/hr -- raising quality threshold to use cheaper models",
                    auto=False,  # Requires human approval
                )
                actions.append(action)
        except Exception as e:
            logger.warning("Cost tuning failed: %s", e)
        return actions

    def _tune_inference_params(self) -> list[TuningAction]:
        """Tune inference sampling parameters from quality scores and Thompson data.

        Reads recent quality score distributions to detect when output quality is
        degraded (too-low scores indicate overly conservative sampling) or too
        variable (high variance suggests temperature is too high). Also reads
        Thompson bandit arm performance data to adjust parameters that correlate
        with reward.
        """
        return [*self._quality_score_tuning_actions(), *self._thompson_inference_actions()]

    def _quality_score_tuning_actions(self) -> list[TuningAction]:
        actions: list[TuningAction] = []
        try:
            from vetinari.learning.quality_scorer import get_quality_scorer

            scorer = get_quality_scorer()
            recent_scores = scorer.get_recent_scores(limit=50)
            if len(recent_scores) < 10:
                return actions  # Not enough data to tune

            avg_score = sum(recent_scores) / len(recent_scores)
            score_variance = sum((s - avg_score) ** 2 for s in recent_scores) / len(recent_scores)
            bounds = self._INFERENCE_PARAM_BOUNDS["temperature"]
            actions.extend(self._temperature_quality_actions(avg_score, score_variance, bounds))
            min_p_action = self._min_p_quality_action(avg_score)
            if min_p_action is not None:
                actions.append(min_p_action)
            max_tokens_action = self._max_tokens_quality_action(recent_scores, bounds)
            if max_tokens_action is not None:
                actions.append(max_tokens_action)

        except Exception as e:
            logger.warning("Inference param tuning failed: %s", e)
        return actions

    def _temperature_quality_actions(
        self,
        avg_score: float,
        score_variance: float,
        bounds: dict[str, float],
    ) -> list[TuningAction]:
        current_temp_offset = self._current_config.get("temperature_offset", 0.0)
        if score_variance > 0.15 and current_temp_offset > -0.2:
            return [
                self._apply(
                    "High quality variance",
                    "temperature_offset",
                    current_temp_offset,
                    round(current_temp_offset - bounds["step"], 3),
                    f"Quality score variance {score_variance:.3f} > 0.15 — lowering temperature offset",
                    auto=True,
                )
            ]
        if avg_score < 0.4 and current_temp_offset < 0.2:
            return [
                self._apply(
                    "Low quality average",
                    "temperature_offset",
                    current_temp_offset,
                    round(current_temp_offset + bounds["step"], 3),
                    f"Quality avg {avg_score:.3f} < 0.4 — raising temperature offset for diversity",
                    auto=True,
                )
            ]
        return []

    def _min_p_quality_action(self, avg_score: float) -> TuningAction | None:
        current_min_p = self._current_config.get("min_p_override", 0.05)
        if avg_score >= 0.35 or current_min_p >= 0.15:
            return None
        return self._apply(
            "Low quality — tighten min_p",
            "min_p_override",
            current_min_p,
            round(current_min_p + 0.01, 3),
            f"Quality avg {avg_score:.3f} — raising min_p to filter low-prob tokens",
            auto=True,
        )

    def _max_tokens_quality_action(
        self,
        recent_scores: Sequence[float],
        bounds: dict[str, float],
    ) -> TuningAction | None:
        truncated_count = sum(1 for score in recent_scores if score < 0.3)
        current_max_tokens_offset = self._current_config.get("max_tokens_offset", 0)
        if truncated_count <= len(recent_scores) * 0.3 or current_max_tokens_offset >= 2048:
            return None
        return self._apply(
            "Possible truncation",
            "max_tokens_offset",
            current_max_tokens_offset,
            current_max_tokens_offset + int(bounds["step"]),
            f"{truncated_count}/{len(recent_scores)} low scores — increasing max_tokens offset",
            auto=True,
        )

    def _thompson_inference_actions(self) -> list[TuningAction]:
        actions: list[TuningAction] = []
        try:
            from vetinari.learning.model_selector import get_model_selector

            selector = get_model_selector()
            if not hasattr(selector, "get_arm_stats"):
                return actions
            arm_stats = selector.get_arm_stats()
            if not arm_stats:
                return actions
            avg_reward = sum(s.get("mean_reward", 0.5) for s in arm_stats.values()) / max(len(arm_stats), 1)
            current_rp_offset = self._current_config.get("repeat_penalty_offset", 0.0)
            if avg_reward < 0.4 and current_rp_offset < 0.2:
                actions.append(self._thompson_repeat_penalty_action(avg_reward, current_rp_offset))
        except Exception as e:
            logger.warning("Thompson-based inference tuning failed: %s", e)
        return actions

    def _thompson_repeat_penalty_action(self, avg_reward: float, current_rp_offset: float) -> TuningAction:
        return self._apply(
            "Low Thompson reward",
            "repeat_penalty_offset",
            current_rp_offset,
            round(current_rp_offset + 0.05, 3),
            f"Thompson avg reward {avg_reward:.3f} — increasing repeat penalty",
            auto=True,
        )

    def _apply(
        self,
        trigger: str,
        parameter: str,
        old_val: Any,
        new_val: Any,
        rationale: str,
        auto: bool,
    ) -> TuningAction:
        """Apply a tuning action."""
        if auto:
            self._current_config[parameter] = new_val

        action = TuningAction(
            timestamp=datetime.now(timezone.utc).isoformat(),
            trigger=trigger,
            parameter=parameter,
            old_value=old_val,
            new_value=new_val,
            rationale=rationale,
            auto_applied=auto,
        )
        self._actions.append(action)

        if auto:
            logger.info("[AutoTuner] %s: %s → %s (%s)", parameter, old_val, new_val, rationale)
        else:
            logger.warning("[AutoTuner] MANUAL ACTION NEEDED: %s: %s → %s (%s)", parameter, old_val, new_val, rationale)

        # Log to memory as a DECISION entry
        try:
            from vetinari.memory import MemoryEntry, MemoryType, get_unified_memory_store

            store = get_unified_memory_store()
            import json

            store.remember(
                MemoryEntry(
                    agent="auto_tuner",
                    entry_type=MemoryType.DECISION,
                    content=json.dumps(
                        {"parameter": parameter, "old": old_val, "new": new_val, "rationale": rationale, "auto": auto},
                    ),
                    summary=f"AutoTuner: {parameter} {old_val}→{new_val}",
                    provenance="auto_tuner",
                ),
            )
        except Exception:
            logger.warning("Failed to persist auto-tuner decision for %s", parameter, exc_info=True)

        return action

    def get_config(self) -> dict[str, Any]:
        """Get current tuned configuration."""
        return dict(self._current_config)

    def get_history(self) -> list[dict[str, Any]]:
        """Get tuning action history.

        Returns:
            All recorded TuningActions serialized as plain dicts, ordered
            by the sequence in which they were applied. Each dict includes
            timestamp, trigger, parameter, old_value, new_value, rationale,
            and auto_applied fields.
        """
        from dataclasses import asdict

        return [asdict(a) for a in self._actions]


_auto_tuner: AutoTuner | None = None
_auto_tuner_lock = threading.Lock()


def get_auto_tuner() -> AutoTuner:
    """Return the singleton AutoTuner instance (thread-safe).

    Returns:
        The shared AutoTuner instance.
    """
    global _auto_tuner
    if _auto_tuner is None:
        with _auto_tuner_lock:
            if _auto_tuner is None:
                _auto_tuner = AutoTuner()
    return _auto_tuner
