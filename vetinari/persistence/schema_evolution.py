"""Schema evolution for versioned persistent formats with auto-migration.

Every persistent format (JSONL, SQLite, JSON config) carries a
``schema_version`` integer.  When data is loaded, this module detects
the version and applies registered migration functions in sequence to
bring the record up to the current version.  Unknown fields are always
preserved; never silently dropped.

Pipeline role: sits below all persistence layers (failure registry,
pipeline state, remediation outcomes) to ensure data compatibility
across Vetinari upgrades.
"""

from __future__ import annotations

import copy
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from vetinari.boundary_guards import assert_dependency_success

logger = logging.getLogger(__name__)


# Type alias for migration functions: (old_record) -> new_record
MigrationFn = Callable[[dict[str, Any]], dict[str, Any]]


@dataclass(frozen=True, slots=True)
class MigrationStep:
    """A single version-to-version migration for a named format.

    Attributes:
        format_name: The persistent format this migration applies to.
        from_version: Source schema version.
        to_version: Target schema version (must be from_version + 1).
        migrate_fn: Callable that transforms a record dict from from_version to to_version.
        description: Human-readable description of what changed.
    """

    format_name: str
    from_version: int
    to_version: int
    migrate_fn: MigrationFn
    description: str = ""

    def __repr__(self) -> str:
        return f"MigrationStep({self.format_name!r}, v{self.from_version}->v{self.to_version})"


class SchemaRegistry:
    """Registry of schema versions and migration paths for all persistent formats.

    Thread-safe.  Each format has a current version and a chain of migration
    steps.  When ``migrate_record()`` is called, it applies the chain from the
    record's version to the current version.

    Side effects:
        - No I/O - purely in-memory registry of migration functions.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # format_name -> list of MigrationStep, ordered by from_version
        self._migrations: dict[str, list[MigrationStep]] = {}
        # format_name -> current (latest) version
        self._current_versions: dict[str, int] = {}

    def register_format(self, format_name: str, current_version: int) -> None:
        """Declare a persistent format and its current schema version.

        Args:
            format_name: Identifier for the format (e.g. ``"failure_registry"``).
            current_version: The latest schema version for this format.
        """
        with self._lock:
            self._current_versions[format_name] = current_version
            if format_name not in self._migrations:
                self._migrations[format_name] = []
        logger.info(
            "Registered format %s at version %d",
            format_name,
            current_version,
        )

    def register_migration(
        self,
        format_name: str,
        from_version: int,
        to_version: int,
        migrate_fn: MigrationFn,
        description: str = "",
    ) -> None:
        """Register a migration step for a format.

        Migrations must be registered in order and each step must increment
        the version by exactly 1.

        Args:
            format_name: The format this migration applies to.
            from_version: Source version (record must be at this version).
            to_version: Target version (must equal ``from_version + 1``).
            migrate_fn: Callable that transforms the record dict.
            description: Human-readable description of the migration.

        Raises:
            ValueError: If ``to_version != from_version + 1``.
        """
        if to_version != from_version + 1:
            msg = f"Migration must increment by 1: {from_version} -> {to_version}"
            raise ValueError(msg)

        step = MigrationStep(
            format_name=format_name,
            from_version=from_version,
            to_version=to_version,
            migrate_fn=migrate_fn,
            description=description,
        )
        with self._lock:
            if format_name not in self._migrations:
                self._migrations[format_name] = []
            self._migrations[format_name].append(step)
            # Keep sorted by from_version
            self._migrations[format_name].sort(key=lambda s: s.from_version)

        logger.debug(
            "Registered migration %s v%d->v%d: %s",
            format_name,
            from_version,
            to_version,
            description or "(no description)",
        )

    def get_current_version(self, format_name: str) -> int:
        """Return the current schema version for a format.

        Args:
            format_name: The format to look up.

        Returns:
            Current version number, or 1 if the format is not registered.
        """
        return self._current_versions.get(format_name, 1)

    def migrate_record(
        self,
        format_name: str,
        record: dict[str, Any],
    ) -> dict[str, Any]:
        """Migrate a record from its current version to the latest version.

        Unknown fields in the record are preserved - they are never dropped.
        If the record is already at the current version, it is returned as-is.
        If no migrations are registered, the record is returned with
        ``schema_version`` set to the current version.

        Args:
            format_name: The format this record belongs to.
            record: The record dict to migrate. Must not be mutated in place.

        Returns:
            A new dict at the current schema version with all fields preserved.

        Raises:
            ValueError: If the record's schema_version is missing a valid
                positive integer shape.
            RuntimeError: If a migration step fails and would otherwise return
                a partially migrated record.
        """
        # Work on a copy to avoid mutating the caller's data
        result = copy.deepcopy(record)
        record_version = result.get("schema_version", 1)
        if isinstance(record_version, bool) or not isinstance(record_version, int) or record_version < 1:
            raise ValueError(f"Invalid schema_version for {format_name}: {record_version!r}")
        current_version = self.get_current_version(format_name)

        if record_version >= current_version:
            # Already at or ahead of current - just ensure version field exists
            result["schema_version"] = max(record_version, current_version)
            return result

        with self._lock:
            steps = list(self._migrations.get(format_name, []))

        if not steps:
            result["schema_version"] = current_version
            return result

        # Apply each migration step in sequence
        for step in steps:
            if step.from_version < record_version:
                continue  # Skip steps below the record's current version
            if step.from_version >= current_version:
                break  # Don't apply steps beyond current

            try:
                result = step.migrate_fn(result)
                actual_version = result.get("schema_version")
                if actual_version is not None and actual_version != step.to_version:
                    assert_dependency_success(False, dependency_id="schema migration version")
                    raise ValueError(
                        f"migration produced stale record: expected v{step.to_version}, got {actual_version!r}"
                    )
                result["schema_version"] = step.to_version
                logger.debug(
                    "Migrated %s record v%d->v%d",
                    format_name,
                    step.from_version,
                    step.to_version,
                )
            except Exception as exc:
                logger.exception(
                    "Migration failed for %s v%d->v%d; refusing partial migration result",
                    format_name,
                    step.from_version,
                    step.to_version,
                )
                raise RuntimeError(
                    f"Migration failed for {format_name} v{step.from_version}->v{step.to_version}"
                ) from exc

        actual_version = result.get("schema_version")
        if actual_version != current_version:
            assert_dependency_success(False, dependency_id="schema migration final version")
            raise ValueError(f"migration produced stale record: expected v{current_version}, got {actual_version!r}")
        return result

    def stamp_record(self, format_name: str, record: dict[str, Any]) -> dict[str, Any]:
        """Add the current schema_version to a new record before persistence.

        Args:
            format_name: The format being written.
            record: The record dict to stamp.

        Returns:
            The same dict with ``schema_version`` set to the current version.
        """
        record["schema_version"] = self.get_current_version(format_name)
        return record

    def get_migration_chain(self, format_name: str) -> list[MigrationStep]:
        """Return all registered migration steps for a format, ordered by version.

        Args:
            format_name: The format to look up.

        Returns:
            List of MigrationStep objects, sorted by from_version.
        """
        with self._lock:
            return list(self._migrations.get(format_name, []))

    def reset(self) -> None:
        """Clear all registrations. For test isolation only."""
        with self._lock:
            self._migrations.clear()
            self._current_versions.clear()


# ── Singleton ────────────────────────────────────────────────────────────────

_registry: SchemaRegistry | None = None
_registry_lock = threading.Lock()


def get_schema_registry() -> SchemaRegistry:
    """Return the process-wide SchemaRegistry singleton.

    Uses double-checked locking so the common read-path never acquires the lock.

    Returns:
        The singleton SchemaRegistry instance.
    """
    global _registry
    if _registry is None:
        with _registry_lock:
            if _registry is None:
                _registry = SchemaRegistry()
                _register_default_formats(_registry)
    return _registry


def reset_schema_registry() -> None:
    """Reset the singleton for test isolation."""
    global _registry
    with _registry_lock:
        if _registry is not None:
            _registry.reset()
        _registry = None


def migrate_record(format_name: str, record: dict[str, Any]) -> dict[str, Any]:
    """Convenience wrapper - migrate a record through the global registry.

    Args:
        format_name: The format this record belongs to.
        record: The record dict to migrate.

    Returns:
        Migrated record at the current schema version.
    """
    return get_schema_registry().migrate_record(format_name, record)


# ── Default format registrations ─────────────────────────────────────────────


def _register_default_formats(registry: SchemaRegistry) -> None:
    """Register all known persistent formats and their current versions.

    Called once when the singleton is first created.  Migration functions
    for each format are registered here.

    Args:
        registry: The SchemaRegistry to populate.
    """
    # Failure registry JSONL
    registry.register_format("failure_registry", current_version=1)

    # Prevention rules JSONL
    registry.register_format("prevention_rules", current_version=1)

    # Remediation outcomes JSONL (new in session 14)
    registry.register_format("remediation_outcomes", current_version=1)

    # Pipeline state JSON (new in session 14)
    registry.register_format("pipeline_state", current_version=1)

    # Decision journal SQLite (schema managed by decision_journal.py itself)
    registry.register_format("decision_journal", current_version=1)

    # Inference profiles JSON config
    registry.register_format("inference_profiles", current_version=1)
