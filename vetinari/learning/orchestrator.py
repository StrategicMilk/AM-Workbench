"""Learning Orchestrator — coordinates all self-improvement modules into one loop.

Runs as a background thread started by ``lifespan.py``.  Every cycle (default
30 minutes) it consults the :class:`MetaOptimizer` to decide which improvement
strategy has the highest ROI, then dispatches work to the relevant subsystem:

- **prompt_evolution**: checks shadow test results and pending promotions
- **training**: evaluates data sufficiency and triggers retraining when needed
- **auto_research**: asks the model scout for better alternatives

Safety rails:
- SATURATION detected → switches to the next-best strategy automatically
- COLLAPSE_RISK detected → halts all learning, publishes an alert event
- Quality drops >5% after an action → auto-rollback within 24 hours

Pipeline role: Background Loop → **Learning Orchestrator** → MetaOptimizer → Subsystems.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any

from vetinari.clock import Clock, SystemClock, utc_now_iso

logger = logging.getLogger(__name__)


# ── Configuration defaults ───────────────────────────────────────────────────

CYCLE_INTERVAL_SECONDS = 30 * 60  # 30 minutes between learning cycles
QUALITY_DROP_THRESHOLD = 0.05  # 5% quality drop triggers rollback
ROLLBACK_WINDOW_HOURS = 24  # Monitor for degradation within this window after an action


# ── Rollback tracking ───────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ActionBaseline:
    """Tracks a quality baseline captured before an improvement action.

    Used to detect degradation: if quality drops more than
    ``QUALITY_DROP_THRESHOLD`` from ``baseline_quality`` within
    ``ROLLBACK_WINDOW_HOURS``, the action is rolled back.

    Args:
        action_id: Identifier for the action (strategy + timestamp).
        strategy: Which strategy produced this action.
        baseline_quality: Average quality score before the action.
        recorded_at: When the baseline was captured (UTC ISO-8601).
        rolled_back: Whether this action has already been rolled back.
    """

    action_id: str
    strategy: str
    baseline_quality: float
    recorded_at: str = field(default_factory=utc_now_iso)
    rolled_back: bool = False

    def __repr__(self) -> str:
        return (
            f"ActionBaseline(action_id={self.action_id!r}, strategy={self.strategy!r},"
            f" baseline_quality={self.baseline_quality!r})"
        )


# ── Orchestrator ─────────────────────────────────────────────────────────────


class LearningOrchestrator:
    """Coordinates all learning subsystems into a single improvement loop.

    Each cycle:
    1. Asks :class:`MetaOptimizer` for the best strategy
    2. Dispatches to the chosen subsystem
    3. Records the outcome back to the MetaOptimizer
    4. Checks recent actions for quality degradation (auto-rollback)

    Side effects:
      - Starts a daemon thread (``learning-orchestrator``) on :meth:`start`
      - Publishes ``KaizenImprovementReverted`` events on rollback
      - Publishes ``HumanApprovalNeeded`` on COLLAPSE_RISK detection
    """

    def __init__(self, cycle_interval_seconds: int = CYCLE_INTERVAL_SECONDS, clock: Clock | None = None) -> None:
        self._cycle_interval = cycle_interval_seconds
        self._clock = clock or SystemClock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._action_baselines: list[ActionBaseline] = []
        self._cycle_count = 0

    def start(self) -> None:
        """Start the background learning loop.

        Launches a daemon thread that runs improvement cycles at the
        configured interval.  Safe to call multiple times — subsequent
        calls are no-ops if the thread is already running.
        """
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                logger.warning("LearningOrchestrator already running — ignoring duplicate start()")
                return
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._run_loop,
                name="learning-orchestrator",
                daemon=True,
            )
            self._thread.start()
        logger.info("LearningOrchestrator started — cycle interval %ds", self._cycle_interval)

    def stop(self) -> None:
        """Signal the background loop to stop and wait for it to exit.

        Blocks for up to 10 seconds.  If the thread does not exit in
        time a warning is logged but shutdown continues.
        """
        self._stop_event.set()
        with self._lock:
            thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=10)
            if thread.is_alive():
                logger.warning("LearningOrchestrator thread did not exit within 10s — proceeding with shutdown")
        logger.info("LearningOrchestrator stopped after %d cycles", self._cycle_count)

    @property
    def is_running(self) -> bool:
        """Whether the background loop thread is alive."""
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    # ── Background loop ──────────────────────────────────────────────────

    def _run_loop(self) -> None:
        """Main loop — runs until stop() is called."""
        logger.info("LearningOrchestrator loop started")
        while not self._stop_event.is_set():
            try:
                self._run_cycle()
            except Exception:
                logger.exception(
                    "LearningOrchestrator cycle %d failed — will retry next cycle",
                    self._cycle_count,
                )
            self._stop_event.wait(timeout=self._cycle_interval)

    def _run_cycle(self) -> None:
        """Execute one improvement cycle."""
        self._cycle_count += 1
        logger.info("[LearningOrchestrator] Starting cycle %d", self._cycle_count)

        from vetinari.learning.meta_optimizer import LearningPhase, get_meta_optimizer

        optimizer = get_meta_optimizer()
        phase = optimizer.detect_phase()

        # ── COLLAPSE_RISK: halt everything, alert user ───────────────
        if phase == LearningPhase.COLLAPSE_RISK:
            self._handle_collapse_risk()
            return

        # ── SATURATION: pick next-best strategy from ROI rankings ────
        if phase == LearningPhase.SATURATION:
            strategy = self._pick_saturation_strategy(optimizer)
        else:
            strategy = optimizer.suggest_next_strategy()

        if strategy is None:
            logger.info("[LearningOrchestrator] No strategy suggested — skipping cycle")
            return

        logger.info(
            "[LearningOrchestrator] Phase=%s, strategy=%s",
            phase,
            strategy,
        )

        # ── Capture baseline before action ───────────────────────────
        baseline_quality = self._get_current_quality()
        action_id = f"{strategy}_{self._cycle_count}"

        # ── Dispatch to subsystem ────────────────────────────────────
        quality_gain = self._dispatch_strategy(strategy)

        # ── Record outcome ───────────────────────────────────────────
        optimizer.record_cycle(strategy, quality_gain)

        # ── Track baseline for rollback monitoring ───────────────────
        if baseline_quality is not None:
            self._action_baselines.append(
                ActionBaseline(
                    action_id=action_id,
                    strategy=strategy,
                    baseline_quality=baseline_quality,
                )
            )

        # ── Check for quality degradation on recent actions ──────────
        self._check_for_rollback()

        logger.info(
            "[LearningOrchestrator] Cycle %d complete — strategy=%s, gain=%+.4f",
            self._cycle_count,
            strategy,
            quality_gain,
        )

    # ── Strategy dispatch ────────────────────────────────────────────────

    def _dispatch_strategy(self, strategy: str) -> float:
        """Run the chosen improvement strategy and return the quality gain.

        Args:
            strategy: One of 'prompt_evolution', 'training', 'auto_research'.

        Returns:
            Estimated quality gain from this cycle (positive = improvement).
        """
        dispatch_map: dict[str, Any] = {
            "prompt_evolution": self._run_prompt_evolution,
            "training": self._run_training,
            "auto_research": self._run_auto_research,
        }

        handler = dispatch_map.get(strategy)
        if handler is None:
            logger.warning(
                "[LearningOrchestrator] Unknown strategy %r — skipping",
                strategy,
            )
            return 0.0

        return handler()

    @staticmethod
    def _run_prompt_evolution() -> float:
        """Check shadow test results and pending variant promotions.

        Returns:
            Quality gain estimate (0.0 if no promotions occurred).
        """
        try:
            from vetinari.learning.prompt_evolver import get_prompt_evolver

            evolver = get_prompt_evolver()
            evolver.check_shadow_test_results()
            logger.info("[LearningOrchestrator] Prompt evolution: checked shadow test results")
            return 0.0  # Gain recorded by the evolver itself when promotions happen
        except Exception:
            logger.exception("[LearningOrchestrator] Prompt evolution cycle failed — no changes applied")
            return 0.0

    @staticmethod
    def _run_training() -> float:
        """Check data sufficiency and trigger retraining if recommended.

        Queries the :class:`~vetinari.learning.training_manager.TrainingManager`
        to decide whether retraining is warranted, then dispatches a bounded
        manual cycle through :meth:`~vetinari.training.idle_scheduler.TrainingScheduler.start_manual_cycle`
        so the orchestrator does real work rather than only logging a recommendation.

        Returns:
            ``0.01`` when a training cycle was successfully dispatched (small
            positive signal — actual gain is measured after the cycle completes),
            or ``0.0`` when no training was needed, the cycle was already running,
            or any error occurred.
        """
        try:
            from vetinari.learning.training_manager import get_training_manager

            manager = get_training_manager()
            # Check all known model+task combinations for retraining needs
            recommendation = manager.should_retrain(model_id="*", task_type="*")
            if recommendation is not None and recommendation.recommended:
                logger.info(
                    "[LearningOrchestrator] Training recommended: %s — dispatching training cycle",
                    recommendation.reason,
                )
                from vetinari.training.idle_scheduler import get_training_scheduler

                scheduler = get_training_scheduler()
                result = scheduler.start_manual_cycle(
                    activity_description=f"Orchestrator-triggered retraining: {recommendation.reason}"
                )
                if result == "already_running":
                    logger.info("[LearningOrchestrator] Training cycle already running — skipping dispatch")
                    return 0.0
                logger.info(
                    "[LearningOrchestrator] Training cycle dispatched: job=%s",
                    result,
                )
                return 0.01  # Small positive signal; actual gain measured later
            logger.info("[LearningOrchestrator] Training: no retraining needed at this time")
            return 0.0
        except Exception:
            logger.exception("[LearningOrchestrator] Training check failed — no training triggered")
            return 0.0

    @staticmethod
    def _run_auto_research() -> float:
        """Ask model scout for better alternatives for underperforming task types.

        Returns:
            Quality gain estimate (0.0 — research doesn't directly improve quality).
        """
        try:
            from vetinari.models.model_scout import ModelScout

            scout = ModelScout()
            # Scout for general task types where models may be underperforming
            for task_type in ("coding", "documentation", "research"):
                recommendations = scout.scout_for_task(task_type)
                if recommendations:
                    logger.info(
                        "[LearningOrchestrator] Auto-research found %d model(s) for %s: %s",
                        len(recommendations),
                        task_type,
                        ", ".join(r.model_name for r in recommendations[:3]),
                    )
            return 0.0  # Research phase — no direct quality gain
        except Exception:
            logger.exception("[LearningOrchestrator] Auto-research cycle failed — no recommendations made")
            return 0.0

    # ── Phase handlers ───────────────────────────────────────────────────

    def _handle_collapse_risk(self) -> None:
        """Halt all learning and alert the user when quality is collapsing.

        Sets the stop event so the outer loop exits after this cycle returns.
        The event bus alert is best-effort; the halt is unconditional.
        """
        logger.warning("[LearningOrchestrator] COLLAPSE_RISK detected — halting all learning activities")
        # Stop the loop unconditionally — must come before the alert so that
        # a bus failure cannot prevent the halt.
        self._stop_event.set()
        try:
            from vetinari.events import HumanApprovalNeeded, get_event_bus

            bus = get_event_bus()
            bus.publish(
                HumanApprovalNeeded(
                    event_type="HumanApprovalNeeded",
                    timestamp=self._event_timestamp(),
                    task_id="learning-orchestrator-collapse",
                    reason="Quality collapse risk detected — all learning halted until manual review",
                    context={"cycle": self._cycle_count, "phase": "collapse_risk"},
                )
            )
        except Exception:
            logger.exception("[LearningOrchestrator] Could not publish collapse alert — event bus unavailable")

    @staticmethod
    def _pick_saturation_strategy(optimizer: Any) -> str | None:
        """When saturated, pick a different strategy from ROI rankings.

        Instead of repeating the top strategy (which is plateauing), try
        the second-best to explore alternative improvement paths.

        Args:
            optimizer: The MetaOptimizer instance.

        Returns:
            Strategy name or None if no alternatives exist.
        """
        rankings = optimizer.get_roi_rankings()
        if len(rankings) >= 2:
            alt = rankings[1]["strategy"]
            logger.info(
                "[LearningOrchestrator] SATURATION — switching from %s to %s",
                rankings[0]["strategy"],
                alt,
            )
            return alt
        if rankings:
            return rankings[0]["strategy"]
        return "prompt_evolution"  # Default when no data exists

    # ── Quality monitoring and rollback ───────────────────────────────────

    @staticmethod
    def _get_current_quality() -> float | None:
        """Fetch the current average quality score across recent evaluations.

        Returns:
            Average quality score (0.0-1.0), or None if unavailable.
        """
        try:
            from vetinari.learning.quality_scorer import get_quality_scorer

            scorer = get_quality_scorer()
            recent = scorer.get_history()[:20]
            if not recent:
                return None
            return sum(s.overall_score for s in recent) / len(recent)
        except Exception:
            logger.warning(
                "[LearningOrchestrator] Could not fetch current quality — scorer unavailable",
                exc_info=True,
            )
            return None

    def _check_for_rollback(self) -> None:
        """Check recent actions for quality degradation and trigger rollback if needed.

        Compares current quality against the baseline captured before each
        action.  If quality has dropped by more than ``QUALITY_DROP_THRESHOLD``
        (5%) and the action is within the rollback window (24h), the action
        is flagged and a rollback event is published.
        """
        current_quality = self._get_current_quality()
        if current_quality is None:
            return

        now = self._clock.utc_now()
        still_active: list[ActionBaseline] = []

        for baseline in self._action_baselines:
            if baseline.rolled_back:
                continue

            # Check if still within rollback window
            recorded = datetime.fromisoformat(baseline.recorded_at)
            hours_elapsed = (now - recorded).total_seconds() / 3600

            if hours_elapsed > ROLLBACK_WINDOW_HOURS:
                # Past the rollback window — action is considered stable
                continue

            still_active.append(baseline)

            # Check for quality degradation
            if baseline.baseline_quality <= 0:
                continue
            quality_drop = baseline.baseline_quality - current_quality
            drop_fraction = quality_drop / baseline.baseline_quality

            if drop_fraction > QUALITY_DROP_THRESHOLD:
                logger.warning(
                    "[LearningOrchestrator] Quality degradation detected after action %s: "
                    "baseline=%.3f, current=%.3f, drop=%.1f%% — triggering rollback",
                    baseline.action_id,
                    baseline.baseline_quality,
                    current_quality,
                    drop_fraction * 100,
                )
                rolled_back = replace(baseline, rolled_back=True)
                still_active[-1] = rolled_back
                self._publish_rollback_event(rolled_back, current_quality)

        self._action_baselines = still_active

    def _event_timestamp(self) -> float:
        return self._clock.utc_now().timestamp()

    def _publish_rollback_event(self, baseline: ActionBaseline, current_quality: float) -> None:
        """Publish a KaizenImprovementReverted event for a rolled-back action.

        Args:
            baseline: The action baseline that triggered the rollback.
            current_quality: The current quality score at the time of rollback.
        """
        try:
            from vetinari.events import KaizenImprovementReverted, get_event_bus

            bus = get_event_bus()
            bus.publish(
                KaizenImprovementReverted(
                    event_type="KaizenImprovementReverted",
                    timestamp=self._event_timestamp(),
                    improvement_id=baseline.action_id,
                    metric="overall_quality",
                    reason=(
                        f"Quality dropped from {baseline.baseline_quality:.3f} to "
                        f"{current_quality:.3f} ({baseline.strategy} strategy) — "
                        f"auto-rollback triggered within {ROLLBACK_WINDOW_HOURS}h window"
                    ),
                )
            )
            logger.info(
                "[LearningOrchestrator] Published rollback event for action %s",
                baseline.action_id,
            )
        except Exception:
            logger.exception(
                "[LearningOrchestrator] Could not publish rollback event for %s — event bus unavailable",
                baseline.action_id,
            )


# ── Singleton ────────────────────────────────────────────────────────────────

# Written by: get_learning_orchestrator()
# Read by: lifespan.py startup, tests
_learning_orchestrator: LearningOrchestrator | None = None
_orchestrator_lock = threading.Lock()


def get_learning_orchestrator() -> LearningOrchestrator:
    """Return the singleton LearningOrchestrator instance (thread-safe).

    Uses double-checked locking to avoid contention after the first call.

    Returns:
        The shared :class:`LearningOrchestrator` instance.
    """
    global _learning_orchestrator
    if _learning_orchestrator is None:
        with _orchestrator_lock:
            if _learning_orchestrator is None:
                _learning_orchestrator = LearningOrchestrator()
    return _learning_orchestrator


def reset_learning_orchestrator() -> None:
    """Destroy the singleton so the next call creates a fresh instance.

    Stops the background thread if running.  Intended for test isolation.
    """
    global _learning_orchestrator
    with _orchestrator_lock:
        if _learning_orchestrator is not None:
            _learning_orchestrator.stop()
        _learning_orchestrator = None
