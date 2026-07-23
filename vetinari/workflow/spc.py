"""Statistical Process Control (SPC) — control charts, alerts, and monitoring.

Provides:

* **SPCAlert**     -- alert raised when a metric goes out of statistical control.
* **ControlChart** -- per-metric control chart with UCL/LCL, Cpk.
* **SPCMonitor**   -- multi-metric SPC monitor with alert generation.

Andon, Nelson rules, and WIP tracking are in their own modules:
:mod:`~vetinari.workflow.andon`, :mod:`~vetinari.workflow.nelson_rules`,
:mod:`~vetinari.workflow.wip`.
"""

from __future__ import annotations

import enum
import logging
import math
import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


# =========================================================================
# SPC -- Statistical Process Control
# =========================================================================


class SPCAlertType(str, enum.Enum):
    """Typed alert categories emitted by SPC control charts."""

    ABOVE_UCL = "above_ucl"
    BELOW_LCL = "below_lcl"
    TREND = "trend"
    RUN = "run"


@dataclass(frozen=True, slots=True)
class SPCAlert:
    """Alert raised when a metric goes out of statistical control."""

    metric_name: str
    value: float
    ucl: float
    lcl: float
    mean: float
    sigma: float
    alert_type: SPCAlertType
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def __post_init__(self) -> None:
        if not isinstance(self.alert_type, SPCAlertType):
            object.__setattr__(self, "alert_type", SPCAlertType(self.alert_type))

    def __repr__(self) -> str:
        """Show key identifying fields for debugging."""
        return f"SPCAlert(metric_name={self.metric_name!r}, alert_type={self.alert_type!r}, value={self.value!r})"


@dataclass
class ControlChart:
    """A control chart for a single metric.

    Maintains a rolling window of observations and derives the mean,
    standard deviation, and upper/lower control limits (mean +/- k * sigma).
    """

    metric_name: str
    values: deque[float] = field(default_factory=lambda: deque(maxlen=1000))
    timestamps: deque[str] = field(default_factory=lambda: deque(maxlen=1000))
    window_size: int = 30
    sigma_multiplier: float = 3.0

    def __repr__(self) -> str:
        """Show key identifying fields for debugging."""
        return (
            f"ControlChart(metric_name={self.metric_name!r},"
            f" window_size={self.window_size!r}, values={len(self.values)!r})"
        )

    # -- derived statistics -------------------------------------------------

    @property
    def _window(self) -> list[float]:
        """Return the most recent *window_size* values."""
        return list(self.values)[-self.window_size :]

    @property
    def mean(self) -> float:
        """Arithmetic mean of the current window."""
        w = self._window
        if not w:
            return 0.0
        return sum(w) / len(w)

    @property
    def sigma(self) -> float:
        """Sample standard deviation of the current window (Bessel-corrected)."""
        w = self._window
        n = len(w)
        if n < 2:
            return 0.0
        m = self.mean
        variance = sum((x - m) ** 2 for x in w) / (n - 1)  # Bessel's correction
        return math.sqrt(variance)

    @property
    def ucl(self) -> float:
        """Upper control limit = mean + k * sigma."""
        return self.mean + self.sigma_multiplier * self.sigma

    @property
    def lcl(self) -> float:
        """Lower control limit = mean - k * sigma."""
        return self.mean - self.sigma_multiplier * self.sigma

    # -- capability ---------------------------------------------------------

    def get_cpk(
        self,
        spec_upper: float | None = None,
        spec_lower: float | None = None,
    ) -> float | None:
        """Process capability index (Cpk).

        Cpk > 1.33 indicates a capable process; 1.0-1.33 is marginal;
        Cpk <= 1.0 requires intervention.

        Args:
            spec_upper: Upper specification limit, or None to omit the
                upper calculation.
            spec_lower: Lower specification limit, or None to omit the
                lower calculation.

        Returns:
            The minimum of the upper and lower Cpk components, or None when
            sigma is zero or no spec limits are provided.
        """
        s = self.sigma
        if s == 0.0:
            return None
        m = self.mean

        cpk_values: list[float] = []
        if spec_upper is not None:
            cpk_values.append((spec_upper - m) / (3 * s))
        if spec_lower is not None:
            cpk_values.append((m - spec_lower) / (3 * s))

        if not cpk_values:
            return None
        return min(cpk_values)

    # -- control check ------------------------------------------------------

    def is_in_control(self, value: float) -> bool:
        """Return ``True`` if *value* falls within the control limits.

        Args:
            value: The observation to test.

        Returns:
            True when *value* is between LCL and UCL (inclusive), or when
            sigma is zero (no variation to trigger an alert).
        """
        if self.sigma == 0.0:
            return True
        return self.lcl <= value <= self.ucl

    def add_value(self, value: float, timestamp: str | None = None) -> None:
        """Append an observation to the chart.

        Args:
            value: The value.
            timestamp: The timestamp.
        """
        self.values.append(value)
        self.timestamps.append(timestamp or datetime.now(timezone.utc).isoformat())

    # -- trend / run detection (Western Electric rules) ---------------------

    def _detect_trend(self, min_consecutive: int = 7) -> bool:
        """Detect a monotone trend of *min_consecutive* points."""
        w = self._window
        if len(w) < min_consecutive:
            return False
        tail = w[-min_consecutive:]
        increasing = all(tail[i] < tail[i + 1] for i in range(len(tail) - 1))
        decreasing = all(tail[i] > tail[i + 1] for i in range(len(tail) - 1))
        return increasing or decreasing

    def _detect_run(self, min_run: int = 8) -> bool:
        """Detect a run of *min_run* consecutive points on one side of the mean."""
        w = self._window
        if len(w) < min_run:
            return False
        m = self.mean
        tail = w[-min_run:]
        above = all(v > m for v in tail)
        below = all(v < m for v in tail)
        return above or below


# =========================================================================
# SPC Monitor
# =========================================================================


class SPCMonitor:
    """Multi-metric SPC monitor with alert generation.

    Usage::

        monitor = SPCMonitor()
        alert = monitor.update("quality_score", 0.92)
        if alert:
            logger.debug("Out of control: %s", alert.alert_type)
    """

    DEFAULT_METRICS = ["quality_score", "latency_ms", "token_count", "drift_score"]

    def __init__(self, window_size: int = 30, sigma_multiplier: float = 3.0):
        self._charts: dict[str, ControlChart] = {}
        self._alerts: deque[SPCAlert] = deque(maxlen=200)
        self._window_size = window_size
        self._sigma_multiplier = sigma_multiplier

    # -- public API ---------------------------------------------------------

    def update(self, metric_name: str, value: float) -> SPCAlert | None:
        """Record a new observation and return an alert if out of control.

        Args:
            metric_name: The metric name.
            value: The value.

        Returns:
            The SPCAlert | None result.
        """
        chart = self._get_or_create_chart(metric_name)

        # We need at least a few data points before alerting.
        chart.add_value(value)
        if len(chart.values) < 3:
            return None

        alert = self._check_control(chart, value)
        if alert is not None:
            self._alerts.append(alert)
            logger.warning(
                "SPC alert for %s: %s (value=%.4f, UCL=%.4f, LCL=%.4f)",
                metric_name,
                alert.alert_type,
                value,
                chart.ucl,
                chart.lcl,
            )
            # Forward critical SPC alerts to the Andon system for pipeline halting
            try:
                from vetinari.workflow.andon import get_andon_system

                get_andon_system().raise_signal(
                    source=f"spc:{metric_name}",
                    severity="warning",
                    message=f"SPC {alert.alert_type} on {metric_name}: value={value:.4f}",
                )
            except Exception:
                logger.warning("SPC->Andon bridge unavailable")
        return alert

    def get_chart(self, metric_name: str) -> ControlChart | None:
        """Return the control chart for *metric_name*, or ``None``."""
        return self._charts.get(metric_name)

    def get_alerts(self, metric_name: str | None = None) -> list[SPCAlert]:
        """Return alerts, optionally filtered by metric name.

        Args:
            metric_name: If given, return only alerts for this metric.

        Returns:
            All historical SPCAlert objects matching the filter, in
            chronological order. Returns all alerts when metric_name is None.
        """
        if metric_name is None:
            return list(self._alerts)
        return [a for a in self._alerts if a.metric_name == metric_name]

    def get_summary(self) -> dict[str, Any]:
        """Return a summary dict of all monitored metrics.

        Returns:
            Dictionary mapping each metric name to a dict containing count,
            mean, sigma, ucl, lcl, in_control status for the latest value,
            and alert count.
        """
        summary: dict[str, Any] = {}
        for name, chart in self._charts.items():
            summary[name] = {
                "count": len(chart.values),
                "mean": chart.mean,
                "sigma": chart.sigma,
                "ucl": chart.ucl,
                "lcl": chart.lcl,
                "in_control": chart.is_in_control(chart.values[-1]) if chart.values else True,
                "alerts": len([a for a in self._alerts if a.metric_name == name]),
            }
        return summary

    # -- internals ----------------------------------------------------------

    def _get_or_create_chart(self, metric_name: str) -> ControlChart:
        if metric_name not in self._charts:
            self._charts[metric_name] = ControlChart(
                metric_name=metric_name,
                window_size=self._window_size,
                sigma_multiplier=self._sigma_multiplier,
            )
        return self._charts[metric_name]

    @staticmethod
    def _check_control(chart: ControlChart, value: float) -> SPCAlert | None:
        """Check whether *value* is out of control on *chart*."""
        # Basic limit check
        if value > chart.ucl:
            return SPCAlert(
                metric_name=chart.metric_name,
                value=value,
                ucl=chart.ucl,
                lcl=chart.lcl,
                mean=chart.mean,
                sigma=chart.sigma,
                alert_type=SPCAlertType.ABOVE_UCL,
            )
        if value < chart.lcl:
            return SPCAlert(
                metric_name=chart.metric_name,
                value=value,
                ucl=chart.ucl,
                lcl=chart.lcl,
                mean=chart.mean,
                sigma=chart.sigma,
                alert_type=SPCAlertType.BELOW_LCL,
            )

        # Western Electric supplementary rules
        if chart._detect_trend():
            return SPCAlert(
                metric_name=chart.metric_name,
                value=value,
                ucl=chart.ucl,
                lcl=chart.lcl,
                mean=chart.mean,
                sigma=chart.sigma,
                alert_type=SPCAlertType.TREND,
            )
        if chart._detect_run():
            return SPCAlert(
                metric_name=chart.metric_name,
                value=value,
                ucl=chart.ucl,
                lcl=chart.lcl,
                mean=chart.mean,
                sigma=chart.sigma,
                alert_type=SPCAlertType.RUN,
            )
        return None


# =========================================================================
# SPC Monitor Singleton
# =========================================================================

_spc_monitor_instance: SPCMonitor | None = None
_spc_lock = threading.Lock()


def get_spc_monitor() -> SPCMonitor:
    """Return the process-global SPCMonitor instance.

    Returns:
        The singleton SPCMonitor.
    """
    global _spc_monitor_instance
    if _spc_monitor_instance is None:
        with _spc_lock:
            if _spc_monitor_instance is None:
                _spc_monitor_instance = SPCMonitor()
    return _spc_monitor_instance


def reset_spc_monitor() -> None:
    """Reset the singleton (intended for testing)."""
    global _spc_monitor_instance
    _spc_monitor_instance = None
