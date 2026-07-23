"""Stable exception types for competitive drift contracts."""

from __future__ import annotations

import sys
import types

_REGISTRY_MODULE = "_vetinari_competitive_drift_error_registry"
_registry = sys.modules.get(_REGISTRY_MODULE)
if not isinstance(_registry, types.ModuleType):
    _registry = types.ModuleType(_REGISTRY_MODULE)
    _registry.__file__ = __file__
    sys.modules[_REGISTRY_MODULE] = _registry

_stable_error = getattr(_registry, "CompetitiveDriftError", None)
if isinstance(_stable_error, type) and issubclass(_stable_error, ValueError):
    CompetitiveDriftError = _stable_error
else:

    class CompetitiveDriftError(ValueError):
        """Raised when competitive drift evidence is unavailable or unsafe."""

    _registry.CompetitiveDriftError = CompetitiveDriftError
