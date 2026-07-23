"""Scoped cwd/history state store for command-safety decisions."""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from vetinari.security.redaction import redact_text
from vetinari.utils.bounded_collections import BoundedDict
from vetinari.workbench.command_safety.contracts import CommandSafetyError, CommandSafetyReason, CwdHistoryStatus
from vetinari.workbench.spine_consumers import record_asset_written

logger = logging.getLogger(__name__)


SCHEMA_VERSION = "1.0"
DEFAULT_STATE_ROOT = Path("outputs") / "workbench" / "command-safety"
_SCOPE_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_LOCKS: BoundedDict[tuple[str, str, str, str], threading.RLock] = BoundedDict(2_048)
_LOCKS_GUARD = threading.RLock()


class CommandSafetyStateStore:
    """Runtime contract for CommandSafetyStateStore."""

    def __init__(self, root: Path | str | None = None, *, history_limit: int = 50) -> None:
        self._root = Path(root) if root is not None else DEFAULT_STATE_ROOT
        self._history_limit = max(1, int(history_limit))

    def inspect(self, *, project_id: str, run_id: str, session_id: str, surface_id: str) -> CwdHistoryStatus:
        """Execute the inspect operation.

        Returns:
            CwdHistoryStatus value produced by inspect().
        """
        scope = self._scope(project_id, run_id, session_id, surface_id)
        path = self._path(scope)
        if not path.exists():
            return CwdHistoryStatus("recovery_needed", (CommandSafetyReason.CWD_RECOVERY_NEEDED,), state_path=str(path))
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return self._status_from_payload(payload, path)
        except (OSError, json.JSONDecodeError, TypeError, ValueError, CommandSafetyError):
            logger.warning("Handled recoverable failure before fallback.", exc_info=True)
            return CwdHistoryStatus("blocked", (CommandSafetyReason.CORRUPT_CWD_HISTORY,), state_path=str(path))

    def record_cwd(
        self,
        *,
        project_id: str,
        run_id: str,
        session_id: str,
        surface_id: str,
        cwd: str,
        command: str = "",
        verdict: str = "",
    ) -> CwdHistoryStatus:
        """Execute the record cwd operation.

        Returns:
            Outcome produced by record_cwd().
        """
        scope = self._scope(project_id, run_id, session_id, surface_id)
        clean_cwd = _safe_cwd(cwd)
        with _lock_for(scope):
            current = self.inspect(project_id=project_id, run_id=run_id, session_id=session_id, surface_id=surface_id)
            history = list(current.history) if current.status != "blocked" else []
            revision = current.revision + 1 if current.status != "blocked" else 1
            history.append({
                "entry_id": f"cwd-history-{uuid4().hex}",
                "cwd": clean_cwd,
                "command": _redact(command),
                "verdict": verdict,
                "recorded_at_utc": _utc_now(),
                "revision": revision,
            })
            history = history[-self._history_limit :]
            payload = {
                "schema_version": SCHEMA_VERSION,
                "project_id": scope[0],
                "run_id": scope[1],
                "session_id": scope[2],
                "surface_id": scope[3],
                "cwd": clean_cwd,
                "history": history,
                "revision": revision,
                "updated_at_utc": _utc_now(),
            }
            path = self._path(scope)
            self._atomic_write(path, payload)
            return self._status_from_payload(payload, path)

    def cleanup_scope(self, *, project_id: str, run_id: str, session_id: str, surface_id: str) -> bool:
        """Execute the cleanup scope operation.

        Returns:
            bool value produced by cleanup_scope().

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        scope = self._scope(project_id, run_id, session_id, surface_id)
        path = self._path(scope)
        root = self._root.resolve()
        if not path.resolve().is_relative_to(root):
            raise CommandSafetyError("refusing to delete command-safety state outside root")
        if path.exists():
            path.unlink()
            return True
        return False

    @staticmethod
    def _scope(project_id: str, run_id: str, session_id: str, surface_id: str) -> tuple[str, str, str, str]:
        return (
            _scope_key(project_id),
            _scope_key(run_id),
            _scope_key(session_id),
            _scope_key(surface_id),
        )

    def _path(self, scope: tuple[str, str, str, str]) -> Path:
        root = self._root.resolve()
        path = (root / scope[0] / scope[1] / scope[2] / scope[3] / "cwd-history.json").resolve()
        if not path.is_relative_to(root):
            raise CommandSafetyError("command-safety state path escaped root")
        return path

    def _status_from_payload(self, payload: dict[str, Any], path: Path) -> CwdHistoryStatus:
        if payload.get("schema_version") != SCHEMA_VERSION:
            raise CommandSafetyError("cwd-history schema_version mismatch")
        cwd = _safe_cwd(str(payload.get("cwd", "")))
        history = payload.get("history", [])
        if not isinstance(history, list):
            raise CommandSafetyError("cwd-history history must be a list")
        return CwdHistoryStatus(
            "ready",
            (),
            cwd,
            tuple(row for row in history[-self._history_limit :] if isinstance(row, dict)),
            int(payload.get("revision", 0)),
            str(path),
        )

    def _atomic_write(self, path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, sort_keys=True, separators=(",", ":")))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        # spine_consumers invokes get_spine() and absorbs observability failures.
        record_asset_written(
            asset_id="cwd-history-" + payload.get("project_id", "default"),
            kind="tool",
            project_id=str(payload.get("project_id", "default")),
            path=str(path),
            redact_fields=["path"],
        )


def _scope_key(value: object) -> str:
    text = str(value).strip()
    if not text or text in {".", ".."} or not _SCOPE_RE.fullmatch(text):
        raise CommandSafetyError("scope keys must be non-empty safe identifiers")
    return text


def _safe_cwd(value: str) -> str:
    text = str(value).strip().replace("\\", "/")
    if not text or "\x00" in text or ".." in Path(text).parts:
        raise CommandSafetyError("cwd is empty or contains traversal")
    return text


def _redact(command: str) -> str:
    value = str(command)
    for marker in ("secret", "token", "password", "$env:"):
        value = re.sub(marker + r"[^ ]*", marker + "=[REDACTED]", value, flags=re.IGNORECASE)
    return redact_text(value)[:300]


def _lock_for(scope: tuple[str, str, str, str]) -> threading.RLock:
    with _LOCKS_GUARD:
        lock = _LOCKS.get(scope)
        if lock is None:
            lock = threading.RLock()
            _LOCKS[scope] = lock
        return lock


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
