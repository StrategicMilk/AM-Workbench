"""Practice configuration compatibility loader."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from vetinari.agents.practices import load_practices as _load_agent_practices

_PRACTICES_CACHE: dict[str, Any] | None = None


def load_practices(config_path: str | Path | None = None) -> dict[str, Any]:
    """Load practice metadata.

    Args:
        config_path: Optional practice config path.

    Returns:
        Practice metadata mapping.
    """
    global _PRACTICES_CACHE
    if config_path is not None:
        return _load_agent_practices(config_path)
    if _PRACTICES_CACHE is None:
        _PRACTICES_CACHE = _load_agent_practices()
    return dict(_PRACTICES_CACHE)


def reset_practices_cache() -> None:
    """Clear the process-local practice cache."""
    global _PRACTICES_CACHE
    _PRACTICES_CACHE = None


__all__ = ["load_practices", "reset_practices_cache"]
