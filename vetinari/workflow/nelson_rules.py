"""Nelson Rule Detector — eight standard SPC rules for process control.

Detects violations of the eight Nelson SPC rules using a rolling window
of observations.  Each rule maps to a :class:`~vetinari.workflow.andon.NelsonViolation`.
"""

from __future__ import annotations

import logging
from collections import deque

from vetinari.workflow.andon import NelsonViolation

logger = logging.getLogger(__name__)


class NelsonRuleDetector:
    """Detect violations of the eight Nelson SPC rules.

    The detector maintains a rolling window of observations and checks all
    eight Nelson rules on each new data point.  Rule 1 is evaluated
    immediately because a single point outside 3 sigma is critical on its own;
    the remaining multi-point rules require enough warm-up history.

    Usage::

        detector = NelsonRuleDetector(mean=50.0, sigma=5.0)
        violations = detector.check_all_rules(observation=65.0)
        for v in violations:
            logger.warning("Nelson rule %d violated: %s", v.rule, v.description)
    """

    def __init__(self, mean: float, sigma: float, window_size: int = 50) -> None:
        """Initialise the detector with known process parameters.

        Args:
            mean: The process mean (center line).
            sigma: The process standard deviation (one sigma).
            window_size: Maximum number of observations to retain in the
                rolling window.  Defaults to 50.
        """
        self._mean = mean
        self._sigma = sigma
        self._window: deque[float] = deque(maxlen=window_size)
        self._update_limits()

    # -- limit management ---------------------------------------------------

    def _update_limits(self) -> None:
        """Recompute the sigma-banded control limits from mean and sigma."""
        self._ucl = self._mean + 3 * self._sigma
        self._lcl = self._mean - 3 * self._sigma
        self._ucl2 = self._mean + 2 * self._sigma
        self._lcl2 = self._mean - 2 * self._sigma
        self._ucl1 = self._mean + self._sigma
        self._lcl1 = self._mean - self._sigma

    def update_control_limits(self, mean: float, sigma: float) -> None:
        """Update the process mean and sigma, then recompute all limits.

        Args:
            mean: New process mean.
            sigma: New process standard deviation.
        """
        self._mean = mean
        self._sigma = sigma
        self._update_limits()
        logger.debug(
            "Nelson limits updated: mean=%.4f sigma=%.4f UCL=%.4f LCL=%.4f",
            mean,
            sigma,
            self._ucl,
            self._lcl,
        )

    # -- main interface -----------------------------------------------------

    def check_all_rules(self, observation: float) -> list[NelsonViolation]:
        """Append an observation and return detected Nelson-rule violations.

        Returns:
            Value produced for the caller.
        """
        self._window.append(observation)
        violations: list[NelsonViolation] = []
        pts = list(self._window)
        if self._check_rule1(pts):
            violations.append(
                NelsonViolation(
                    rule=1,
                    severity="critical",
                    description="One point beyond 3 sigma control limits",
                ),
            )
        if len(self._window) < 15:
            return violations
        if self._check_rule2(pts):
            violations.append(
                NelsonViolation(
                    rule=2,
                    severity="warning",
                    description="Nine consecutive points on the same side of the center line",
                ),
            )
        if self._check_rule3(pts):
            violations.append(
                NelsonViolation(
                    rule=3,
                    severity="warning",
                    description="Six consecutive points steadily increasing or decreasing",
                ),
            )
        if self._check_rule4(pts):
            violations.append(
                NelsonViolation(
                    rule=4,
                    severity="warning",
                    description="Fourteen consecutive points alternating up and down",
                ),
            )
        if self._check_rule5(pts):
            violations.append(
                NelsonViolation(
                    rule=5,
                    severity="warning",
                    description="Two of three consecutive points beyond 2 sigma on the same side",
                ),
            )
        if self._check_rule6(pts):
            violations.append(
                NelsonViolation(
                    rule=6,
                    severity="warning",
                    description="Four of five consecutive points beyond 1 sigma on the same side",
                ),
            )
        if self._check_rule7(pts):
            violations.append(
                NelsonViolation(
                    rule=7,
                    severity="info",
                    description="Fifteen consecutive points within 1 sigma of the center line",
                ),
            )
        if self._check_rule8(pts):
            violations.append(
                NelsonViolation(
                    rule=8,
                    severity="warning",
                    description="Eight consecutive points beyond 1 sigma on either side of center",
                ),
            )
        return violations

    # -- individual rule checks ---------------------------------------------

    def _check_rule1(self, pts: list[float]) -> bool:
        """One point beyond 3 sigma (the most recent point)."""
        last = pts[-1]
        return last > self._ucl or last < self._lcl

    def _check_rule2(self, pts: list[float]) -> bool:
        """Nine consecutive points on the same side of the mean."""
        tail = pts[-9:]
        if len(tail) < 9:
            return False
        above = all(p > self._mean for p in tail)
        below = all(p < self._mean for p in tail)
        return above or below

    @staticmethod
    def _check_rule3(pts: list[float]) -> bool:
        """Six consecutive points steadily increasing or decreasing."""
        tail = pts[-6:]
        if len(tail) < 6:
            return False
        increasing = all(tail[i] < tail[i + 1] for i in range(5))
        decreasing = all(tail[i] > tail[i + 1] for i in range(5))
        return increasing or decreasing

    @staticmethod
    def _check_rule4(pts: list[float]) -> bool:
        """Fourteen consecutive points alternating up and down."""
        tail = pts[-14:]
        if len(tail) < 14:
            return False
        diffs = [tail[i + 1] - tail[i] for i in range(len(tail) - 1)]
        if any(diff == 0 for diff in diffs):
            return False
        return all((diffs[i] > 0) != (diffs[i + 1] > 0) for i in range(len(diffs) - 1))

    def _check_rule5(self, pts: list[float]) -> bool:
        """Two of three consecutive points beyond 2 sigma on the same side."""
        tail = pts[-3:]
        if len(tail) < 3:
            return False
        above = sum(1 for p in tail if p > self._ucl2)
        below = sum(1 for p in tail if p < self._lcl2)
        return above >= 2 or below >= 2

    def _check_rule6(self, pts: list[float]) -> bool:
        """Four of five consecutive points beyond 1 sigma on the same side."""
        tail = pts[-5:]
        if len(tail) < 5:
            return False
        above = sum(1 for p in tail if p > self._ucl1)
        below = sum(1 for p in tail if p < self._lcl1)
        return above >= 4 or below >= 4

    def _check_rule7(self, pts: list[float]) -> bool:
        """Fifteen consecutive points within 1 sigma of the center line."""
        tail = pts[-15:]
        if len(tail) < 15:
            return False
        return all(self._lcl1 <= p <= self._ucl1 for p in tail)

    def _check_rule8(self, pts: list[float]) -> bool:
        """Eight consecutive points beyond 1 sigma on either side (stratification)."""
        tail = pts[-8:]
        if len(tail) < 8:
            return False
        return all(p > self._ucl1 or p < self._lcl1 for p in tail)
