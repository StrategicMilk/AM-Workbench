"""Config migration — versioned schema upgrades for YAML config files.

When Vetinari's config schema evolves, this module handles automatic
migration from older versions to the current one.  Each config file
carries a ``config_version`` integer at the top level.  On load, the
migration chain runs all N->N+1 steps needed to reach the current
version.  The original file is backed up before any migration.

Usage::

    from vetinari.utils.config_migration import migrate_config

    data = migrate_config(Path("config/rules.yaml"), CURRENT_VERSION, MIGRATIONS)
"""

from __future__ import annotations

import logging
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)


# Type alias: a migration function transforms config dict in-place
MigrationFn = Callable[[dict[str, Any]], dict[str, Any]]


def migrate_config(
    config_path: Path,
    current_version: int,
    migrations: dict[int, MigrationFn],
) -> dict[str, Any]:
    """Load a YAML config file and migrate it to ``current_version``.

    If the file has no ``config_version`` key, it's treated as version 0.
    Each migration step N->N+1 is applied in order until the config
    reaches ``current_version``.  The original file is backed up to
    ``{path}.bak`` before any migration writes.

    Args:
        config_path: Path to the YAML config file.
        current_version: The target schema version.
        migrations: Mapping of ``from_version -> migration_function``.
            Each function receives the config dict and returns the
            updated dict (may mutate in place).

    Returns:
        The migrated config dict with ``config_version`` set to
        ``current_version``.

    Raises:
        FileNotFoundError: If config_path does not exist.
        ValueError: If a required migration step is missing.
    """
    if not isinstance(current_version, int) or isinstance(current_version, bool) or current_version < 0:
        raise ValueError(f"current_version must be a non-negative integer, got {current_version!r}")

    with config_path.open(encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    if not isinstance(data, dict):
        raise ValueError(f"Config {config_path} must contain a YAML mapping at the document root")

    file_version = data.get("config_version", 0)
    if not isinstance(file_version, int) or isinstance(file_version, bool) or file_version < 0:
        raise ValueError(f"Config {config_path} has invalid config_version {file_version!r}")

    if file_version == current_version:
        return data  # Already up to date

    if file_version > current_version:
        logger.warning(
            "Config %s has version %d which is newer than expected %d — skipping migration",
            config_path,
            file_version,
            current_version,
        )
        return data

    # Back up before migrating
    backup_path = config_path.with_suffix(config_path.suffix + ".bak")
    shutil.copy2(config_path, backup_path)
    logger.info(
        "Backed up %s to %s before migration (v%d -> v%d)",
        config_path,
        backup_path,
        file_version,
        current_version,
    )

    # Apply migrations in order
    version = file_version
    while version < current_version:
        if version not in migrations:
            raise ValueError(f"Missing migration for config version {version} -> {version + 1} in {config_path}")
        logger.info("Migrating %s: v%d -> v%d", config_path, version, version + 1)
        data = migrations[version](data)
        if not isinstance(data, dict):
            raise ValueError(f"Migration {version} -> {version + 1} for {config_path} returned non-mapping data")
        version += 1

    data["config_version"] = current_version

    # Write back the migrated config
    with config_path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, default_flow_style=False, sort_keys=False)

    logger.info("Config %s migrated to version %d", config_path, current_version)
    return data
