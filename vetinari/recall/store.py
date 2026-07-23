"""Fail-closed episodic recall facade.

Delegates read calls to ``vetinari.learning.episodic_recall`` and write calls
to the episode memory singleton. Backend failures are surfaced as typed errors
so callers cannot confuse missing recall with a successful empty history.
"""

from __future__ import annotations

import importlib
import logging
import sys
from types import ModuleType
from typing import Any

logger = logging.getLogger(__name__)


class RecallStoreError(RuntimeError):
    """Raised when the recall backend cannot satisfy a read or write."""


class RecallStore:
    """Unified facade for episodic memory recall and recording."""

    @staticmethod
    def _learning_backend(module_name: str) -> ModuleType:
        """Resolve a learning backend without ignoring stale package state."""
        parent_name, _, child_name = module_name.rpartition(".")
        parent = importlib.import_module(parent_name)
        parent_child = getattr(parent, child_name, None)
        has_sys_entry = module_name in sys.modules
        sys_entry = sys.modules.get(module_name)
        if parent_child is not None and not has_sys_entry:
            raise RecallStoreError(f"{module_name} backend module state is inconsistent")
        if parent_child is not None and sys_entry is not None and parent_child is not sys_entry:
            raise RecallStoreError(f"{module_name} backend module state is inconsistent")

        module = importlib.import_module(module_name)
        refreshed_child = getattr(parent, child_name, None)
        if refreshed_child is not module:
            raise RecallStoreError(f"{module_name} backend module state is inconsistent")
        return module

    def recall(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        """Return up to *limit* past episodes relevant to *query*.

        Args:
            query: Natural-language recall query.
            limit: Maximum number of matching episodes to return.

        Returns:
            Relevant recall rows from the episodic memory backend.

        Raises:
            RecallStoreError: If the underlying recall backend fails.
        """
        try:
            recall_for_planning = self._learning_backend("vetinari.learning.episodic_recall").recall_for_planning

            return recall_for_planning(goal=query, k=limit)
        except Exception as exc:
            logger.warning("RecallStore.recall failed for query=%r", query[:80], exc_info=True)
            raise RecallStoreError("recall backend unavailable") from exc

    def record(self, entry: dict[str, Any]) -> None:
        """Persist one recall entry to the episode memory store.

        Raises:
            TypeError: If *entry* is not a dict.
            RecallStoreError: If the underlying episode store fails.
        """
        if not isinstance(entry, dict):
            raise TypeError(f"RecallStore.record expects a dict, got {type(entry).__name__!r}")
        try:
            get_episode_memory = self._learning_backend("vetinari.learning.episode_memory").get_episode_memory

            get_episode_memory().record(entry)
        except Exception as exc:
            logger.warning(
                "RecallStore.record failed; episode not persisted (task_summary=%r)",
                entry.get("task_summary", "<unknown>"),
                exc_info=True,
            )
            raise RecallStoreError("recall record backend unavailable") from exc
