"""Configuration migration helpers."""

from __future__ import annotations

import copy
import os
import tempfile
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import Any

import yaml


class MigrationNotImplementedError(Exception):
    """Raised when a requested schema migration step is not registered."""

    def __init__(self, from_version: str, to_version: str) -> None:
        super().__init__(f"no migration step registered from {from_version!r} to {to_version!r}")
        self.from_version = from_version
        self.to_version = to_version


_MIGRATION_STEPS: dict[tuple[str, str], Callable[[dict[str, Any]], dict[str, Any]]] = {}


def migrate_config(
    config: dict[str, Any] | str | Path,
    *,
    from_version: str | None = None,
    to_version: str | None = None,
) -> dict[str, Any]:
    """Migrate a config mapping between schema versions.

    Args:
        config: Source configuration mapping or YAML path.
        from_version: Optional source version.
        to_version: Optional target version.

    Returns:
        Migrated config mapping.

    Raises:
        ValueError: If a YAML path has a non-mapping root or invalid version.
    """
    if isinstance(config, (str, Path)):
        path = Path(config)
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(loaded, dict):
            raise ValueError("config migration root must be a mapping")
        version = loaded.get("version")
        if version is not None and type(version) is not int:
            raise ValueError("config version must be an integer")
        migrated = copy.deepcopy(loaded)
        should_write_back = to_version is not None
    else:
        migrated = copy.deepcopy(config)
        path = None
        should_write_back = False
    if from_version is not None:
        migrated.setdefault("schema_version", from_version)
    if to_version is not None:
        migrated["schema_version"] = to_version
    if from_version is not None and to_version is not None:
        step = _MIGRATION_STEPS.get((from_version, to_version))
        if step is None:
            raise MigrationNotImplementedError(from_version, to_version)
        migrated = step(migrated)
    if should_write_back and path is not None:
        _atomic_write_yaml(path, migrated)
    return migrated


def _atomic_write_yaml(path: Path, data: dict[str, Any]) -> None:
    """Write YAML via temp file and replace so failed writes do not truncate."""
    parent = path.parent
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=parent, delete=False, newline="\n") as handle:
            temp_path = Path(handle.name)
            yaml.safe_dump(data, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        temp_path.replace(path)
    except Exception:
        if temp_path is not None:
            with suppress(OSError):
                temp_path.unlink()
        raise


__all__ = ["MigrationNotImplementedError", "migrate_config"]
