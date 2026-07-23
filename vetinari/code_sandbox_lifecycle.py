"""Lifecycle helpers for :mod:`vetinari.code_sandbox`."""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from vetinari.boundary_guards import require_nonempty
from vetinari.learning.atomic_writers import _write_text_atomic

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ApplyChangesResult:
    """Result of applying sandbox file changes."""

    success: bool
    error: str = ""


class CodeSandboxLifecycleMixin:
    """Provide filesystem lifecycle helpers for CodeSandbox."""

    working_dir: Path
    _execution_count: int
    max_execution_time: int
    max_memory_mb: int
    allow_network: bool

    def cleanup(self) -> None:
        """Remove the sandbox working directory and all temporary files."""
        if self.working_dir.exists():
            try:
                shutil.rmtree(self.working_dir)
                logger.info("Cleaned up sandbox: %s", self.working_dir)
            except Exception as e:
                logger.warning("Failed to cleanup sandbox: %s", e)

    def apply_changes(self, changes: dict[str, str]) -> ApplyChangesResult:
        """Apply file changes and report failures instead of swallowing them.

        Returns:
            Value produced for the caller.
        """
        try:
            self._apply_to_fs(changes)
        except (OSError, ValueError) as exc:
            logger.warning("Sandbox apply_changes failed: %s", exc)
            return ApplyChangesResult(False, str(exc))
        return ApplyChangesResult(True, "")

    def _apply_to_fs(self, changes: dict[str, str]) -> None:
        """Apply changes to files inside the sandbox working directory."""
        root = self.working_dir.resolve()
        for rel_path, content in changes.items():
            target = (root / require_nonempty(rel_path, field_name="sandbox_relative_path")).resolve()
            try:
                target.relative_to(root)
            except ValueError as exc:
                raise ValueError(f"sandbox path escapes working directory: {rel_path}") from exc
            target.parent.mkdir(parents=True, exist_ok=True)
            _write_text_atomic(target, content)

    def __exit__(self, *args: object) -> None:
        """Exit context manager by removing the working directory."""
        self.cleanup()

    def get_stats(self) -> dict[str, Any]:
        """Return sandbox usage statistics."""
        return {
            "working_dir": str(self.working_dir),
            "execution_count": self._execution_count,
            "max_execution_time": self.max_execution_time,
            "max_memory_mb": self.max_memory_mb,
            "allow_network": self.allow_network,
        }
