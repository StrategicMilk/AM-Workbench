"""Deterministic token-cost accounting backed by resource pricing config."""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_PRICING_PATH = _REPO_ROOT / "config/resource_pricing.yaml"


class PricingConfigError(RuntimeError):
    """Raised when pricing is missing, unreadable, or incomplete."""


@dataclass(frozen=True, slots=True)
class TokenCostSplit:
    """Input/output token cost split for one metered model call."""

    provider: str
    model: str
    input_tokens: int
    output_tokens: int
    input_cost_usd: float
    output_cost_usd: float

    def __repr__(self) -> str:
        return (
            "TokenCostSplit("
            f"provider={self.provider!r}, model={self.model!r}, "
            f"input_tokens={self.input_tokens}, output_tokens={self.output_tokens}, "
            f"total_cost_usd={self.total_cost_usd!r})"
        )

    @property
    def total_cost_usd(self) -> float:
        return round(self.input_cost_usd + self.output_cost_usd, 8)

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "input_cost_usd": self.input_cost_usd,
            "output_cost_usd": self.output_cost_usd,
            "total_cost_usd": self.total_cost_usd,
        }


@dataclass(frozen=True, slots=True)
class JsonlRotationSettings:
    """Rotation limits for a JSONL resource ledger."""

    max_bytes: int
    max_lines: int
    backup_count: int


def calculate_token_cost(
    *,
    provider: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    pricing_path: str | Path = DEFAULT_PRICING_PATH,
) -> TokenCostSplit:
    """Return token cost and fail closed when pricing is absent.

    Returns:
        A split of input, output, and total token cost.

    Raises:
        PricingConfigError: if inputs are invalid or pricing data is missing,
            unreadable, or malformed.
    """
    provider = _require_text(provider, "provider")
    model = _require_text(model, "model")
    input_tokens = _non_negative_int(input_tokens, "input_tokens")
    output_tokens = _non_negative_int(output_tokens, "output_tokens")
    pricing = load_pricing(pricing_path)
    try:
        models = pricing["tokens"]["providers"][provider]["models"]
    except KeyError as exc:
        raise PricingConfigError(f"missing token pricing provider: {provider}") from exc
    model_pricing = models.get(model) or models.get("*")
    if not isinstance(model_pricing, dict):
        raise PricingConfigError(f"missing token pricing model: {provider}/{model}")
    input_rate = _rate(model_pricing, "input_per_1k_usd", f"{provider}/{model}")
    output_rate = _rate(model_pricing, "output_per_1k_usd", f"{provider}/{model}")
    return TokenCostSplit(
        provider=provider,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        input_cost_usd=round((input_tokens / 1000.0) * input_rate, 8),
        output_cost_usd=round((output_tokens / 1000.0) * output_rate, 8),
    )


def load_rotation_settings(
    ledger_name: str,
    *,
    pricing_path: str | Path = DEFAULT_PRICING_PATH,
    default_max_bytes: int = 1_048_576,
    default_max_lines: int = 10_000,
    default_backup_count: int = 10,
) -> JsonlRotationSettings:
    """Load JSONL rotation settings for one resource ledger.

    Args:
        ledger_name: Key under the pricing config ``rotation`` mapping.
        pricing_path: Pricing YAML path.
        default_max_bytes: Fallback byte limit when the ledger is unconfigured.
        default_max_lines: Fallback line limit when the ledger is unconfigured.
        default_backup_count: Fallback archive count when the ledger is
            unconfigured.

    Returns:
        Validated rotation limits.

    Raises:
        PricingConfigError: If configured rotation limits are invalid.
    """
    name = _require_text(ledger_name, "ledger_name")
    pricing = load_pricing(pricing_path)
    rotation = pricing.get("rotation", {})
    if rotation is None:
        rotation = {}
    if not isinstance(rotation, dict):
        raise PricingConfigError("pricing config rotation must be a mapping")
    configured = rotation.get(name, {})
    if configured is None:
        configured = {}
    if not isinstance(configured, dict):
        raise PricingConfigError(f"rotation config for {name} must be a mapping")
    return JsonlRotationSettings(
        max_bytes=_positive_int(configured.get("max_bytes", default_max_bytes), f"rotation.{name}.max_bytes"),
        max_lines=_positive_int(configured.get("max_lines", default_max_lines), f"rotation.{name}.max_lines"),
        backup_count=_positive_int(
            configured.get("backup_count", default_backup_count),
            f"rotation.{name}.backup_count",
        ),
    )


def load_pricing(path: str | Path = DEFAULT_PRICING_PATH) -> dict[str, Any]:
    """Load pricing YAML and fail closed on missing required sections.

    Returns:
        The parsed pricing configuration.

    Raises:
        PricingConfigError: if the file is missing, malformed, or lacks
            required schema sections.
    """
    pricing_path = Path(path)
    try:
        data = yaml.safe_load(pricing_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise PricingConfigError(f"unable to read pricing config: {pricing_path}") from exc
    except yaml.YAMLError as exc:
        raise PricingConfigError(f"invalid pricing YAML: {pricing_path}") from exc
    if not isinstance(data, dict):
        raise PricingConfigError(f"pricing config must be a mapping: {pricing_path}")
    if data.get("schema_version") != "1.0":
        raise PricingConfigError("pricing config schema_version must be 1.0")
    if data.get("currency") != "USD":
        raise PricingConfigError("pricing config currency must be USD")
    if not isinstance(data.get("tokens"), dict) or not isinstance(data.get("gpu_hour_rates"), dict):
        raise PricingConfigError("pricing config missing tokens or gpu_hour_rates")
    return data


def _rate(mapping: dict[str, Any], key: str, label: str) -> float:
    try:
        value = float(mapping[key])
    except (KeyError, TypeError, ValueError) as exc:
        raise PricingConfigError(f"missing {key} for {label}") from exc
    if not math.isfinite(value) or value < 0:
        raise PricingConfigError(f"{key} for {label} must be finite and non-negative")
    return value


def _require_text(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PricingConfigError(f"{field_name} must be non-empty")
    return value.strip()


def _non_negative_int(value: int, field_name: str) -> int:
    if not isinstance(value, int) or value < 0:
        raise PricingConfigError(f"{field_name} must be a non-negative integer")
    return value


def _positive_int(value: Any, field_name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise PricingConfigError(f"{field_name} must be a positive integer")
    return value


__all__ = [
    "DEFAULT_PRICING_PATH",
    "JsonlRotationSettings",
    "PricingConfigError",
    "TokenCostSplit",
    "calculate_token_cost",
    "load_pricing",
    "load_rotation_settings",
]
