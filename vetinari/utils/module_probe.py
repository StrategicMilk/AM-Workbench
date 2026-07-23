"""Non-executing optional-module availability probes."""

from __future__ import annotations

import importlib.util
import logging
import sys

logger = logging.getLogger(__name__)


def module_is_available(module_name: str) -> bool:
    """Return whether a module can be discovered without importing it.

    Returns:
        True when the module is already loaded or has an import spec.
    """
    if module_name in sys.modules:
        return sys.modules[module_name] is not None
    try:
        return importlib.util.find_spec(module_name) is not None
    except (ModuleNotFoundError, ValueError) as exc:
        logger.warning("optional module probe failed for %s: %s", module_name, exc)
        return False


__all__ = ["module_is_available"]
