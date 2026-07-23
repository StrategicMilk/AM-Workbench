"""Layered configuration resolution for Vetinari's config-backed defaults pipeline."""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from typing import Any

logger = logging.getLogger(__name__)


ENV_SEPARATOR = "__"
INVALID_KEY_MESSAGE = "YAML key contains '__' or leading/trailing '_'; this collides with the env-var separator"


class LayeredResolver:
    """Resolve configuration keys using env > user > project > baked precedence."""

    def __init__(
        self,
        baked: dict[str, Any],
        project: dict[str, Any] | None = None,
        user: dict[str, Any] | None = None,
        env_prefix: str = "VETINARI_",
    ) -> None:
        self._validate_keys(baked)
        self._validate_keys(project or {})
        self._validate_keys(user or {})
        self.baked = baked
        self.project = project or {}
        self.user = user or {}
        self.env_prefix = env_prefix

    def resolve(self, key: str) -> tuple[Any, str]:
        """Return the resolved value and source for a dot-notation key.

        Returns:
            A tuple containing the resolved value and its source layer name.
        """
        fallback_value, fallback_source = self._resolve_without_env(key)
        env_name = self._env_name(key)
        if env_name in os.environ:
            value = self._coerce_env_value(os.environ[env_name], fallback_value)
            logger.debug("Key %s resolved from %s (value: %r)", key, "env", value)
            return value, "env"
        if fallback_source != "baked":
            logger.debug("Key %s resolved from %s (value: %r)", key, fallback_source, fallback_value)
        return fallback_value, fallback_source

    def resolve_all(self) -> dict[str, tuple[Any, str]]:
        """Resolve every leaf key present in the baked configuration."""
        return {key: self.resolve(key) for key in self._flatten_keys(self.baked)}

    def _resolve_without_env(self, key: str) -> tuple[Any, str]:
        for source, layer in (("user", self.user), ("project", self.project), ("baked", self.baked)):
            found, value = self._lookup(layer, key)
            if found:
                return value, source
        raise KeyError(f"configuration key not found: {key}")

    @staticmethod
    def _lookup(layer: Mapping[str, Any], key: str) -> tuple[bool, Any]:
        current: Any = layer
        for part in key.split("."):
            if not isinstance(current, Mapping) or part not in current:
                return False, None
            current = current[part]
        return True, current

    def _env_name(self, key: str) -> str:
        return f"{self.env_prefix}{key.replace('.', ENV_SEPARATOR).upper()}"

    @staticmethod
    def _coerce_env_value(raw: str, template: Any) -> Any:
        if isinstance(template, bool):
            lowered = raw.strip().lower()
            if lowered in {"1", "true", "yes", "on"}:
                return True
            if lowered in {"0", "false", "no", "off"}:
                return False
            raise ValueError(f"cannot coerce environment value {raw!r} to bool")
        if isinstance(template, int) and not isinstance(template, bool):
            return int(raw)
        if isinstance(template, float):
            return float(raw)
        return raw

    @classmethod
    def _validate_keys(cls, data: Mapping[str, Any], path: str = "") -> None:
        for key, value in data.items():
            if ENV_SEPARATOR in key or key.startswith("_") or key.endswith("_"):
                dotted = f"{path}.{key}" if path else key
                raise ValueError(f"{INVALID_KEY_MESSAGE}: {dotted}")
            if isinstance(value, Mapping):
                cls._validate_keys(value, f"{path}.{key}" if path else key)

    @classmethod
    def _flatten_keys(cls, data: Mapping[str, Any], prefix: str = "") -> list[str]:
        keys: list[str] = []
        for key, value in data.items():
            dotted = f"{prefix}.{key}" if prefix else key
            if isinstance(value, Mapping):
                keys.extend(cls._flatten_keys(value, dotted))
            else:
                keys.append(dotted)
        return keys
