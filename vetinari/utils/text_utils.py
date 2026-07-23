"""Shared text formatting helpers."""

from __future__ import annotations


def truncate(value: str, limit: int = 200, *, marker: str = "...") -> str:
    """Return ``value`` shortened to ``limit`` characters with ``marker``.

    Args:
        value: Text to shorten.
        limit: Maximum returned length.
        marker: Suffix used when truncation occurs.

    Returns:
        The original value or a shortened value.
    """
    if limit <= 0:
        return ""
    if len(value) <= limit:
        return value
    if len(marker) >= limit:
        return marker[:limit]
    return value[: limit - len(marker)] + marker


__all__ = ["truncate"]
