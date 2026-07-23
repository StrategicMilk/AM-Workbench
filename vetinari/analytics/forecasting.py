"""Forecasting & Capacity Planning — vetinari.analytics.forecasting  (Phase 5).

Provides lightweight, dependency-free time-series forecasting suitable for
short-horizon capacity planning:

    SimpleMovingAverage (SMA)   — mean of the last N points.
    ExponentialSmoothing (ES)   — Holt single exponential smoothing.
    LinearTrend                 — ordinary least-squares linear extrapolation.
    SeasonalDecomposition       — additive trend + weekly seasonality.

All methods operate on plain Python lists / deques.

Usage
-----
    from vetinari.analytics.forecasting import get_forecaster, ForecastRequest

    fc = get_forecaster()
    fc.ingest("adapter.latency", 120.0)   # call repeatedly as data arrives
    # ...

    result = fc.forecast(ForecastRequest(
        metric="adapter.latency",
        horizon=5,
        method="linear_trend",
    ))
    logger.debug(result.predictions)   # list of 5 forecasted values
    logger.debug(result.trend_slope)
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Any

from vetinari.analytics import forecasting_methods as _forecasting_methods
from vetinari.analytics.forecasting_methods import (
    _METHODS,
    ForecastRequest,
    ForecastResult,
)
from vetinari.exceptions import ConfigurationError

logger = logging.getLogger(__name__)

_forecast_auto = _forecasting_methods._forecast_auto
_forecast_exp_smoothing = _forecasting_methods._forecast_exp_smoothing
_forecast_holt_winters = _forecasting_methods._forecast_holt_winters
_forecast_linear_trend = _forecasting_methods._forecast_linear_trend
_forecast_seasonal = _forecasting_methods._forecast_seasonal
_forecast_sma = _forecasting_methods._forecast_sma
_ols = _forecasting_methods._ols
_rmse = _forecasting_methods._rmse
_stddev = _forecasting_methods._stddev


# ---------------------------------------------------------------------------
# Forecaster
# ---------------------------------------------------------------------------


class Forecaster:
    """Manages time-series history and produces forecasts.

    Singleton — use ``get_forecaster()``.
    """

    _instance: Forecaster | None = None
    _class_lock = threading.Lock()
    MAX_HISTORY = 1_000

    def __new__(cls) -> Forecaster:
        if cls._instance is None:
            with cls._class_lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._setup()
        return cls._instance

    def _setup(self) -> None:
        self._lock = threading.RLock()
        self._history: dict[str, deque[float]] = {}

    # ------------------------------------------------------------------
    # Ingestion
    # ------------------------------------------------------------------

    # How often (in observations) to run the automatic trend check
    _ALERT_CHECK_INTERVAL: int = 100
    # Minimum absolute trend slope to trigger an alert
    _ALERT_SLOPE_THRESHOLD: float = 0.05

    def ingest(self, metric: str, value: float) -> None:
        """Append a new observation for a metric.

        Every ``_ALERT_CHECK_INTERVAL`` observations, runs a quick linear
        forecast and emits an alert via AlertEngine if the trend slope
        exceeds the threshold (rising latency, rising cost, etc.).

        Args:
            metric: The metric.
            value: The value.
        """
        with self._lock:
            q = self._history.setdefault(metric, deque(maxlen=self.MAX_HISTORY))
            q.append(value)

        # Periodic trend check
        if len(q) > 0 and len(q) % self._ALERT_CHECK_INTERVAL == 0:
            self._check_trend_and_alert(metric)

    def ingest_many(self, metric: str, values: list[float]) -> None:
        """Ingest many.

        Args:
            metric: The metric.
            values: The values.
        """
        for v in values:
            self.ingest(metric, v)

    def _check_trend_and_alert(self, metric: str) -> None:
        """Run a quick linear forecast and emit alert if trend is rising.

        Called automatically every ``_ALERT_CHECK_INTERVAL`` observations.
        Runs in the caller's thread — kept lightweight to avoid blocking.
        """
        try:
            result = self.forecast(
                ForecastRequest(
                    metric=metric,
                    horizon=5,
                    method="linear_trend",
                )
            )
            if result.trend_slope is not None and abs(result.trend_slope) > self._ALERT_SLOPE_THRESHOLD:
                direction = "rising" if result.trend_slope > 0 else "falling"
                logger.warning(
                    "Forecast alert: %s is %s (slope=%.4f) — predicted next 5: %s",
                    metric,
                    direction,
                    result.trend_slope,
                    [round(p, 2) for p in result.predictions[:3]],
                )
                # Emit via EventBus for downstream consumers (dashboard, alerting)
                try:
                    from vetinari.events import Event, get_event_bus

                    get_event_bus().publish(Event(event_type="forecast.trend_alert", timestamp=time.time()))
                except Exception:
                    logger.warning("EventBus publish failed for trend alert on %s — alert not delivered", metric)
        except Exception:
            logger.warning("Trend check failed for %s — trend alerts may be missed", metric)

    # ------------------------------------------------------------------
    # Forecasting
    # ------------------------------------------------------------------

    def forecast(self, request: ForecastRequest) -> ForecastResult:
        """Produce a forecast for *request.metric* using *request.method*.

        Returns a ForecastResult with ``horizon`` predictions.
        If insufficient history exists (< 2 points) the last known value
        is repeated for all steps.

        Returns:
            ForecastResult containing the requested number of predicted values,
            80% confidence bounds, trend slope (linear/Holt-Winters only), and
            in-sample RMSE.

        Raises:
            ValueError: If ``request.method`` is not a recognised method name.
        """
        with self._lock:
            history = list(self._history.get(request.metric, deque()))

        # Validate method name early (before any fallback paths)
        method_fn = _METHODS.get(request.method)
        if method_fn is None:
            raise ConfigurationError(f"Unknown forecasting method '{request.method}'. Valid: {sorted(_METHODS)}")

        if len(history) < 2:
            if len(history) == 1:
                # Single data point — repeat it
                preds = [history[0]] * request.horizon
            else:
                preds = [0.0] * request.horizon
            return ForecastResult(
                metric=request.metric,
                forecast_method_used=request.method,
                horizon=request.horizon,
                predictions=preds,
                confidence_lo=preds,
                confidence_hi=preds,
                samples_used=len(history),
            )

        # Simple moving average fallback for sparse data (2-4 points)
        if len(history) < 5:
            window = len(history)
            avg = sum(history[-window:]) / window
            trend = (history[-1] - history[0]) / max(window - 1, 1)
            preds = [avg + trend * (i + 1) for i in range(request.horizon)]
            # Simple confidence bands (widen with horizon)
            spread = max(abs(max(history) - min(history)), abs(avg) * 0.1)
            lo = [p - spread * (1 + 0.1 * i) for i, p in enumerate(preds)]
            hi = [p + spread * (1 + 0.1 * i) for i, p in enumerate(preds)]
            return ForecastResult(
                metric=request.metric,
                forecast_method_used=request.method,
                horizon=request.horizon,
                predictions=preds,
                confidence_lo=lo,
                confidence_hi=hi,
                samples_used=len(history),
            )

        result = method_fn(history, request)
        result.metric = request.metric
        return result

    # ------------------------------------------------------------------
    # Capacity planning helpers
    # ------------------------------------------------------------------

    def will_exceed(self, metric: str, threshold: float, horizon: int = 10, method: str = "linear_trend") -> bool:
        """Return True if the forecasted trajectory is predicted to exceed.

        *threshold* within *horizon* steps.

        Args:
            metric: The metric.
            threshold: The threshold.
            horizon: The horizon.
            method: The method.

        Returns:
            True if successful, False otherwise.
        """
        req = ForecastRequest(metric=metric, horizon=horizon, method=method)
        result = self.forecast(req)
        return any(p > threshold for p in result.predictions)

    def steps_until_threshold(
        self,
        metric: str,
        threshold: float,
        horizon: int = 50,
        method: str = "linear_trend",
    ) -> int | None:
        """Return the number of steps until the forecast first exceeds *threshold*,.

        or None if it does not within *horizon*.

        Args:
            metric: The metric.
            threshold: The threshold.
            horizon: The horizon.
            method: The method.

        Returns:
            int | None value produced by steps_until_threshold().
        """
        req = ForecastRequest(metric=metric, horizon=horizon, method=method)
        result = self.forecast(req)
        for i, p in enumerate(result.predictions):
            if p > threshold:
                return i + 1
        return None

    def check_sla_breach(
        self,
        metric: str,
        sla_threshold: float,
        horizon_days: int = 7,
    ) -> bool:
        """Check if forecast predicts SLA breach within horizon.

        Emits RetrainingRecommended event when the 80% confidence interval
        lower bound crosses the SLA threshold.

        Args:
            metric: The quality metric to check.
            sla_threshold: Minimum acceptable quality level.
            horizon_days: How far ahead to forecast (days).

        Returns:
            True if breach predicted, False otherwise.
        """
        req = ForecastRequest(metric=metric, horizon=horizon_days, method="auto")
        result = self.forecast(req)

        # Check if 80% CI lower bound crosses SLA threshold
        for day_idx, lo_bound in enumerate(result.confidence_lo):
            if lo_bound < sla_threshold:
                logger.warning(
                    "SLA breach predicted for %s in %d days (predicted_lo=%.3f < threshold=%.3f, method=%s)",
                    metric,
                    day_idx + 1,
                    lo_bound,
                    sla_threshold,
                    result.forecast_method_used,
                )
                # Emit event
                try:
                    from vetinari.events import RetrainingRecommended, get_event_bus

                    ci_width = result.confidence_hi[day_idx] - lo_bound
                    event = RetrainingRecommended(
                        event_type="",
                        timestamp=time.time(),
                        metric=metric,
                        predicted_quality=result.predictions[day_idx],
                        days_until_breach=day_idx + 1,
                        confidence_interval=ci_width,
                        forecast_method_used=result.forecast_method_used,
                    )
                    event_bus = get_event_bus()
                    event_bus.publish(event)
                    event_bus.drain_handlers(timeout=1.0)
                except Exception:  # Broad: event emission is best-effort; never blocks forecasting
                    logger.exception("Failed to emit RetrainingRecommended event")
                return True

        return False

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    def get_history(self, metric: str) -> list[float]:
        """Get history.

        Returns:
            List of results.
        """
        with self._lock:
            return list(self._history.get(metric, []))

    def list_metrics(self) -> list[str]:
        """List metrics.

        Returns:
            Names of all metrics for which at least one observation has been
            ingested, in insertion order.
        """
        with self._lock:
            return list(self._history.keys())

    def get_stats(self) -> dict[str, Any]:
        """Summarise the current forecaster state.

        Returns:
            Dictionary with ``tracked_metrics`` (number of distinct metric
            names ingested) and ``history_sizes`` (mapping of metric name to
            the number of stored observations for that metric).
        """
        with self._lock:
            return {
                "tracked_metrics": len(self._history),
                "history_sizes": {k: len(v) for k, v in self._history.items()},
            }

    def clear(self) -> None:
        """Clear for the current context."""
        with self._lock:
            self._history.clear()


# ---------------------------------------------------------------------------
# Singleton helpers
# ---------------------------------------------------------------------------


def get_forecaster() -> Forecaster:
    """Return the singleton Forecaster instance, creating it if necessary.

    Returns:
        The shared Forecaster singleton used for all time-series forecasting.
    """
    return Forecaster()


def reset_forecaster() -> None:
    """Reset forecaster."""
    with Forecaster._class_lock:
        Forecaster._instance = None
