"""Fail-closed JSONL append and rotation helpers for resource ledgers."""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from vetinari.workbench.security_primitives import SECRET_KEY_NAMES


class JsonlRotationError(RuntimeError):
    """Raised when a JSONL ledger cannot be trusted."""


_REDACTED_KEYS = frozenset({"prompt", "raw_prompt", "raw_user_payload", "response", "user_payload"})


@dataclass(frozen=True, slots=True)
class JsonlAppendResult:
    """Result of one append, including whether a pre-append rotation occurred."""

    path: str
    archive_path: str | None
    bytes_written: int


class RotatingJsonlStore:
    """Append JSONL rows and rotate before configured size or line budgets are exceeded."""

    def __init__(
        self,
        path: str | Path,
        *,
        max_bytes: int = 1_048_576,
        max_lines: int = 10_000,
        backup_count: int = 10,
        archive_dir: str | Path | None = None,
    ) -> None:
        if max_bytes < 1:
            raise ValueError("max_bytes must be positive")
        if max_lines < 1:
            raise ValueError("max_lines must be positive")
        if backup_count < 1:
            raise ValueError("backup_count must be positive")
        self.path = Path(path)
        self.max_bytes = max_bytes
        self.max_lines = max_lines
        self.backup_count = backup_count
        self.archive_dir = Path(archive_dir) if archive_dir is not None else self.path.parent
        self._lock = threading.Lock()

    def append(self, row: Mapping[str, Any]) -> JsonlAppendResult:
        """Append one sanitized row, rotating the existing file first if needed.

        Returns:
            The append path, optional archive path, and bytes written.

        Raises:
            JsonlRotationError: if the row is invalid, rotation fails, or the
                append cannot be durably written.
        """
        if not isinstance(row, Mapping):
            raise JsonlRotationError("jsonl row must be a mapping")
        sanitized = _sanitize_mapping(row)
        try:
            line = json.dumps(sanitized, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
            encoded = line.encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise JsonlRotationError(f"jsonl row is not serializable for {self.path}") from exc
        with self._lock:
            archive_path = self._rotate_if_needed(len(encoded))
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                    handle.write(line)
                    handle.flush()
                    os.fsync(handle.fileno())
            except OSError as exc:
                raise JsonlRotationError(f"jsonl append failed for {self.path}") from exc
        return JsonlAppendResult(str(self.path), str(archive_path) if archive_path is not None else None, len(encoded))

    def read_rows(self, *, include_archives: bool = False) -> tuple[dict[str, Any], ...]:
        """Read all rows from the JSONL store.

        Args:
            include_archives: When true, read retained archive files before the
                active ledger.

        Returns:
            Parsed row mappings in file order.

        Raises:
            JsonlRotationError: if the ledger is unreadable, truncated, or
                contains malformed JSON rows.
        """
        paths = [*self.archive_paths(), self.path] if include_archives else [self.path]
        try:
            raw_by_path = [(path, path.read_bytes()) for path in paths if path.exists()]
        except OSError as exc:
            raise JsonlRotationError(f"jsonl unreadable: {self.path}") from exc
        rows: list[dict[str, Any]] = []
        for source_path, raw in raw_by_path:
            if raw and not raw.endswith(b"\n"):
                raise JsonlRotationError(f"jsonl truncated: {source_path}")
            for line_number, line in enumerate(raw.decode("utf-8").splitlines(), start=1):
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise JsonlRotationError(f"jsonl parse failed at line {line_number} in {source_path}") from exc
                if not isinstance(row, dict):
                    raise JsonlRotationError(f"jsonl row {line_number} in {source_path} must be an object")
                rows.append(row)
        return tuple(rows)

    def archive_paths(self) -> tuple[Path, ...]:
        """Return retained archive paths from oldest to newest.

        Returns:
            Archive paths ordered by modification time and name.
        """
        if not self.archive_dir.exists():
            return ()
        archives = sorted(
            (
                path
                for path in self.archive_dir.glob(f"{self.path.stem}.*{self.path.suffix}")
                if path.name != self.path.name
            ),
            key=lambda path: (path.stat().st_mtime_ns, path.name),
        )
        return tuple(archives)

    def _rotate_if_needed(self, incoming_bytes: int) -> Path | None:
        if not self.path.exists():
            return None
        try:
            current_bytes = self.path.stat().st_size
            current_lines = len(self.path.read_bytes().splitlines())
        except OSError as exc:
            raise JsonlRotationError(f"jsonl state unreadable before rotation: {self.path}") from exc
        if current_bytes + incoming_bytes <= self.max_bytes and current_lines + 1 <= self.max_lines:
            return None
        self.archive_dir.mkdir(parents=True, exist_ok=True)
        archive_path = self._next_archive_path()
        try:
            self.path.replace(archive_path)
        except OSError as exc:
            raise JsonlRotationError(f"jsonl rotation failed for {self.path}") from exc
        self._prune_archives()
        return archive_path

    def _next_archive_path(self) -> Path:
        stamp = _utc_stamp()
        archive_path = self.archive_dir / f"{self.path.stem}.{stamp}{self.path.suffix}"
        if not archive_path.exists():
            return archive_path
        for suffix in range(1, 1000):
            candidate = self.archive_dir / f"{self.path.stem}.{stamp}-{suffix}{self.path.suffix}"
            if not candidate.exists():
                return candidate
        raise JsonlRotationError(f"could not allocate archive path for {self.path}")

    def _prune_archives(self) -> None:
        archives = self.archive_paths()
        excess = len(archives) - self.backup_count
        if excess <= 0:
            return
        for archive in archives[:excess]:
            try:
                archive.unlink()
            except OSError as exc:
                raise JsonlRotationError(f"jsonl archive pruning failed for {archive}") from exc


def _sanitize_mapping(row: Mapping[str, Any]) -> dict[str, Any]:
    sanitized: dict[str, Any] = {}
    for key, value in row.items():
        text_key = str(key)
        lowered = text_key.lower()
        if any(secret in lowered for secret in SECRET_KEY_NAMES):
            continue
        if lowered in _REDACTED_KEYS:
            sanitized[text_key] = "[redacted]"
        elif isinstance(value, Mapping):
            sanitized[text_key] = _sanitize_mapping(value)
        elif isinstance(value, (tuple, list)):
            sanitized[text_key] = [_sanitize_value(item) for item in value]
        else:
            sanitized[text_key] = value
    return sanitized


def _sanitize_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return _sanitize_mapping(value)
    if isinstance(value, (list, tuple)):
        return [_sanitize_value(item) for item in value]
    return value


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


__all__ = ["JsonlAppendResult", "JsonlRotationError", "RotatingJsonlStore"]
