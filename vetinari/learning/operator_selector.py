"""Thompson Sampling Operator Selector — Department 9.6.

Learns which mutation operators improve which agent modes through
Bayesian bandit selection.  Arms are keyed by
``(operator, agent_type, mode)`` with Beta(alpha, beta) distributions.

The selector integrates with the existing Thompson Sampling
infrastructure in ``model_selector.py`` but maintains a separate arm
space to avoid interference with model selection arms.

Decision: use structured operators + Thompson selection over GEPA/DSPy.
Structured operators are deterministic and free (no LLM call to
generate variants); Thompson selection learns per-mode effectiveness.
(ADR-0081)
"""

from __future__ import annotations

import json
import logging
import os
import random
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from vetinari.constants import VETINARI_STATE_DIR
from vetinari.learning.prompt_mutator import MutationOperator

logger = logging.getLogger(__name__)


# Minimum pulls before an arm's posterior is trusted over the prior
_MIN_PULLS_TRUSTED = 5

# Decay factor for non-stationarity — older observations matter less
_DECAY_FACTOR = 0.995


@dataclass
class OperatorArm:
    """Beta distribution arm for an (operator, agent_type, mode) triple.

    Args:
        operator: The mutation operator name.
        agent_type: The agent type value string.
        mode: The agent mode string.
        alpha: Successes (positive quality deltas).
        beta: Failures (negative or zero quality deltas).
        total_pulls: Total number of updates.
        last_updated: ISO timestamp of last update.
    """

    operator: str
    agent_type: str
    mode: str
    alpha: float = 1.0  # Uniform prior — no initial bias
    beta: float = 1.0
    total_pulls: int = 0
    last_updated: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def __repr__(self) -> str:
        return f"OperatorArm(operator={self.operator!r}, agent_type={self.agent_type!r}, mode={self.mode!r})"

    @property
    def mean(self) -> float:
        """Expected success rate of this operator for this agent+mode."""
        return self.alpha / (self.alpha + self.beta)

    def sample(self) -> float:
        """Sample from the Beta distribution.

        Returns:
            Sampled value in [0, 1].
        """
        try:
            return random.betavariate(max(self.alpha, 0.01), max(self.beta, 0.01))
        except ValueError:
            logger.warning(
                "Beta sampling failed (alpha=%.4f, beta=%.4f) — returning mean %.4f instead of sampling",
                self.alpha,
                self.beta,
                self.mean,
            )
            return self.mean

    def update(self, quality_delta: float) -> None:
        """Update arm based on observed quality change.

        Args:
            quality_delta: Quality score difference (post-mutation minus
                pre-mutation).  Positive means the operator improved
                quality.
        """
        # Apply decay to existing observations (non-stationarity)
        self.alpha *= _DECAY_FACTOR
        self.beta *= _DECAY_FACTOR

        if quality_delta > 0:
            self.alpha += 1.0
        else:
            self.beta += 1.0

        self.total_pulls += 1
        self.last_updated = datetime.now(timezone.utc).isoformat()


class OperatorSelector:
    """Learns which mutation operators improve which agent modes.

    Uses Thompson Sampling to balance exploration (trying under-tested
    operators) with exploitation (using operators known to work).

    Arms: ``(operator, agent_type, mode) → Beta(alpha, beta)``
    Reward signal: quality score delta (post-mutation vs pre-mutation)
    """

    def __init__(self, state_path: Path | None = None) -> None:
        self._arms: dict[str, OperatorArm] = {}
        self._lock = threading.Lock()
        self._arm_lock = threading.Lock()
        self._state_path = state_path or self._default_state_path()
        self._load_state()

    def select_operator(self, agent_type: str, mode: str) -> MutationOperator:
        """Select the most promising mutation operator for this agent+mode.

        Samples from each operator's Beta distribution and returns the
        one with the highest sampled value.

        Args:
            agent_type: The agent type value string.
            mode: The agent mode string.

        Returns:
            The selected MutationOperator.
        """
        best_score = -1.0
        best_op = MutationOperator.INSTRUCTION_REPHRASE  # Default fallback

        with self._lock:
            for op in MutationOperator:
                arm = self._get_or_create_arm(op.value, agent_type, mode)
                sample = arm.sample()
                if sample > best_score:
                    best_score = sample
                    best_op = op

        logger.debug(
            "Selected operator %s for %s/%s (sampled %.3f)",
            best_op.value,
            agent_type,
            mode,
            best_score,
        )
        return best_op

    def update(
        self,
        operator: MutationOperator,
        agent_type: str,
        mode: str,
        quality_delta: float,
    ) -> None:
        """Update arm after observing the quality impact of an operator.

        A positive quality_delta increments the arm's alpha (successes).
        A non-positive quality_delta increments the arm's beta (failures).

        Args:
            operator: The mutation operator that was applied.
            agent_type: The agent type value string.
            mode: The agent mode string.
            quality_delta: Quality difference (post minus pre).  Positive
                values are treated as successes; zero or negative as failures.
        """
        with self._lock, self._arm_lock:
            arm = self._get_or_create_arm(operator.value, agent_type, mode)
            arm.update(quality_delta)
            logger.debug(
                "Updated operator arm %s/%s/%s: delta=%.3f, alpha=%.2f, beta=%.2f, pulls=%d",
                operator.value,
                agent_type,
                mode,
                quality_delta,
                arm.alpha,
                arm.beta,
                arm.total_pulls,
            )
            self._save_state()

    def get_stats(
        self,
        agent_type: str | None = None,
        mode: str | None = None,
    ) -> list[dict]:
        """Get operator performance statistics.

        Args:
            agent_type: Filter by agent type string.  None for all.
            mode: Filter by mode.  None for all.

        Returns:
            List of arm statistics dictionaries sorted by mean descending.
        """
        with self._lock:
            stats = []
            for arm in self._arms.values():
                if agent_type and arm.agent_type != agent_type:
                    continue
                if mode and arm.mode != mode:
                    continue
                stats.append({
                    "operator": arm.operator,
                    "agent_type": arm.agent_type,
                    "mode": arm.mode,
                    "mean": round(arm.mean, 3),
                    "alpha": round(arm.alpha, 2),
                    "beta": round(arm.beta, 2),
                    "pulls": arm.total_pulls,
                })
            return sorted(stats, key=lambda s: s["mean"], reverse=True)

    def get_best_operator(self, agent_type: str, mode: str) -> MutationOperator | None:
        """Return the operator with the highest expected value (no sampling).

        Useful for reporting / dashboards where you want the current
        best-guess rather than an exploration-aware sample.

        Args:
            agent_type: The agent type value string.
            mode: The agent mode string.

        Returns:
            The best MutationOperator, or None if no arms have sufficient
            observations.
        """
        best_mean = -1.0
        best_op = None

        with self._lock:
            for op in MutationOperator:
                key = self._arm_key(op.value, agent_type, mode)
                arm = self._arms.get(key)
                if arm and arm.total_pulls >= _MIN_PULLS_TRUSTED and arm.mean > best_mean:
                    best_mean = arm.mean
                    best_op = op

        return best_op

    # ── Internal helpers ─────────────────────────────────────────────

    @staticmethod
    def _arm_key(operator: str, agent_type: str, mode: str) -> str:
        """Generate a unique key for an arm.

        Args:
            operator: Operator value string.
            agent_type: Agent type value string.
            mode: Mode string.

        Returns:
            Composite key string.
        """
        return f"op:{operator}:{agent_type}:{mode}"

    def _get_or_create_arm(
        self,
        operator: str,
        agent_type: str,
        mode: str,
    ) -> OperatorArm:
        """Get an existing arm or create a new one with uniform prior.

        Args:
            operator: Operator value string.
            agent_type: Agent type value string.
            mode: Mode string.

        Returns:
            The OperatorArm instance.
        """
        key = self._arm_key(operator, agent_type, mode)
        if key not in self._arms:
            self._arms[key] = OperatorArm(
                operator=operator,
                agent_type=agent_type,
                mode=mode,
            )
        return self._arms[key]

    # ── Persistence ──────────────────────────────────────────────────

    @staticmethod
    def _default_state_path() -> Path:
        """Resolve default path for operator selector state.

        Returns:
            Path to operator_selector_state.json.
        """
        state_dir_env = os.environ.get("VETINARI_STATE_DIR", "")
        if state_dir_env:
            state_dir = Path(state_dir_env)
        else:
            state_dir = VETINARI_STATE_DIR
        state_dir.mkdir(parents=True, exist_ok=True)
        return state_dir / "operator_selector_state.json"

    def _load_state(self) -> None:
        """Load arm state from JSON file."""
        try:
            if self._state_path.exists():
                with Path(self._state_path).open(encoding="utf-8") as f:
                    data = json.load(f)
                for key, arm_data in data.items():
                    self._arms[key] = OperatorArm(**arm_data)
                logger.debug(
                    "Loaded %d operator arms from %s",
                    len(self._arms),
                    self._state_path,
                )
        except Exception:
            logger.warning(
                "Could not load operator selector state from %s",
                self._state_path,
                exc_info=True,
            )

    def _save_state(self) -> None:
        """Persist arm state to JSON file."""
        try:
            data = {key: asdict(arm) for key, arm in self._arms.items()}
            with Path(self._state_path).open("w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except Exception:
            logger.warning(
                "Could not save operator selector state to %s",
                self._state_path,
                exc_info=True,
            )


# Module-level singleton
_operator_selector: OperatorSelector | None = None
_operator_selector_lock = threading.Lock()


def get_operator_selector() -> OperatorSelector:
    """Return the singleton OperatorSelector instance (thread-safe).

    Creates the instance on first call and registers an atexit handler to
    persist state on process exit.

    Returns:
        The shared OperatorSelector instance.
    """
    global _operator_selector
    if _operator_selector is None:
        with _operator_selector_lock:
            if _operator_selector is None:
                _operator_selector = OperatorSelector()
                import atexit

                atexit.register(_operator_selector._save_state)
    return _operator_selector
