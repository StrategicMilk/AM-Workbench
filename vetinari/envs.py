"""Packaged runtime environment tier resolution helpers."""

from __future__ import annotations

import os
import sys
import threading
from pathlib import Path
from typing import Any

import yaml

from vetinari.constants import PROJECT_ROOT
from vetinari.exceptions import CapabilityNotAvailable

_CONFIG_PATH = PROJECT_ROOT / "config" / "runtime_environments.yaml"
_INSTANCE: EnvTierResolver | None = None
_INSTANCE_LOCK = threading.Lock()


class EnvTierUnavailable(CapabilityNotAvailable):
    """Raised when a capability requires a runtime tier missing on this host."""

    def __init__(self, *, capability: str, tier_name: str, install_command: str) -> None:
        self.capability = capability
        self.tier_name = tier_name
        self.install_command = install_command
        super().__init__(
            f"Capability {capability!r} requires runtime tier {tier_name!r}. Install it with: {install_command}",
            capability=capability,
            tier_name=tier_name,
            install_command=install_command,
        )


class EnvTierResolver:
    """Map capability names to runtime tiers and fail closed when absent."""

    def __init__(self, config_path: Path | str = _CONFIG_PATH, *, environ: dict[str, str] | None = None) -> None:
        self.config_path = Path(config_path)
        self.environ = environ if environ is not None else os.environ
        self._cache_lock = threading.RLock()
        self._config: dict[str, Any] | None = None
        self._capability_map: dict[str, str] | None = None

    def load_config(self) -> dict[str, Any]:
        """Load and validate the runtime tier configuration.

        Returns:
            Resolved config value.

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        if self._config is not None:
            return self._config
        with self._cache_lock:
            if self._config is not None:
                return self._config
            if not self.config_path.exists():
                raise FileNotFoundError(f"runtime tier config missing: {self.config_path}")
            data = yaml.safe_load(self.config_path.read_text(encoding="utf-8")) or {}
            tiers = data.get("tiers")
            if not isinstance(tiers, dict) or not tiers:
                raise ValueError("runtime tier config must define a non-empty tiers mapping")
            self._config = data
            return data

    def capability_tier_map(self) -> dict[str, str]:
        """Return a capability-to-tier map built from ``runtime_environments.yaml``.

        Returns:
            dict[str, str] value produced by capability_tier_map().
        """
        if self._capability_map is not None:
            return self._capability_map
        with self._cache_lock:
            if self._capability_map is not None:
                return self._capability_map
            mapping: dict[str, str] = {}
            for tier_name, tier in self.load_config()["tiers"].items():
                for capability in tier.get("capabilities", []) or []:
                    mapping[str(capability)] = str(tier_name)
            self._capability_map = mapping
            return mapping

    def tier_present(self, tier_name: str) -> bool:
        """Return whether a tier should be treated as installed on this host.

        Returns:
            bool value produced by tier_present().
        """
        tier = self.load_config()["tiers"].get(tier_name)
        if tier is None:
            return False
        if tier_name == "core":
            expected = str(tier.get("present_when", {}).get("value", "core")).lower()
            return self.environ.get(str(tier.get("env_var", "VETINARI_ENV_TIER")), "core").lower() == expected
        raw = self.environ.get(str(tier.get("env_var", f"VETINARI_ENV_{tier_name.upper()}")), "")
        return raw.strip().lower() in {"1", "true", "yes", "on"}

    def install_command(self, tier_name: str) -> str:
        """Return the documented install command for a tier.

        Returns:
            str value produced by install_command().
        """
        tier = self.load_config()["tiers"].get(tier_name, {})
        return str(tier.get("install_command", f'pip install -e ".[{tier_name}]"'))

    def tier_for_capability(self, capability: str) -> str:
        """Return the installed tier for ``capability`` or raise a typed failure.

        Returns:
            str value produced by tier_for_capability().

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        tier_name = self.capability_tier_map().get(capability)
        if tier_name is None:
            raise CapabilityNotAvailable(
                f"Capability {capability!r} is not declared in runtime_environments.yaml; request denied.",
                capability=capability,
            )
        if not self.tier_present(tier_name):
            raise EnvTierUnavailable(
                capability=capability,
                tier_name=tier_name,
                install_command=self.install_command(tier_name),
            )
        return tier_name


def _resolver() -> EnvTierResolver:
    global _INSTANCE
    if _INSTANCE is None:
        with _INSTANCE_LOCK:
            if _INSTANCE is None:
                _INSTANCE = EnvTierResolver()
    return _INSTANCE


def tier_for_capability(capability: str) -> str:
    """Resolve ``capability`` against the process-wide tier resolver."""
    return _resolver().tier_for_capability(capability)


def resolve_tier_interpreter(tier_name: str) -> str:
    """Return the interpreter path for a tier.

    Returns:
        Resolved tier interpreter value.
    """
    if tier_name == "core":
        return sys.executable
    bin_dir = "Scripts" if os.name == "nt" else "bin"
    exe_name = "python.exe" if os.name == "nt" else "python"
    return str(PROJECT_ROOT / ".venv312" / bin_dir / exe_name)


__all__ = [
    "EnvTierResolver",
    "EnvTierUnavailable",
    "resolve_tier_interpreter",
    "tier_for_capability",
]
