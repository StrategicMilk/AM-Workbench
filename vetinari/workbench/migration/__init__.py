"""Safe Workbench migration import surface."""

from __future__ import annotations

import importlib
import sqlite3

from vetinari.workbench.migration.contracts import (
    MigrationApplyRequest,
    MigrationApplyResult,
    MigrationApplyStatus,
    MigrationBlockReason,
    MigrationConflict,
    MigrationFinding,
    MigrationPlan,
    MigrationRisk,
    MigrationSourceKind,
    MigrationSourceSpec,
    migration_json_safe,
)
from vetinari.workbench.migration.runtime import (
    WorkbenchMigrationError,
    WorkbenchMigrationService,
    get_workbench_migration_service,
    load_migration_source_specs,
)

_MIGRATION_MODULES = ("001_composite_indexes",)


def run_all_migrations(conn: sqlite3.Connection) -> None:
    """Run all Workbench metadata spine migrations in numeric order.

    Args:
        conn: Open SQLite connection for the metadata spine.
    """
    for module_name in _MIGRATION_MODULES:
        module = importlib.import_module(f"{__name__}.{module_name}")
        module.run(conn)


__all__ = [
    "MigrationApplyRequest",
    "MigrationApplyResult",
    "MigrationApplyStatus",
    "MigrationBlockReason",
    "MigrationConflict",
    "MigrationFinding",
    "MigrationPlan",
    "MigrationRisk",
    "MigrationSourceKind",
    "MigrationSourceSpec",
    "WorkbenchMigrationError",
    "WorkbenchMigrationService",
    "get_workbench_migration_service",
    "load_migration_source_specs",
    "migration_json_safe",
    "run_all_migrations",
]
