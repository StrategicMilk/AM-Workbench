"""Pure forecasting models used by :mod:`vetinari.analytics.forecasting`."""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, cast

from vetinari.utils.serialization import dataclass_to_dict


@dataclass(frozen=True, slots=True)
class ForecastRequest:
    """Parameters for a forecast call."""

    metric: str
    horizon: int = 5
    method: str = "linear_trend"
    alpha: float = 0.3
    period: int = 7

    def __repr__(self) -> str:
        return f"ForecastRequest(metric={self.metric!r}, horizon={self.horizon!r}, method={self.method!r})"

    def to_dict(self) -> dict[str, Any]:
        """Serialize this forecast request to a plain dictionary.

        Returns:
            Dictionary containing the request fields.
        """
        return cast(dict[str, Any], dataclass_to_dict(self))


@dataclass
class ForecastResult:
    """Output of a forecast operation."""

    metric: str
    forecast_method_used: str
    horizon: int
    predictions: list[float]
    confidence_lo: list[float]
    confidence_hi: list[float]
    trend_slope: float = 0.0
    rmse: float = 0.0
    samples_used: int = 0

    def __repr__(self) -> str:
        return (
            f"ForecastResult(metric={self.metric!r}, forecast_method_used={self.forecast_method_used!r}, "
            f"horizon={self.horizon!r})"
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize this forecast result to a plain dictionary.

        Returns:
            Dictionary containing predictions, confidence bounds, trend slope, and RMSE.
        """
        return cast(dict[str, Any], dataclass_to_dict(self))


def _ols(y: list[float]) -> tuple[float, float]:
    """Return (slope, intercept) of the OLS line through enumerate(y)."""
    n = len(y)
    sx = n * (n - 1) / 2
    sx2 = n * (n - 1) * (2 * n - 1) / 6
    sy = sum(y)
    sxy = sum(i * v for i, v in enumerate(y))
    denom = n * sx2 - sx * sx
    if denom == 0:
        return 0.0, sum(y) / n if y else 0.0
    slope = (n * sxy - sx * sy) / denom
    intercept = (sy - slope * sx) / n
    return slope, intercept


def _rmse(actual: list[float], predicted: list[float]) -> float:
    """Return root mean squared error for aligned values."""
    if not actual:
        return 0.0
    n = min(len(actual), len(predicted))
    return math.sqrt(sum((actual[i] - predicted[i]) ** 2 for i in range(n)) / n)


def _stddev(vals: list[float]) -> float:
    """Delegate to canonical stddev with sample correction."""
    from vetinari.utils.math_helpers import stddev

    return stddev(vals, sample=True)


def _conf_bounds(preds: list[float], std: float, z: float = 1.28) -> tuple[list[float], list[float]]:
    """Return symmetric confidence bounds around predictions."""
    lo = [p - z * std for p in preds]
    hi = [p + z * std for p in preds]
    return lo, hi


def _forecast_sma(history: list[float], horizon: int, window: int = 10) -> ForecastResult:
    """Forecast by repeating the simple moving average."""
    w = history[-window:] if len(history) >= window else history
    pred = sum(w) / len(w) if w else 0.0
    preds = [pred] * horizon
    std = _stddev(history)
    lo, hi = _conf_bounds(preds, std)
    fitted = [pred] * len(history)
    return ForecastResult("", "sma", horizon, preds, lo, hi, rmse=_rmse(history, fitted), samples_used=len(history))


def _forecast_exp_smoothing(history: list[float], horizon: int, alpha: float = 0.3) -> ForecastResult:
    """Forecast with single exponential smoothing."""
    if not history:
        return ForecastResult("", "exp_smoothing", horizon, [0.0] * horizon, [0.0] * horizon, [0.0] * horizon)
    level = history[0]
    fitted: list[float] = []
    for value in history:
        fitted.append(level)
        level = alpha * value + (1 - alpha) * level
    preds = [level] * horizon
    std = _stddev(history)
    lo, hi = _conf_bounds(preds, std)
    return ForecastResult(
        "",
        "exp_smoothing",
        horizon,
        preds,
        lo,
        hi,
        rmse=_rmse(history, fitted),
        samples_used=len(history),
    )


def _forecast_linear_trend(history: list[float], horizon: int) -> ForecastResult:
    """Forecast by ordinary least-squares linear extrapolation."""
    if len(history) < 2:
        value = history[-1] if history else 0.0
        return ForecastResult("", "linear_trend", horizon, [value] * horizon, [0.0] * horizon, [0.0] * horizon)
    slope, intercept = _ols(history)
    n = len(history)
    preds = [intercept + slope * (n + i) for i in range(horizon)]
    fitted = [intercept + slope * i for i in range(n)]
    std = _stddev([a - f for a, f in zip(history, fitted)])
    lo, hi = _conf_bounds(preds, std)
    return ForecastResult(
        "",
        "linear_trend",
        horizon,
        preds,
        lo,
        hi,
        trend_slope=slope,
        rmse=_rmse(history, fitted),
        samples_used=n,
    )


def _forecast_seasonal(history: list[float], horizon: int, period: int = 7) -> ForecastResult:
    """Forecast with additive trend plus seasonal indices."""
    n = len(history)
    if n < period * 2:
        result = _forecast_linear_trend(history, horizon)
        result.forecast_method_used = "seasonal"
        return result

    slope, intercept = _ols(history)
    detrended = [history[i] - (intercept + slope * i) for i in range(n)]
    indices = [0.0] * period
    counts = [0] * period
    for i, value in enumerate(detrended):
        phase = i % period
        indices[phase] += value
        counts[phase] += 1
    indices = [indices[i] / counts[i] if counts[i] else 0.0 for i in range(period)]

    preds = [intercept + slope * (n + h) + indices[(n + h) % period] for h in range(horizon)]
    fitted = [intercept + slope * i + indices[i % period] for i in range(n)]
    std = _stddev([a - f for a, f in zip(history, fitted)])
    lo, hi = _conf_bounds(preds, std)
    return ForecastResult(
        "",
        "seasonal",
        horizon,
        preds,
        lo,
        hi,
        trend_slope=slope,
        rmse=_rmse(history, fitted),
        samples_used=n,
    )


def _forecast_holt_winters(history: list[float], horizon: int, alpha: float = 0.3, beta: float = 0.1) -> ForecastResult:
    """Forecast with Holt-Winters double exponential smoothing."""
    if len(history) < 2:
        return _forecast_linear_trend(history, horizon)

    level = history[0]
    trend = history[1] - history[0]
    fitted: list[float] = []
    for value in history:
        fitted.append(level + trend)
        new_level = alpha * value + (1 - alpha) * (level + trend)
        new_trend = beta * (new_level - level) + (1 - beta) * trend
        level = new_level
        trend = new_trend

    preds = [level + trend * (h + 1) for h in range(horizon)]
    residuals = [history[i] - fitted[i] for i in range(len(history))]
    std = _stddev(residuals) if len(residuals) >= 2 else _stddev(history)
    lo_80 = [p - 1.28 * std * math.sqrt(h + 1) for h, p in enumerate(preds)]
    hi_80 = [p + 1.28 * std * math.sqrt(h + 1) for h, p in enumerate(preds)]
    return ForecastResult(
        "",
        "holt_winters",
        horizon,
        preds,
        lo_80,
        hi_80,
        trend_slope=trend,
        rmse=_rmse(history, fitted),
        samples_used=len(history),
    )


def _forecast_auto(history: list[float], horizon: int, period: int = 7) -> ForecastResult:
    """Auto-select the best forecast method using walk-forward validation."""
    if len(history) < 14:
        result = _forecast_holt_winters(history, horizon)
        result.forecast_method_used = "auto(holt_winters)"
        return result

    val_size = min(7, len(history) // 3)
    train = history[:-val_size]
    actual = history[-val_size:]
    methods: dict[str, Callable[[list[float], int], ForecastResult]] = {
        "holt_winters": lambda h, hz: _forecast_holt_winters(h, hz),
        "linear_trend": lambda h, hz: _forecast_linear_trend(h, hz),
    }
    if len(history) >= period * 2:
        methods["seasonal"] = lambda h, hz: _forecast_seasonal(h, hz, period)

    best_method = "holt_winters"
    best_mape = float("inf")
    for name, fn in methods.items():
        result = fn(train, val_size)
        mape = 0.0
        valid_count = 0
        for actual_value, predicted_value in zip(actual, result.predictions):
            if abs(actual_value) > 1e-10:
                mape += abs((actual_value - predicted_value) / actual_value)
                valid_count += 1
        mape = mape / valid_count if valid_count > 0 else float("inf")
        if mape < best_mape:
            best_mape = mape
            best_method = name

    result = methods[best_method](history, horizon)
    result.forecast_method_used = f"auto({best_method})"
    return result


_METHODS = {
    "sma": lambda h, req: _forecast_sma(h, req.horizon),
    "exp_smoothing": lambda h, req: _forecast_exp_smoothing(h, req.horizon, req.alpha),
    "linear_trend": lambda h, req: _forecast_linear_trend(h, req.horizon),
    "seasonal": lambda h, req: _forecast_seasonal(h, req.horizon, req.period),
    "holt_winters": lambda h, req: _forecast_holt_winters(h, req.horizon),
    "auto": lambda h, req: _forecast_auto(h, req.horizon, req.period),
}
