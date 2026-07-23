"""Static adapter catalog helpers."""

from __future__ import annotations


def get_static_fallback_catalog() -> list[str]:
    """Return static fallback model ids.

    Returns:
        Fallback model id list.
    """
    return ["local-small", "local-medium"]


__all__ = ["get_static_fallback_catalog"]
