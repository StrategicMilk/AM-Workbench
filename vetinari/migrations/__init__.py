"""Data migration scripts for evolving Vetinari's storage schemas.

Provides :func:`run_migrations` to initialise or upgrade SQLite databases
used by the plan-tracking, durable-execution, and memory subsystems.
"""

from __future__ import annotations

from vetinari.migrations.runner import run_migrations
from vetinari.migrations.upgrade_subtask_schema_v1_to_v2 import upgrade_payload, upgrade_subtask_record

__all__ = ["run_migrations", "upgrade_payload", "upgrade_subtask_record"]
