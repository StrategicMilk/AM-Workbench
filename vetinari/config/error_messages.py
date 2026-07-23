"""Error-message catalog loader."""

from __future__ import annotations

import logging
from typing import Any

import yaml

from vetinari.config_paths import resolve_config_path

logger = logging.getLogger(__name__)

_ERROR_MESSAGES: dict[str, Any] | None = None
_DEFAULT_MESSAGES: dict[str, Any] = {
    "default": "An unexpected error occurred. Check server logs for details.",
    "config": "Configuration error.",
}
_CONFIG_PATH = resolve_config_path("error_messages.yaml")


def load_error_messages() -> dict[str, Any]:
    """Load operator-facing error messages.

    Returns:
        Error message mapping.
    """
    global _ERROR_MESSAGES
    if _ERROR_MESSAGES is None:
        try:
            raw = yaml.safe_load(_CONFIG_PATH.read_text(encoding="utf-8")) or {}
        except FileNotFoundError:
            logger.warning("Error message config not found at %s; using defaults", _CONFIG_PATH)
            raw = {}
        except Exception:
            logger.exception("Error message config at %s could not be loaded; using defaults", _CONFIG_PATH)
            raw = {}
        if not isinstance(raw, dict):
            logger.warning(
                "Error message config at %s has non-mapping root %s; using defaults",
                _CONFIG_PATH,
                type(raw).__name__,
            )
            raw = {}
        _ERROR_MESSAGES = {**_DEFAULT_MESSAGES, **raw}
    return dict(_ERROR_MESSAGES)


def reload_error_messages() -> None:
    """Refresh operator-facing error messages."""
    global _ERROR_MESSAGES
    _ERROR_MESSAGES = None


__all__ = ["load_error_messages", "reload_error_messages"]
