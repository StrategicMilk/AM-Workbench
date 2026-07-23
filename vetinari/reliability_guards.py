"""Fail-closed enforcement primitives for reliability subsystems."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


def coerce_aware_datetime(value: datetime | str, *, label: str = "datetime") -> datetime:
    """Return a timezone-aware datetime or fail closed for ambiguous values.

    Returns:
        Parsed timezone-aware datetime.

    Raises:
        TypeError: If ``value`` is not a datetime or ISO-8601 string.
        ValueError: If the datetime is naive.
    """
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed
    if not isinstance(value, datetime):
        raise TypeError(f"{label} must be datetime or ISO-8601 string")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"naive datetime rejected in {label}; use timezone.utc")
    return value


def enforce_closed(result: Any, *, label: str, allow_none: bool = False) -> Any:
    """Return a guard result, raising when the guard produced no decision.

    Returns:
        Original guard result.

    Raises:
        RuntimeError: If ``result`` is None and ``allow_none`` is false.
    """
    if result is None and not allow_none:
        raise RuntimeError(f"guard {label!r} returned None - failing closed")
    return result


def require_not_none(value: T | None, *, label: str) -> T:
    """Return ``value`` or raise when a required guard dependency is absent.

    Returns:
        Non-None value.

    Raises:
        RuntimeError: If ``value`` is None.
    """
    if value is None:
        raise RuntimeError(f"required value {label!r} was None - failing closed")
    return value


__all__ = ["coerce_aware_datetime", "enforce_closed", "require_not_none"]
