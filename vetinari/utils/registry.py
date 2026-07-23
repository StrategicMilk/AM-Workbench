"""Base registry pattern — shared foundation for all registry singletons.

Consolidates the register/get/list/unregister pattern duplicated across
9+ registry classes (AdapterRegistry, ToolRegistry, SkillRegistry, etc.).
Concrete registries inherit from ``BaseRegistry`` and add domain-specific
query methods.
"""

from __future__ import annotations

import logging
import threading
from typing import Generic, TypeVar

logger = logging.getLogger(__name__)


K = TypeVar("K")
V = TypeVar("V")


class BaseRegistry(Generic[K, V]):
    """Thread-safe generic registry with register/get/list/unregister.

    Subclasses get a ready-made ``_items`` dict protected by ``_lock``
    and standard CRUD operations.  Domain-specific query methods (e.g.
    ``list_tools_for_mode``, ``get_constraints_for_agent``) belong on
    the subclass.

    Example::

        class ToolRegistry(BaseRegistry[str, Tool]):
            def list_for_mode(self, mode: ExecutionMode) -> list[Tool]:
                return [t for t in self.list_all() if mode in t.allowed_modes]
    """

    def __init__(self) -> None:
        self._items: dict[K, V] = {}
        self._lock = threading.Lock()

    def register(self, key: K, item: V) -> None:
        """Add or replace an item in the registry.

        Args:
            key: Unique key for the item.
            item: The item to store.
        """
        with self._lock:
            self._items[key] = item

    def get(self, key: K) -> V | None:
        """Look up an item by key, returning ``None`` if not found.

        Args:
            key: The key to look up.

        Returns:
            The registered item, or ``None``.
        """
        with self._lock:
            return self._items.get(key)

    def unregister(self, key: K) -> V | None:
        """Remove and return an item by key.

        Args:
            key: The key to remove.

        Returns:
            The removed item, or ``None`` if not found.
        """
        with self._lock:
            return self._items.pop(key, None)

    def list_all(self) -> list[V]:
        """Return a snapshot list of all registered items.

        Returns:
            List of all items currently in the registry.
        """
        with self._lock:
            return list(self._items.values())

    def list_keys(self) -> list[K]:
        """Return a snapshot list of all registered keys.

        Returns:
            List of all keys currently in the registry.
        """
        with self._lock:
            return list(self._items.keys())

    def clear(self) -> None:
        """Remove all items from the registry."""
        with self._lock:
            self._items.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)

    def __contains__(self, key: K) -> bool:
        with self._lock:
            return key in self._items

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__}({len(self)} items)>"
