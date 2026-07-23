"""Database compatibility package."""

from __future__ import annotations

from importlib import import_module
from types import ModuleType

__all__ = ["bootstrap", "connection", "migrations"]


def __getattr__(name: str) -> ModuleType:
    """Lazy-load compatibility submodules advertised by ``__all__``."""
    if name not in __all__:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module = import_module(f"{__name__}.{name}")
    globals()[name] = module
    return module
