"""LoRA adapter registry management for Vetinari continual learning.

The adapter registry keeps task-specific LoRA adapters separate so continual
training does not overwrite unrelated task capabilities.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from vetinari.security.fail_closed import PathTraversalError, confine_to_root, sanitize_untrusted_text
from vetinari.training.continual_learning_persistence import (
    ADAPTER_REGISTRY_FILENAME,
    _atomic_write_text,
    _default_adapters_dir,
    _move_corrupt_file,
)

logger = logging.getLogger(__name__)


def _confine_adapter_path(root: Path, adapter_path: Path | str) -> Path:
    candidate = Path(adapter_path)
    if candidate.is_absolute():
        try:
            candidate = candidate.resolve().relative_to(root)
        except ValueError as exc:
            raise PathTraversalError("adapter_path must stay under adapters directory") from exc
    return confine_to_root(root, candidate)


class LoRAAdapterManager:
    """Manages per-skill LoRA adapters for task-specific fine-tuning.

    Each task type gets its own LoRA adapter stored at a registered path.
    Adapters are tracked by path, and callers are responsible for loading
    adapter weights from the returned path. The registry is persisted as JSON,
    which prevents different skills from overwriting each other's adapter weights.
    """

    def __init__(self, adapters_dir: Path | None = None) -> None:
        """Initialise the adapter manager.

        Args:
            adapters_dir: Directory where adapter registry is stored. Defaults
                to the configured Vetinari user directory and is created on the
                first save.
        """
        self.adapters_dir = Path(adapters_dir).resolve() if adapters_dir is not None else _default_adapters_dir()
        self._registry: dict[str, Path] = {}
        self._registry_path = self.adapters_dir / ADAPTER_REGISTRY_FILENAME

        if self._registry_path.exists():
            self.load_registry()

    def register_adapter(self, task_type: str, adapter_path: Path) -> None:
        """Register a LoRA adapter path for a task type.

        Args:
            task_type: Non-empty skill or task identifier.
            adapter_path: Path to the LoRA adapter directory or file.

        Raises:
            ValueError: If task_type is empty.
        """
        try:
            safe_task_type = sanitize_untrusted_text(task_type, max_length=256)
        except ValueError as exc:
            raise ValueError("task_type must be a non-empty string") from exc
        if not safe_task_type:
            raise ValueError("task_type must be a non-empty string")
        safe_adapter_path = _confine_adapter_path(self.adapters_dir.parent, adapter_path)
        self._registry[safe_task_type] = safe_adapter_path
        logger.info(
            "LoRAAdapterManager: registered adapter for task_type=%s at %s",
            safe_task_type,
            safe_adapter_path,
        )

    def get_adapter(self, task_type: str) -> Path | None:
        """Return the registered adapter path for a task type.

        Args:
            task_type: The task identifier to look up.

        Returns:
            Path to the adapter, or ``None`` if no adapter is registered for
            this task type.
        """
        return self._registry.get(sanitize_untrusted_text(task_type, max_length=256))

    def list_adapters(self) -> dict[str, Path]:
        """Return all registered adapters.

        Returns:
            Mapping of task type strings to adapter paths.
        """
        return dict(self._registry)

    def remove_adapter(self, task_type: str) -> bool:
        """Remove the adapter registration for a task type.

        Does not delete adapter files from disk; only removes the registry
        entry.

        Args:
            task_type: The task identifier to deregister.

        Returns:
            True if the adapter was registered and removed, otherwise False.
        """
        safe_task_type = sanitize_untrusted_text(task_type, max_length=256)
        if safe_task_type in self._registry:
            del self._registry[safe_task_type]
            logger.info(
                "LoRAAdapterManager: removed registration for task_type=%s",
                safe_task_type,
            )
            return True
        logger.warning(
            "LoRAAdapterManager: remove_adapter called for unknown task_type=%s",
            safe_task_type,
        )
        return False

    def save_registry(self) -> None:
        """Persist the adapter registry to JSON.

        Creates the adapters directory if it does not exist. Paths are stored
        as strings for JSON compatibility.
        """
        serialisable = {key: str(value) for key, value in self._registry.items()}
        _atomic_write_text(Path(self._registry_path), json.dumps(serialisable, indent=2) + "\n")
        logger.info(
            "LoRAAdapterManager: registry saved (%d adapters) to %s",
            len(self._registry),
            self._registry_path,
        )

    def load_registry(self) -> None:
        """Load the adapter registry from JSON.

        No-ops if the registry file does not exist. Stored strings are converted
        back to Path objects.

        Raises:
            OSError: If the registry file exists but cannot be read.
            ValueError: If the registry file is malformed.
        """
        if not self._registry_path.exists():
            logger.info(
                "LoRAAdapterManager: no registry file at %s; starting empty",
                self._registry_path,
            )
            return

        try:
            with Path(self._registry_path).open(encoding="utf-8") as file:
                raw: dict[str, str] = json.load(file)
        except json.JSONDecodeError as exc:
            corrupt_path = _move_corrupt_file(Path(self._registry_path))
            raise ValueError(f"LoRA adapter registry is corrupt; moved aside to {corrupt_path}") from exc

        self._registry = {
            sanitize_untrusted_text(key, max_length=256): _confine_adapter_path(self.adapters_dir.parent, value)
            for key, value in raw.items()
        }
        logger.info(
            "LoRAAdapterManager: loaded %d adapters from %s",
            len(self._registry),
            self._registry_path,
        )

    def get_stats(self) -> dict[str, Any]:
        """Return statistics about registered adapters.

        Computes total on-disk size for adapters whose paths exist.

        Returns:
            Dictionary with adapter count, combined existing size in bytes, and
            sorted task type names.
        """
        total_size = 0
        for adapter_path in self._registry.values():
            path = Path(adapter_path)
            if path.is_dir():
                total_size += sum(file.stat().st_size for file in path.rglob("*") if file.is_file())
            elif path.is_file():
                total_size += path.stat().st_size

        return {
            "count": len(self._registry),
            "total_size_bytes": total_size,
            "task_types": sorted(self._registry.keys()),
        }


__all__ = ["LoRAAdapterManager"]
