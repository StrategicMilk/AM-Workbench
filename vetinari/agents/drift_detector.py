"""Agent drift detector — track quality per-session and trigger behavioral anchoring.

Monitors quality scores for each agent across a session using a sliding
window. When quality trend drops below the session mean minus a
configurable margin for N consecutive scores, triggers an anchoring
callback to re-inject the system prompt and clear accumulated context.

Pipeline role: Execute → Completion → **Drift Check** → Learn.
"""

from __future__ import annotations

import logging
import threading
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone

from vetinari.boundary_guards import require_nonempty
from vetinari.types import AgentType

logger = logging.getLogger(__name__)


# -- Configuration defaults --
DEFAULT_DRIFT_WINDOW = 10  # Sliding window size for score tracking
DEFAULT_CONSECUTIVE_THRESHOLD = 3  # Consecutive below-mean scores to trigger drift
DEFAULT_DRIFT_MARGIN = 0.1  # How far below mean counts as "drift"


@dataclass(frozen=True, slots=True)
class DriftReport:
    """Report generated when agent drift is detected."""

    agent_type: AgentType
    session_id: str
    window_scores: tuple[float, ...]
    session_mean: float
    drift_magnitude: float  # How far below mean the recent scores are
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def __repr__(self) -> str:
        return f"DriftReport(agent_type={self.agent_type!r}, drift_magnitude={self.drift_magnitude!r})"


# Type alias for the callback that receives drift notifications
DriftCallback = Callable[[AgentType, DriftReport], None]


class AgentDriftDetector:
    """Track per-agent quality scores and detect behavioral drift.

    Maintains a sliding window of quality scores per (agent_type, session_id).
    When the last N consecutive scores fall below (session_mean - margin),
    triggers the on_drift_detected callback.

    Args:
        window_size: Number of recent scores to track.
        consecutive_threshold: How many consecutive below-mean scores trigger drift.
        drift_margin: How far below the session mean counts as "drifting".
        on_drift_detected: Optional callback invoked when drift is detected.
    """

    def __init__(
        self,
        window_size: int = DEFAULT_DRIFT_WINDOW,
        consecutive_threshold: int = DEFAULT_CONSECUTIVE_THRESHOLD,
        drift_margin: float = DEFAULT_DRIFT_MARGIN,
        on_drift_detected: DriftCallback | None = None,
    ) -> None:
        self._window_size = window_size
        self._consecutive_threshold = consecutive_threshold
        self._drift_margin = drift_margin
        self._on_drift_detected = on_drift_detected
        self._baseline_anchor: dict | None = (
            anchor_baseline(
                baseline={"window_size": window_size, "consecutive_threshold": consecutive_threshold},
                on_drift=lambda _baseline: on_drift_detected,
            )
            if on_drift_detected is not None
            else None
        )
        # scores keyed by (agent_type.value, session_id) -> list of scores
        self._scores: dict[str, list[float]] = defaultdict(list)
        # Track which window tuple last fired the drift callback to prevent
        # re-firing on repeated check_drift() calls with the same stale window.
        self._last_drift_window: dict[str, tuple[float, ...]] = {}
        self._lock = threading.Lock()

    def record_score(
        self,
        agent_type: AgentType,
        session_id: str,
        score: float,
    ) -> None:
        """Record a quality score for an agent in a session.

        Args:
            agent_type: The type of agent that produced this score.
            session_id: Identifier for the current session.
            score: Quality score between 0.0 and 1.0.
        """
        key = self._make_key(agent_type, session_id)
        with self._lock:
            scores = self._scores[key]
            scores.append(score)
            # Trim to window size
            if len(scores) > self._window_size:
                self._scores[key] = scores[-self._window_size :]

    def check_drift(
        self,
        agent_type: AgentType,
        session_id: str,
    ) -> DriftReport | None:
        """Check if the agent is exhibiting quality drift in this session.

        Drift is detected when the last N consecutive scores are all
        below (session_mean - drift_margin).

        Args:
            agent_type: The agent type to check.
            session_id: The session to check.

        Returns:
            DriftReport if drift detected, None otherwise.
        """
        key = self._make_key(agent_type, session_id)
        with self._lock:
            scores = self._scores.get(key, [])
            if len(scores) < self._consecutive_threshold:
                return None

            session_mean = sum(scores) / len(scores)
            threshold = session_mean - self._drift_margin

            # Check last N scores
            recent = scores[-self._consecutive_threshold :]
            is_drifting = all(s < threshold for s in recent)

            if not is_drifting:
                return None

            drift_magnitude = session_mean - (sum(recent) / len(recent))
            window_tuple = tuple(scores)
            report = DriftReport(
                agent_type=agent_type,
                session_id=session_id,
                window_scores=window_tuple,
                session_mean=session_mean,
                drift_magnitude=drift_magnitude,
            )

            # Deduplicate: only fire the callback when the window content
            # has changed since the last callback for this key.  Repeated
            # check_drift() calls on a stale window must NOT re-fire.
            already_fired = self._last_drift_window.get(key) == window_tuple
            if not already_fired:
                self._last_drift_window[key] = window_tuple
            fire_callback = not already_fired

        # Fire callback outside the lock to avoid deadlocks
        if fire_callback:
            logger.warning(
                "Agent drift detected for %s in session %s — magnitude %.3f",
                agent_type.value,
                session_id,
                report.drift_magnitude,
            )
            if self._on_drift_detected:
                try:
                    self._on_drift_detected(agent_type, report)
                except Exception:
                    logger.warning(
                        "Drift callback failed for %s — anchoring may not have occurred",
                        agent_type.value,
                    )
        return report

    def reset_session(self, agent_type: AgentType, session_id: str) -> None:
        """Clear all tracked scores for an agent in a session.

        Call this after anchoring to start fresh drift tracking.

        Args:
            agent_type: The agent type to reset.
            session_id: The session to reset.
        """
        key = self._make_key(agent_type, session_id)
        with self._lock:
            self._scores.pop(key, None)
            self._last_drift_window.pop(key, None)
        logger.info(
            "Drift tracking reset for %s in session %s",
            agent_type.value,
            session_id,
        )

    def get_session_scores(self, agent_type: AgentType, session_id: str) -> list[float]:
        """Get the current score window for an agent session.

        Args:
            agent_type: The agent type.
            session_id: The session identifier.

        Returns:
            List of scores in the current window (may be empty).
        """
        key = self._make_key(agent_type, session_id)
        with self._lock:
            return list(self._scores.get(key, []))

    @staticmethod
    def _make_key(agent_type: AgentType, session_id: str) -> str:
        return f"{agent_type.value}:{session_id}"


# -- Singleton access --
_instance: AgentDriftDetector | None = None
_instance_lock = threading.Lock()


def get_drift_detector() -> AgentDriftDetector:
    """Get the singleton AgentDriftDetector instance.

    Returns:
        The shared AgentDriftDetector instance.
    """
    global _instance
    if _instance is None:
        with _instance_lock:
            if _instance is None:
                _instance = AgentDriftDetector()
    return _instance


def anchor_baseline(*, baseline: dict, on_drift: Callable[[dict], object] | None) -> dict:
    """Anchor a drift baseline only when a drift callback is registered.

    Returns:
        Copied baseline plus the drift callback reference.
    """
    require_nonempty(str(on_drift) if on_drift is not None else "", field_name="on_drift")
    return {"baseline": dict(baseline), "on_drift": on_drift}
