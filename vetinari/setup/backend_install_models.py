"""Backend install plan models."""

from __future__ import annotations

import os
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from vetinari.privacy import privacy_receipt
from vetinari.types import ModelProvider


@dataclass(frozen=True, slots=True)
class BackendInstallPlan:
    """Concrete dependency plan for one backend install target."""

    provider: ModelProvider
    project_root: Path
    python_executable: str
    extras: tuple[str, ...] = field(default_factory=tuple)
    pip_command: tuple[str, ...] = field(default_factory=tuple)
    verification_modules: tuple[str, ...] = field(default_factory=tuple)
    endpoint_env_vars: tuple[str, ...] = field(default_factory=tuple)
    environment_key: str = "default"
    shared_environment_safe: bool = True
    isolation_reasons: tuple[str, ...] = field(default_factory=tuple)
    system_commands: tuple[str, ...] = field(default_factory=tuple)
    notes: tuple[str, ...] = field(default_factory=tuple)
    hardware_supported: bool = True
    priority: int = 100
    skip_reasons: tuple[str, ...] = field(default_factory=tuple)

    def command_text(self) -> str:
        """Return the pip command as shell-display text.

        Returns:
            Shell-display command text.
        """
        if os.name == "nt":
            return subprocess.list2cmdline(self.pip_command)
        return shlex.join(self.pip_command)

    def privacy_receipt(self) -> dict[str, object]:
        """Return operational privacy metadata for exposing this install plan."""
        return privacy_receipt(
            privacy_class="operational",
            source=f"backend_installer:{self.provider.value}",
            retention_days=30,
            redaction_applied=True,
        )

    def __repr__(self) -> str:
        """Show the provider, environment, and installation safety posture."""
        return (
            "BackendInstallPlan("
            f"provider={self.provider.value!r}, "
            f"environment_key={self.environment_key!r}, "
            f"shared_environment_safe={self.shared_environment_safe!r}, "
            f"hardware_supported={self.hardware_supported!r})"
        )


__all__ = ["BackendInstallPlan"]
