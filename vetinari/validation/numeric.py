"""Shared fail-closed validation for runtime numeric signals."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from numbers import Real
from typing import Any


class NumericValidationError(ValueError):
    """Raised when a numeric quality or confidence signal is not trustworthy."""


@dataclass(frozen=True)
class NumericSignal:
    """Validated bounded numeric signal with provenance for downstream gates."""

    field_name: str
    value: float
    minimum: float
    maximum: float
    source: str

    def __repr__(self) -> str:
        return (
            "NumericSignal("
            f"field_name={self.field_name!r}, "
            f"value={self.value!r}, "
            f"bounds=({self.minimum!r}, {self.maximum!r}), "
            f"source={self.source!r}"
            ")"
        )

    def as_dict(self) -> dict[str, float | str]:
        return {
            "field_name": self.field_name,
            "value": self.value,
            "minimum": self.minimum,
            "maximum": self.maximum,
            "source": self.source,
        }


def validate_numeric_signal(
    value: Any,
    *,
    field_name: str,
    minimum: float = 0.0,
    maximum: float = 1.0,
    source: str | None = None,
) -> NumericSignal:
    """Validate a bounded numeric runtime signal and fail closed on bad input.

    Returns:
        Numeric signal containing the finite value, bounds, field name, and source.

    Raises:
        NumericValidationError: If field identity, source provenance, numeric type,
            finiteness, bounds, or range membership is invalid.
    """
    field_name = _validate_field_name(field_name)
    source = _validate_source(source, field_name=field_name)
    if isinstance(value, bool) or not isinstance(value, Real):
        raise NumericValidationError(f"{field_name} must be a numeric value")

    numeric_minimum = _validate_bound(minimum, field_name=field_name, bound_name="minimum")
    numeric_maximum = _validate_bound(maximum, field_name=field_name, bound_name="maximum")
    if numeric_minimum > numeric_maximum:
        raise NumericValidationError(f"{field_name} minimum cannot exceed maximum")

    numeric_value = float(value)
    if not math.isfinite(numeric_value):
        raise NumericValidationError(f"{field_name} must be finite")
    if not numeric_minimum <= numeric_value <= numeric_maximum:
        raise NumericValidationError(f"{field_name}={numeric_value} is outside [{numeric_minimum}, {numeric_maximum}]")

    return NumericSignal(
        field_name=field_name,
        value=numeric_value,
        minimum=numeric_minimum,
        maximum=numeric_maximum,
        source=source,
    )


def validate_confidence(value: Any, *, field_name: str = "confidence", source: str | None = None) -> float:
    """Validate a confidence-like score on the closed interval [0, 1]."""
    return validate_numeric_signal(value, field_name=field_name, source=source).value


def require_confidence(value: Any, *, field_name: str = "confidence", source: str | None = None) -> float:
    """Require a finite confidence score in ``[0, 1]`` with provenance."""
    return validate_confidence(value, field_name=field_name, source=source)


def clamp_confidence(value: Any, *, field_name: str = "confidence", source: str | None = None) -> float:
    """Clamp a finite confidence score after proving the source is available."""
    return _clamp_numeric(value, field_name=field_name, minimum=0.0, maximum=1.0, source=source)


def require_progress(value: Any, *, field_name: str = "progress", source: str | None = None) -> float:
    """Require a normalized progress signal in ``[0, 1]``."""
    return validate_numeric_signal(value, field_name=field_name, source=source).value


def require_score(value: Any, *, field_name: str = "score", source: str | None = None) -> float:
    """Require a normalized score signal in ``[0, 1]``."""
    return validate_numeric_signal(value, field_name=field_name, source=source).value


def clamp_score(value: Any, *, field_name: str = "score", source: str | None = None) -> float:
    """Clamp a finite normalized score after provenance validation."""
    return _clamp_numeric(value, field_name=field_name, minimum=0.0, maximum=1.0, source=source)


def require_rubric_value(value: Any, *, field_name: str = "rubric_value", source: str | None = None) -> float:
    """Require a rubric value on Vetinari's closed ``[1, 5]`` review scale."""
    return validate_numeric_signal(value, field_name=field_name, minimum=1.0, maximum=5.0, source=source).value


def require_finite_metric(value: Any, *, field_name: str, source: str | None = None) -> float:
    """Require a finite metric with no default bounds."""
    return validate_numeric_signal(
        value,
        field_name=field_name,
        minimum=-math.inf,
        maximum=math.inf,
        source=source,
    ).value


def require_positive_token_budget(
    value: Any,
    *,
    field_name: str = "token_budget",
    source: str | None = None,
) -> int:
    """Require a positive integer token budget.

    Returns:
        Positive token budget as an integer.

    Raises:
        NumericValidationError: If the value lacks provenance, is not numeric,
            is non-positive, is non-finite, or is not an integer.
    """
    signal = validate_numeric_signal(value, field_name=field_name, minimum=1.0, maximum=math.inf, source=source)
    if not float(signal.value).is_integer():
        raise NumericValidationError(f"{field_name} must be an integer token budget")
    return int(signal.value)


def require_non_negative_integer(
    value: Any,
    *,
    field_name: str,
    source: str | None = None,
) -> int:
    """Require a non-negative integer count with provenance.

    Returns:
        Validated non-negative integer count.

    Raises:
        NumericValidationError: If the value lacks provenance, is not numeric,
            is negative, is non-finite, or is not an integer.
    """
    signal = validate_numeric_signal(value, field_name=field_name, minimum=0.0, maximum=math.inf, source=source)
    if not float(signal.value).is_integer():
        raise NumericValidationError(f"{field_name} must be an integer count")
    return int(signal.value)


def validate_numeric_payload(
    payload: Mapping[str, Any],
    field_bounds: Mapping[str, tuple[float, float]],
    *,
    source: str | None,
    allow_extra_fields: bool = False,
) -> dict[str, NumericSignal]:
    """Validate required numeric fields from a mapping without defaulting missing values.

    Args:
        payload: Mapping that must contain every declared field.
        field_bounds: Required field names mapped to inclusive minimum and maximum bounds.
        source: Provenance label attached to every validated signal.
        allow_extra_fields: When false, reject undeclared payload keys instead of
            silently letting unvalidated numeric signals pass through.

    Returns:
        Mapping from field name to validated numeric signal.

    Raises:
        NumericValidationError: If the payload is not a mapping, no bounds are
            declared, a required field is missing, bounds are malformed, or any
            field value fails numeric validation.
    """
    if not isinstance(payload, Mapping):
        raise NumericValidationError("numeric payload must be a mapping")
    if not field_bounds:
        raise NumericValidationError("numeric payload field_bounds are required")

    declared_fields: set[str] = set()
    validated: dict[str, NumericSignal] = {}
    for field_name, bounds in field_bounds.items():
        field_name = _validate_field_name(field_name)
        declared_fields.add(field_name)
        if field_name not in payload:
            raise NumericValidationError(f"{field_name} is required")
        if not isinstance(bounds, tuple) or len(bounds) != 2:
            raise NumericValidationError(f"{field_name} bounds must be a (minimum, maximum) tuple")
        minimum, maximum = bounds
        validated[field_name] = validate_numeric_signal(
            payload[field_name],
            field_name=field_name,
            minimum=minimum,
            maximum=maximum,
            source=source,
        )
    if not allow_extra_fields:
        extra_fields = sorted(
            str(field_name) for field_name in payload if str(field_name).strip() not in declared_fields
        )
        if extra_fields:
            raise NumericValidationError(f"unexpected numeric payload field(s): {', '.join(extra_fields)}")
    return validated


def _validate_field_name(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise NumericValidationError("numeric signal field_name is required")
    return value.strip()


def _validate_source(value: str | None, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise NumericValidationError(f"{field_name} source is required")
    return value.strip()


def _validate_bound(value: Any, *, field_name: str, bound_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise NumericValidationError(f"{field_name} {bound_name} bound must be numeric")
    numeric_value = float(value)
    if math.isnan(numeric_value):
        raise NumericValidationError(f"{field_name} {bound_name} bound must not be NaN")
    return numeric_value


def _clamp_numeric(
    value: Any,
    *,
    field_name: str,
    minimum: float,
    maximum: float,
    source: str | None,
) -> float:
    field_name = _validate_field_name(field_name)
    _validate_source(source, field_name=field_name)
    if isinstance(value, bool) or not isinstance(value, Real):
        raise NumericValidationError(f"{field_name} must be a numeric value")
    numeric_value = float(value)
    if not math.isfinite(numeric_value):
        raise NumericValidationError(f"{field_name} must be finite")
    if math.isnan(minimum) or math.isnan(maximum):
        raise NumericValidationError(f"{field_name} clamp bounds must not be NaN")
    if minimum > maximum:
        raise NumericValidationError(f"{field_name} minimum cannot exceed maximum")
    return min(max(numeric_value, minimum), maximum)


__all__ = [
    "NumericSignal",
    "NumericValidationError",
    "clamp_confidence",
    "clamp_score",
    "require_confidence",
    "require_finite_metric",
    "require_non_negative_integer",
    "require_positive_token_budget",
    "require_progress",
    "require_rubric_value",
    "require_score",
    "validate_confidence",
    "validate_numeric_payload",
    "validate_numeric_signal",
]
