"""Agent-facing in-process tool registrations.

Registration is lazy: callers must invoke ``register_all()`` (or
``get_tool_registry()`` followed by ``register_all(target)``) during application
bootstrap or test setup.  Importing this package alone has no side effect on
the global tool registry, satisfying the
``registers_module_level_state: false`` declaration in SHARD-01.
"""

from __future__ import annotations

from vetinari.tool_interface import ToolRegistry, get_tool_registry

from .scraping_tool import register as register_scraping_tool


def register_all(registry: ToolRegistry | None = None) -> ToolRegistry:
    """Register all built-in agent tools into a registry.

    Args:
        registry: Optional explicit registry; defaults to the process-wide
            singleton from ``get_tool_registry()``.

    Returns:
        The registry that received the tool registrations.
    """
    target = registry if registry is not None else get_tool_registry()
    register_scraping_tool(target)
    return target


__all__ = ["register_all", "register_scraping_tool"]
