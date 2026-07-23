"""Replay identity and cursor decoding for the AM Engine event transport."""

from __future__ import annotations

import json
import logging
import os
import time
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from vetinari.analytics.cost_models import CostEntry
from vetinari.analytics.cost_storage import CostPersistenceConfig, load_persisted_cost_entries
from vetinari.engine.event_schema import EventSchemaError

logger = logging.getLogger(__name__)

_WINDOWS_TRANSIENT_REPLACE_ERRORS = frozenset({5, 32, 33})
_CHECKPOINT_REPLACE_ATTEMPTS = 6
_CHECKPOINT_RETRY_DELAY_SECONDS = 0.05


@dataclass(frozen=True, slots=True)
class StreamItem:
    """One detached transport event and its optional durable cursor."""

    raw: Mapping[str, Any]
    generation: str | None
    cursor: int | None


@dataclass(frozen=True, slots=True)
class EventCheckpoint:
    """Last durably consumed cursor within an engine generation."""

    generation: str
    cursor: int


def next_stream_item(stream: Any) -> StreamItem | None:
    """Detach and validate the next event plus transport cursor metadata.

    Returns:
        A detached stream item, or ``None`` when the stream has ended.

    Raises:
        EventSchemaError: When the event or cursor metadata is malformed.
    """
    raw: Any | None
    try:
        raw = next(stream)
    except StopIteration:
        raw = None
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise EventSchemaError("engine event stream item must be a mapping")
    generation = getattr(stream, "generation", None)
    cursor = getattr(stream, "last_cursor", None)
    if (generation is None) != (cursor is None):
        raise EventSchemaError("engine event stream transport metadata is incomplete")
    if generation is not None and (not isinstance(generation, str) or not generation):
        raise EventSchemaError("engine event stream generation is malformed")
    if cursor is not None and (isinstance(cursor, bool) or not isinstance(cursor, int) or cursor < 0):
        raise EventSchemaError("engine event stream cursor is malformed")
    return StreamItem(raw=dict(raw), generation=generation, cursor=cursor)


def transport_identity(
    raw: Mapping[str, Any],
    *,
    generation: str | None,
    cursor: int | None,
) -> str:
    """Build a stable replay identity from a cursor or legacy payload.

    Returns:
        Stable identity suitable for replay suppression and cost persistence.
    """
    if generation is not None and cursor is not None:
        return f"amw-engine-event:{json.dumps([generation, cursor], separators=(',', ':'))}"
    return f"amw-engine-event:legacy:{event_fingerprint(raw)}"


def load_checkpoint(path: Path) -> EventCheckpoint | None:
    """Load the durable resume cursor, or return ``None`` on first use.

    Returns:
        The validated checkpoint, or ``None`` when no checkpoint exists.

    Raises:
        EventSchemaError: When an existing checkpoint is unreadable or invalid.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        logger.debug("No durable AM Engine event checkpoint found; starting without a resume cursor")
        return None
    except (OSError, json.JSONDecodeError) as exc:
        raise EventSchemaError(f"engine event checkpoint is unreadable: {path}") from exc
    if not isinstance(raw, Mapping) or set(raw) != {"schema_version", "generation", "cursor"}:
        raise EventSchemaError("engine event checkpoint has an invalid schema")
    generation = raw.get("generation")
    cursor = raw.get("cursor")
    if raw.get("schema_version") != 1 or not isinstance(generation, str) or not generation:
        raise EventSchemaError("engine event checkpoint has invalid version or generation")
    if isinstance(cursor, bool) or not isinstance(cursor, int) or cursor < 0:
        raise EventSchemaError("engine event checkpoint has an invalid cursor")
    return EventCheckpoint(generation, cursor)


def load_durable_event_ids(config: CostPersistenceConfig) -> set[str]:
    """Load replay identities already committed to the cost ledger.

    Returns:
        Identities for durable AM Engine cost entries.

    Raises:
        EventSchemaError: When the ledger cannot be read safely.
    """
    entries: deque[CostEntry] = deque()
    try:
        load_persisted_cost_entries(
            entries,
            config.entries_path,
            config.backup_count,
            max_bytes=config.max_bytes,
            retention_days=config.retention_days,
        )
    except (OSError, ValueError) as exc:
        raise EventSchemaError("engine cost ledger is unreadable; refusing replay") from exc
    return {
        entry.task_id
        for entry in entries
        if isinstance(entry.task_id, str) and entry.task_id.startswith("amw-engine-event:")
    }


def write_checkpoint(path: Path, checkpoint: EventCheckpoint) -> None:
    """Atomically persist a durable resume cursor.

    Args:
        path: Final checkpoint path.
        checkpoint: Validated cursor to persist.

    Raises:
        EventSchemaError: When the checkpoint cannot be persisted atomically.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    staged = path.with_suffix(f"{path.suffix}.tmp")
    payload = {
        "schema_version": 1,
        "generation": checkpoint.generation,
        "cursor": checkpoint.cursor,
    }
    try:
        with staged.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        replace_checkpoint(staged, path)
    except OSError as exc:
        raise EventSchemaError(f"engine event checkpoint could not be persisted: {path}") from exc


def replace_checkpoint(staged: Path, path: Path) -> None:
    """Replace a checkpoint, retrying transient Windows file locks.

    Args:
        staged: Fully written temporary checkpoint.
        path: Final checkpoint path.

    Raises:
        OSError: When replacement fails permanently or retries are exhausted.
    """
    for attempt in range(_CHECKPOINT_REPLACE_ATTEMPTS):
        try:
            os.replace(staged, path)
            return
        except OSError as exc:
            transient = getattr(exc, "winerror", None) in _WINDOWS_TRANSIENT_REPLACE_ERRORS
            if not transient or attempt == _CHECKPOINT_REPLACE_ATTEMPTS - 1:
                raise
            time.sleep(_CHECKPOINT_RETRY_DELAY_SECONDS * (attempt + 1))


def event_fingerprint(raw: Mapping[str, Any]) -> str:
    """Return a stable identity for reconnect replay suppression.

    Returns:
        Canonical JSON identity for a legacy event.

    Raises:
        EventSchemaError: When the raw event contains a non-JSON value.
    """
    try:
        return json.dumps(raw, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    except (TypeError, ValueError) as exc:
        raise EventSchemaError("engine event contains a non-JSON value") from exc


def is_log_boundary(count: int) -> bool:
    """Return whether a monotonically increasing counter reached a power of two."""
    return count > 0 and count & (count - 1) == 0


__all__ = [
    "EventCheckpoint",
    "StreamItem",
    "event_fingerprint",
    "is_log_boundary",
    "load_checkpoint",
    "load_durable_event_ids",
    "next_stream_item",
    "replace_checkpoint",
    "transport_identity",
    "write_checkpoint",
]
