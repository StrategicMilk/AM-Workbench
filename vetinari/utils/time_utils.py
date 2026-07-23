"""Shared UTC timestamp helpers."""

from __future__ import annotations

from datetime import datetime, timezone


def now_utc() -> datetime:
    """Return the current timezone-aware UTC datetime."""
    return datetime.now(timezone.utc)


def utc_now_iso(
    value: datetime | None = None,
    *,
    z_suffix: bool = False,
    drop_microseconds: bool = False,
) -> str:
    """Return the current UTC timestamp as an ISO-8601 string.

    Returns:
        ISO-8601 timestamp for ``value`` or the current UTC time.
    """
    value = value or now_utc()
    if drop_microseconds:
        value = value.replace(microsecond=0)
    result = value.isoformat()
    return result.replace("+00:00", "Z") if z_suffix else result


__all__ = ["now_utc", "utc_now_iso"]
