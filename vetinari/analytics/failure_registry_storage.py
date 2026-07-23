"""Storage helpers for the failure registry facade."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

from vetinari.workbench.cost.jsonl_rotator import RotatingJsonlStore
from vetinari.workbench.cost.token_cost_split import PricingConfigError

if TYPE_CHECKING:
    from vetinari.analytics.failure_registry import FailureRegistryEntry, PreventionRule

logger = logging.getLogger(__name__)


class FailureRegistryStorageError(RuntimeError):
    """Raised when failure-registry evidence cannot be trusted."""


def _registry_module() -> Any:
    """Return the compatibility facade module after import-time initialization."""
    import vetinari.analytics.failure_registry as registry_module

    return registry_module


def _rotating_jsonl_store(path: Path, rotation_key: str) -> RotatingJsonlStore:
    """Return a JSONL store using resource-pricing rotation config."""
    try:
        rotation = _registry_module().load_rotation_settings(rotation_key)
    except PricingConfigError:
        logger.warning("JSONL rotation config unavailable for %s; using defaults", rotation_key, exc_info=True)
        return RotatingJsonlStore(path)
    return RotatingJsonlStore(
        path,
        max_bytes=rotation.max_bytes,
        max_lines=rotation.max_lines,
        backup_count=rotation.backup_count,
    )


def _load_jsonl_dicts(path: Path, rotation_key: str, label: str) -> list[dict[str, Any]]:
    """Load active and retained archive rows, preserving malformed-row tolerance."""
    store = _rotating_jsonl_store(path, rotation_key)
    rows: list[dict[str, Any]] = []
    for source_path in (*store.archive_paths(), path):
        if not source_path.exists():
            continue
        try:
            with source_path.open(encoding="utf-8") as f:
                for line_no, line in enumerate(f, 1):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError as exc:
                        raise FailureRegistryStorageError(
                            f"malformed {label} row at line {line_no} in {source_path}"
                        ) from exc
                    if isinstance(row, dict):
                        rows.append(row)
                    else:
                        raise FailureRegistryStorageError(f"non-object {label} row at line {line_no} in {source_path}")
        except OSError as exc:
            raise FailureRegistryStorageError(f"could not read {label}: {exc}") from exc
    return rows


class FailureRegistryStorageMixin:
    """Persistence operations shared by the FailureRegistry facade."""

    @staticmethod
    def _append_entry(entry: FailureRegistryEntry) -> None:
        """Append a single entry as JSONL. Caller must hold self._lock."""
        registry_module = _registry_module()
        path = registry_module._registry_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            _rotating_jsonl_store(path, registry_module._FAILURE_REGISTRY_ROTATION_KEY).append(entry.to_dict())
        except Exception as exc:
            logger.error(
                "Could not write failure entry %s - entry lost: %s",
                entry.failure_id,
                exc,
            )
            return

    @staticmethod
    def _load_all_entries() -> list[dict[str, Any]]:
        """Load all entries from the JSONL file as raw dicts."""
        registry_module = _registry_module()
        path = registry_module._registry_path()
        store = _rotating_jsonl_store(path, registry_module._FAILURE_REGISTRY_ROTATION_KEY)
        paths = (*store.archive_paths(), path)
        if not any(candidate.exists() for candidate in paths):
            return []
        if any(candidate.exists() for candidate in paths[:-1]):
            return _load_jsonl_dicts(path, registry_module._FAILURE_REGISTRY_ROTATION_KEY, "failure registry")

        entries: list[dict[str, Any]] = []
        try:
            with path.open(encoding="utf-8") as f:
                for line_no, line in enumerate(f, 1):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entries.append(json.loads(line))
                    except json.JSONDecodeError as exc:
                        raise FailureRegistryStorageError(f"malformed failure registry row at line {line_no}") from exc
        except OSError as exc:
            raise FailureRegistryStorageError(f"could not read failure registry: {exc}") from exc
        return entries

    @staticmethod
    def _rewrite_entries(entries: list[dict[str, Any]]) -> None:
        """Rewrite all entries to the JSONL file. Caller must hold self._lock."""
        registry_module = _registry_module()
        path = registry_module._registry_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with tmp_path.open("w", encoding="utf-8") as f:
                for entry in entries:
                    f.write(json.dumps(entry, default=str) + "\n")
                f.flush()
                os.fsync(f.fileno())
            tmp_path.replace(path)
            for archive_path in _rotating_jsonl_store(
                path, registry_module._FAILURE_REGISTRY_ROTATION_KEY
            ).archive_paths():
                with contextlib.suppress(OSError):
                    archive_path.unlink()
        except OSError as exc:
            with contextlib.suppress(OSError):
                tmp_path.unlink()
            logger.error("Could not rewrite failure registry - data may be stale: %s", exc)
            raise FailureRegistryStorageError("could not rewrite failure registry") from exc

    @staticmethod
    def _save_rule(rule: PreventionRule) -> None:
        """Append a prevention rule to the rules JSONL file."""
        registry_module = _registry_module()
        path = registry_module._rules_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            _rotating_jsonl_store(path, registry_module._PREVENTION_RULES_ROTATION_KEY).append(rule.to_dict())
        except Exception as exc:
            logger.error(
                "Could not save prevention rule %s - rule lost: %s",
                rule.rule_id,
                exc,
            )
            raise FailureRegistryStorageError(f"could not save prevention rule {rule.rule_id}") from exc

    @staticmethod
    def _load_rules_from_disk() -> list[PreventionRule]:
        """Load all prevention rules from the JSONL file."""
        registry_module = _registry_module()
        path = registry_module._rules_path()
        store = _rotating_jsonl_store(path, registry_module._PREVENTION_RULES_ROTATION_KEY)
        paths = (*store.archive_paths(), path)
        if not any(candidate.exists() for candidate in paths):
            return []
        if any(candidate.exists() for candidate in paths[:-1]):
            archived_rules: list[PreventionRule] = []
            for data in _load_jsonl_dicts(path, registry_module._PREVENTION_RULES_ROTATION_KEY, "prevention rules"):
                try:
                    archived_rules.append(
                        registry_module.PreventionRule(**{
                            k: v for k, v in data.items() if k in registry_module.PreventionRule.__dataclass_fields__
                        })
                    )
                except TypeError as exc:
                    raise FailureRegistryStorageError("malformed prevention rule in rotated registry") from exc
            return archived_rules

        rules: list[PreventionRule] = []
        try:
            with path.open(encoding="utf-8") as f:
                for line_no, line in enumerate(f, 1):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                        rules.append(
                            registry_module.PreventionRule(**{
                                k: v
                                for k, v in data.items()
                                if k in registry_module.PreventionRule.__dataclass_fields__
                            })
                        )
                    except (json.JSONDecodeError, TypeError) as exc:
                        raise FailureRegistryStorageError(f"malformed prevention rule at line {line_no}") from exc
        except OSError as exc:
            raise FailureRegistryStorageError(f"could not read prevention rules: {exc}") from exc
        return rules
