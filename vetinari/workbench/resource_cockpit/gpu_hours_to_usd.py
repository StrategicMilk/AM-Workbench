"""GPU-hour monetization for resource cockpit accounting."""

from __future__ import annotations

import math
from pathlib import Path

from vetinari.workbench.cost.token_cost_split import DEFAULT_PRICING_PATH, PricingConfigError, load_pricing


def gpu_hours_to_usd(
    *,
    gpu_model: str,
    gpu_hours: float,
    pricing_path: str | Path = DEFAULT_PRICING_PATH,
) -> float:
    """Convert GPU-hours to USD using explicit pricing config.

    Returns:
        The rounded USD cost for the supplied GPU-hours.

    Raises:
        PricingConfigError: if the GPU model, hours, or pricing data are
            invalid.
    """
    if not isinstance(gpu_model, str) or not gpu_model.strip():
        raise PricingConfigError("gpu_model must be non-empty")
    if not isinstance(gpu_hours, (int, float)) or not math.isfinite(gpu_hours) or gpu_hours < 0:
        raise PricingConfigError("gpu_hours must be finite and non-negative")
    pricing = load_pricing(pricing_path)
    rates = pricing.get("gpu_hour_rates")
    if not isinstance(rates, dict):
        raise PricingConfigError("pricing config missing gpu_hour_rates")
    rate = rates.get(gpu_model) or rates.get("generic_gpu")
    if rate is None:
        raise PricingConfigError(f"missing GPU-hour pricing for {gpu_model}")
    try:
        rate_float = float(rate)
    except (TypeError, ValueError) as exc:
        raise PricingConfigError(f"invalid GPU-hour pricing for {gpu_model}") from exc
    if not math.isfinite(rate_float) or rate_float < 0:
        raise PricingConfigError(f"GPU-hour pricing for {gpu_model} must be finite and non-negative")
    return round(float(gpu_hours) * rate_float, 8)


__all__ = ["gpu_hours_to_usd"]
