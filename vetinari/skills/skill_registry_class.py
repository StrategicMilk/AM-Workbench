"""Disk-aware SkillRegistry compatibility facade.

``SkillRegistry`` merges disk-based JSON skill manifests with the programmatic
``SKILL_REGISTRY`` entries defined in ``vetinari.skills.skill_definitions``.
The behavior lives in focused mixins so this facade remains small while the
public import path stays stable.

Also provides ``get_registry()`` as the global singleton accessor.
"""

from __future__ import annotations

import threading
from typing import Any

from vetinari.skills.skill_registry_catalog import SkillRegistryCatalogMixin
from vetinari.skills.skill_registry_core import SkillRegistryCoreMixin
from vetinari.skills.skill_registry_governance import SkillRegistryGovernanceMixin


class SkillRegistry(
    SkillRegistryCoreMixin,
    SkillRegistryCatalogMixin,
    SkillRegistryGovernanceMixin,
):
    """Central registry for Vetinari skills.

    Merges disk-based JSON skill manifests with the programmatic registry.
    Disk entries take precedence over programmatic ones when both define the
    same skill id.
    """

    def __init__(self, load_on_init: bool = True) -> None:
        """Initialise the registry, optionally loading from disk immediately.

        Args:
            load_on_init: When True, call ``load()`` during initialisation.
        """
        self._registry: dict[str, Any] = {}
        self._manifests: dict[str, dict[str, Any]] = {}
        self._agent_map: dict[str, Any] = {}
        self._contexts: dict[str, dict[str, Any]] = {}
        self.is_loaded = False
        self._loading_levels: dict[str, int] = {}

        if load_on_init:
            self.load()


_global_registry: SkillRegistry | None = None
_global_registry_lock = threading.Lock()


def get_registry() -> SkillRegistry:
    """Return the global ``SkillRegistry`` singleton.

    The registry is lazily created and loaded on first access.

    Returns:
        The shared ``SkillRegistry`` instance.
    """
    global _global_registry
    if _global_registry is None:
        with _global_registry_lock:
            if _global_registry is None:
                _global_registry = SkillRegistry()
    return _global_registry
