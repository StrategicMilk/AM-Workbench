"""Persistence mixin for the public blackboard facade."""

from __future__ import annotations

import contextlib
import json
import logging
import time
from typing import TYPE_CHECKING, Any

from vetinari.constants import CACHE_TTL_ONE_HOUR

logger = logging.getLogger("vetinari.memory.blackboard")


class BlackboardPersistenceMixin:
    """SQLite persistence and restore behavior for blackboard entries."""

    if TYPE_CHECKING:
        _auto_persist: Any
        _entries: Any
        _lock: Any
        _project_id: Any

    def _persist_if_enabled(self) -> None:
        if self._auto_persist:
            self.persist(self._project_id)

    def persist(self, project_id: str = "global") -> bool:
        """Serialize all entries to the ``blackboard_state`` SQLite table.

        Allows crash-recovery: entries are restored by :meth:`restore` on the
        next process start. Non-fatal: errors are logged and False is returned.

        Args:
            project_id: Project scope key used as the table's partition key.

        Returns:
            True when state was successfully persisted, False on error.
        """
        try:
            from vetinari.database import get_connection

            conn = get_connection()
            with self._lock:
                entries_data = {eid: entry.to_dict() for eid, entry in self._entries.items()}
            state_json = json.dumps(entries_data)
            conn.execute(
                """INSERT OR REPLACE INTO blackboard_state (project_id, state_key, state_json, updated_at)
                   VALUES (?, 'blackboard', ?, datetime('now'))""",
                (project_id, state_json),
            )
            conn.commit()
            logger.debug("[Blackboard] Persisted %d entries for project %s", len(entries_data), project_id)
            return True
        except Exception as exc:
            logger.warning("[Blackboard] persist failed: %s", exc)
            return False

    def restore(self, project_id: str = "global") -> int:
        """Restore entries from the ``blackboard_state`` SQLite table.

        Silently skips unknown states. Returns the number of entries restored.

        Args:
            project_id: Project scope key used to find the saved state.

        Returns:
            Number of entries successfully restored.

        Raises:
            No exceptions are expected; restore failures are logged and return 0.
        """
        from vetinari.memory.blackboard import BlackboardEntry, EntryState

        try:
            from vetinari.database import get_connection

            conn = get_connection()
            row = conn.execute(
                "SELECT state_json FROM blackboard_state WHERE project_id = ? AND state_key = 'blackboard'",
                (project_id,),
            ).fetchone()
            if row is None:
                return 0
            entries_data: dict[str, Any] = json.loads(row[0])
            if not isinstance(entries_data, dict):
                raise ValueError("blackboard persisted state root must be an object")
            restored = 0
            with self._lock:
                for entry_dict in entries_data.values():
                    try:
                        state_val = entry_dict.get("state", "pending")
                        try:
                            state = EntryState(state_val)
                        except ValueError:
                            state = EntryState.PENDING
                        raw_result = entry_dict.get("result")
                        if isinstance(raw_result, str):
                            with contextlib.suppress(ValueError, TypeError):
                                raw_result = json.loads(raw_result)
                        entry = BlackboardEntry(
                            entry_id=entry_dict["entry_id"],
                            content=entry_dict["content"],
                            request_type=entry_dict["request_type"],
                            requested_by=entry_dict["requested_by"],
                            priority=entry_dict.get("priority", 5),
                            state=state,
                            claimed_by=entry_dict.get("claimed_by"),
                            claimed_at=entry_dict.get("claimed_at"),
                            result=raw_result,
                            error=entry_dict.get("error"),
                            created_at=float(entry_dict.get("created_at", time.time())),
                            completed_at=entry_dict.get("completed_at"),
                            ttl_seconds=float(entry_dict.get("ttl_seconds", CACHE_TTL_ONE_HOUR)),
                            metadata=entry_dict.get("metadata") or {},
                            scope=entry_dict.get("scope", "global"),
                        )
                        self._entries[entry.entry_id] = entry
                        restored += 1
                    except Exception as exc:
                        raise ValueError(
                            f"blackboard persisted entry is malformed for project {project_id}: {exc}"
                        ) from exc
            logger.info("[Blackboard] Restored %d entries for project %s", restored, project_id)
            return restored
        except (OSError, json.JSONDecodeError, ValueError, TypeError) as exc:
            logger.error("[Blackboard] restore failed closed for project %s: %s", project_id, exc)
            raise RuntimeError(f"Blackboard restore failed for project {project_id}: corrupt persisted state") from exc
