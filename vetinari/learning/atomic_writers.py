"""Atomic persistence helpers for learning and program JSON/YAML files.

Each helper writes a sibling ``.<name>.tmp`` file, flushes and fsyncs it, then
atomically replaces the target path. A stale tmp file from a prior process
crash is safely overwritten on the next write; the target is not modified
until ``Path.replace()`` succeeds.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
import threading
import time
from dataclasses import asdict, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

_WINDOWS_TRANSIENT_REPLACE_ERRORS = {5, 32, 33}
_REPLACE_ATTEMPTS = 6
_REPLACE_RETRY_DELAY_SECONDS = 0.05
_PATH_LOCKS: dict[Path, threading.RLock] = {}
_LOCKS_LOCK = threading.Lock()


def _tmp_path(path: Path) -> Path:
    return path.with_name(f".{path.name}.tmp")


def _is_transient_replace_error(exc: OSError) -> bool:
    return getattr(exc, "winerror", None) in _WINDOWS_TRANSIENT_REPLACE_ERRORS


def _replace_with_transient_retry(tmp_path: Path, path: Path) -> None:
    for attempt in range(_REPLACE_ATTEMPTS):
        try:
            tmp_path.replace(path)
            return
        except OSError as exc:
            if not _is_transient_replace_error(exc) or attempt == _REPLACE_ATTEMPTS - 1:
                raise
            time.sleep(_REPLACE_RETRY_DELAY_SECONDS * (attempt + 1))


def _to_jsonable(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value) and not isinstance(value, type):
        return _to_jsonable(asdict(value))
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, tuple):
        return [_to_jsonable(item) for item in value]
    if isinstance(value, list):
        return [_to_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _to_jsonable(item) for key, item in value.items()}
    return value


def _write_text_atomic(path: Path, text: str, *, encoding: str = "utf-8") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        _tmp_path(path).unlink()
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding=encoding,
            newline="\n",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            tmp_path = Path(handle.name)
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        _replace_with_transient_retry(tmp_path, path)
    except Exception:
        logger.exception("atomic write failed for %s", path)
        if tmp_path is not None:
            with contextlib.suppress(OSError):
                tmp_path.unlink()
        raise


def _lock_for_path(path: Path) -> threading.RLock:
    resolved = path.resolve()
    with _LOCKS_LOCK:
        lock = _PATH_LOCKS.get(resolved)
        if lock is None:
            lock = threading.RLock()
            _PATH_LOCKS[resolved] = lock
        return lock


def write_bytes_atomic(path: Path, data: bytes) -> None:
    """Write bytes through a tmp+fsync+replace sequence.

    Args:
        path: Target binary file path.
        data: Bytes to persist.

    Raises:
        OSError: Raised by filesystem operations. The temporary sibling is
            removed before re-raising.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "wb",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            tmp_path = Path(handle.name)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        _replace_with_transient_retry(tmp_path, path)
    except Exception:
        logger.exception("atomic byte write failed for %s", path)
        if tmp_path is not None:
            with contextlib.suppress(OSError):
                tmp_path.unlink()
        raise


def write_json_atomic(path: Path, data: Any, *, indent: int = 2, encoding: str = "utf-8") -> None:
    """Write JSON data through a tmp+fsync+replace sequence.

    Args:
        path: Target JSON file path.
        data: JSON-compatible value to serialize.
        indent: Indentation passed to ``json.dumps``.
        encoding: Text encoding for the temporary file.

    Raises:
        OSError: Raised by filesystem operations such as open, fsync, unlink,
            or replace. The temporary sibling is removed before re-raising.
    """
    text = json.dumps(_to_jsonable(data), ensure_ascii=False, indent=indent) + "\n"
    _write_text_atomic(path, text, encoding=encoding)


def write_jsonl_atomic(path: Path, rows: list[Any], *, encoding: str = "utf-8") -> None:
    """Write JSONL rows through a tmp+fsync+replace sequence.

    Args:
        path: Target JSONL file path.
        rows: JSON-compatible rows to serialize.
        encoding: Text encoding for the temporary file.
    """
    text = "".join(json.dumps(_to_jsonable(row), ensure_ascii=False) + "\n" for row in rows)
    _write_text_atomic(path, text, encoding=encoding)


def append_jsonl_atomic(path: Path, row: Any, *, encoding: str = "utf-8") -> None:
    """Append one JSONL row through the shared atomic replace path.

    The read-modify-write sequence is protected by a per-path in-process lock.
    The final persistence step reuses ``_write_text_atomic`` so callers get the
    same tmp, fsync, and transient Windows replace retry behavior as the other
    atomic writer helpers.

    Args:
        path: Target JSONL path.
        row: JSON-compatible row to append.
        encoding: Text encoding used to read and rewrite the JSONL file.
    """
    path = Path(path)
    with _lock_for_path(path):
        existing = path.read_text(encoding=encoding).splitlines() if path.exists() else []
        rows = [line for line in existing if line.strip()]
        rows.append(json.dumps(_to_jsonable(row), ensure_ascii=False))
        _write_text_atomic(path, "".join(f"{line}\n" for line in rows), encoding=encoding)


def write_yaml_atomic(path: Path, data: Any, *, encoding: str = "utf-8") -> None:
    """Write YAML data through a tmp+fsync+replace sequence.

    A stale ``.<name>.tmp`` sibling left by a prior interrupted write is
    overwritten on the next call; the target path changes only after replace.

    Args:
        path: Target YAML file path.
        data: YAML-compatible value to serialize.
        encoding: Text encoding for the temporary file.

    Raises:
        OSError: Raised by filesystem operations such as open, fsync, unlink,
            or replace. The temporary sibling is removed before re-raising.
    """
    text = yaml.dump(_to_jsonable(data), default_flow_style=False, allow_unicode=True)
    _write_text_atomic(path, text, encoding=encoding)


def migrate_jsonl_schema_version(path: Path, *, current_version: int = 1) -> int:
    """Upgrade JSONL rows missing an older schema_version field.

    This helper rewrites the whole file with the same tmp+fsync+replace
    invariant used by the JSON/YAML helpers. It does not acquire an
    application-level lock; callers that share the same JSONL file with other
    writers must hold their own lock before calling it.

    Args:
        path: JSONL file to migrate.
        current_version: Schema version to stamp onto missing or older rows.

    Returns:
        Number of valid rows upgraded.

    Raises:
        OSError: Raised by the atomic write-back path. The original file stays
            untouched until replace succeeds.
    """
    if not path.exists():
        return 0

    upgraded = 0
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            row = json.loads(stripped)
        except json.JSONDecodeError:
            logger.warning("Skipping malformed JSONL line %s in %s", line_number, path)
            continue
        if not isinstance(row, dict):
            logger.warning("Skipping non-object JSONL line %s in %s", line_number, path)
            continue
        if int(row.get("schema_version", 0) or 0) < current_version:
            row["schema_version"] = current_version
            upgraded += 1
        rows.append(row)

    text = "".join(json.dumps(_to_jsonable(row), ensure_ascii=False) + "\n" for row in rows)
    _write_text_atomic(path, text)
    return upgraded
