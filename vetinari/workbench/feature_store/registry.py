"""Append-only feature definition registry."""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from pathlib import Path
from typing import Any

from vetinari.constants import OUTPUTS_DIR
from vetinari.workbench.feature_store.definitions import (
    Definition,
    definition_from_payload,
    definition_to_payload,
    definition_type,
)
from vetinari.workbench.spine_consumers import record_asset_written

logger = logging.getLogger(__name__)


_DEFAULT_REGISTRY_DIR = OUTPUTS_DIR / "workbench" / "spine" / "feature_store"
_PROJECT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_SCHEMA_VERSION = 1


def _default_registry_dir() -> Path:
    return OUTPUTS_DIR / "workbench" / "spine" / "feature_store"


class FeatureStoreRegistryError(Exception):
    """Raised when feature definition registry state cannot be trusted."""


class FeatureDefinitionRegistry:
    """Instance-owned JSONL registry for feature-store definitions."""

    def __init__(self, base_dir: Path | str | None = None, *, project_id: str = "default") -> None:
        if not project_id or not _PROJECT_ID_RE.fullmatch(project_id):
            raise FeatureStoreRegistryError(f"project_id {project_id!r} fails path-traversal regex")
        root = Path(base_dir if base_dir is not None else _default_registry_dir()).expanduser().resolve()
        project_dir = (root / project_id).resolve()
        if not _is_relative_to(project_dir, root):
            raise FeatureStoreRegistryError(f"project_id {project_id!r} escapes feature-store root")
        self._project_id = project_id
        self._project_dir = project_dir
        self._definitions_path = project_dir / "definitions.jsonl"
        self._write_lock = threading.Lock()
        self._definitions: tuple[Definition, ...] = ()
        self._project_dir.mkdir(parents=True, exist_ok=True)
        self._ensure_header()
        self._definitions = self._load_definitions()

    @property
    def definitions_path(self) -> Path:
        """Return the registry JSONL path."""
        return self._definitions_path

    def list_definitions(self) -> tuple[Definition, ...]:
        """Return definitions in append order."""
        return self._definitions

    def register_definition(self, definition: Definition) -> Definition:
        """Append one definition row under the registry write lock.

        Returns:
            Definition value produced by register_definition().

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        payload = definition_to_payload(definition)
        type_name = definition_type(definition)
        row = {
            "schema_version": _SCHEMA_VERSION,
            "kind": "definition",
            "definition_type": type_name,
            "payload": payload,
        }
        line = json.dumps(row, separators=(",", ":"), sort_keys=True) + "\n"
        with self._write_lock:
            try:
                with self._definitions_path.open("a", encoding="utf-8", newline="\n") as fh:
                    fh.write(line)
                    fh.flush()
                    os.fsync(fh.fileno())
                # spine_consumers invokes get_spine() and absorbs observability failures.
                record_asset_written(
                    asset_id=_definition_asset_id(type_name, payload),
                    kind="tool",
                    project_id=self._project_id,
                    path=str(self._definitions_path),
                    redact_fields=["path"],
                )
            except OSError as exc:
                raise FeatureStoreRegistryError("definition registry append failed") from exc
            self._definitions = (*self._definitions, definition)
        return definition

    def _ensure_header(self) -> None:
        if self._definitions_path.exists():
            return
        header = json.dumps({"kind": "header", "schema_version": _SCHEMA_VERSION}, sort_keys=True) + "\n"
        try:
            with self._definitions_path.open("x", encoding="utf-8", newline="\n") as fh:
                fh.write(header)
                fh.flush()
                os.fsync(fh.fileno())
        except OSError as exc:
            raise FeatureStoreRegistryError("definition registry header write failed") from exc

    def _load_definitions(self) -> tuple[Definition, ...]:
        try:
            raw = self._definitions_path.read_bytes()
        except OSError as exc:
            raise FeatureStoreRegistryError("definition registry unreadable") from exc
        if raw and not raw.endswith(b"\n"):
            raise FeatureStoreRegistryError("definition registry truncated")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise FeatureStoreRegistryError("definition registry decode failed") from exc
        rows: list[Definition] = []
        saw_header = False
        for lineno, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue
            row = _loads(line, lineno)
            if row.get("kind") == "header":
                saw_header = True
                if row.get("schema_version") != _SCHEMA_VERSION:
                    raise FeatureStoreRegistryError("definition registry schema version mismatch")
                continue
            if row.get("schema_version") != _SCHEMA_VERSION or row.get("kind") != "definition":
                raise FeatureStoreRegistryError(f"definition registry invalid row at line {lineno}")
            if "definition_type" not in row or "payload" not in row:
                raise FeatureStoreRegistryError(f"definition registry missing payload at line {lineno}")
            rows.append(definition_from_payload(str(row["definition_type"]), row["payload"]))
        if not saw_header:
            raise FeatureStoreRegistryError("definition registry missing schema header")
        return tuple(rows)


def _loads(line: str, lineno: int) -> dict[str, Any]:
    try:
        value = json.loads(line)
    except json.JSONDecodeError as exc:
        raise FeatureStoreRegistryError(f"definition registry parse failed at line {lineno}") from exc
    if not isinstance(value, dict):
        raise FeatureStoreRegistryError(f"definition registry invalid JSON object at line {lineno}")
    return value


def _definition_asset_id(definition_kind: str, payload: dict[str, Any]) -> str:
    field_by_kind = {
        "entity": "entity_id",
        "feature": "feature_id",
        "transformation": "transformation_id",
        "context_view": "context_view_id",
    }
    field_name = field_by_kind.get(definition_kind)
    value = payload.get(field_name) if field_name is not None else None
    if not isinstance(value, str) or not value.strip():
        raise FeatureStoreRegistryError("definition registry missing stable asset id")
    return f"feature-store:{definition_kind}:{value}"


def _is_relative_to(candidate: Path, root: Path) -> bool:
    try:
        candidate.relative_to(root)
        return True
    except ValueError:
        logger.warning("Handled recoverable failure before fallback.", exc_info=True)
        candidate_text = str(candidate).removeprefix("\\\\?\\").casefold()
        root_text = str(root).removeprefix("\\\\?\\").casefold()
        return os.path.commonpath([candidate_text, root_text]) == root_text


__all__ = ["FeatureDefinitionRegistry", "FeatureStoreRegistryError"]
