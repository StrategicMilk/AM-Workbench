"""Persistence helpers for server-sent event replay."""

from __future__ import annotations

import json
import logging
from typing import Any

from vetinari.boundary_guards import account_evidence_drop, require_nonempty

logger = logging.getLogger(__name__)


def _next_sequence_num(project_id: str) -> int:
    from vetinari.database import get_connection

    conn = get_connection()
    row = conn.execute(
        "SELECT COALESCE(MAX(sequence_num), 0) AS max_sequence FROM sse_event_log WHERE project_id = ?",
        (project_id,),
    ).fetchone()
    return int(row["max_sequence"] if row is not None else 0) + 1


def _persist_sse_event(
    project_id: str,
    event_type: str,
    payload: dict[str, Any],
    *,
    sequence_num: int | str | None = None,
) -> None:
    """Write an SSE event to the ``sse_event_log`` table (best-effort).

    Called immediately after an event is delivered live so the event is also
    persisted for later replay.

    Args:
        project_id: Project identifier for the event stream.
        event_type: SSE event type name.
        payload: Event payload to persist.
        sequence_num: Optional preallocated sequence number.
    available for replay.  Failures are logged at WARNING and swallowed so live
    delivery is never interrupted.

    Args:
        project_id: The project the event belongs to.
        event_type: The SSE event type string (e.g. ``"task_started"``).
        payload: The event data dict to store as JSON.
    """
    conn = None
    try:
        sequence = (
            _next_sequence_num(project_id)
            if sequence_num is None
            else int(require_nonempty(str(sequence_num), field_name="sequence_num"))
        )
        from vetinari.database import get_connection

        conn = get_connection()
        conn.execute(
            "INSERT INTO sse_event_log (project_id, event_type, payload_json, sequence_num) VALUES (?, ?, ?, ?)",
            (project_id, event_type, json.dumps(payload, ensure_ascii=False), sequence),
        )
        conn.commit()
    except Exception:
        account_evidence_drop(
            logger=logger,
            evidence_ref=f"sse_event:{project_id}:{event_type}",
            reason="sse_event_persist_failure",
        )
        if conn is not None:
            try:
                conn.rollback()
            except Exception:
                logger.warning(
                    "Rollback also failed during SSE event persistence — database may be in inconsistent state"
                )
        logger.warning(
            "Could not persist SSE event %s for project %s — event delivered but not stored",
            event_type,
            project_id,
        )


def get_recent_sse_events(
    project_id: str,
    limit: int = 100,
    since: str | None = None,
) -> list[dict[str, Any]]:
    """Return persisted SSE events for *project_id* in ascending order.

    Args:
        project_id: Project to query.
        limit: Maximum number of rows to return.
        since: ISO-format timestamp string; only events emitted after this
            timestamp are returned.  Pass None to return all events.

    Returns:
        List of event dicts with keys ``id``, ``project_id``, ``event_type``,
        ``payload`` (parsed JSON dict), and ``emitted_at``.  Events with
        unparseable JSON return ``{"_raw": <original string>}`` as payload.
    """
    from vetinari.database import get_connection

    conn = get_connection()
    if since is not None:
        try:
            since_sequence = int(since)
        except ValueError:
            # Compatibility fallback for legacy callers that still pass an
            # emitted_at timestamp instead of the Last-Event-ID sequence.
            rows = conn.execute(
                "SELECT id, project_id, event_type, payload_json, emitted_at, sequence_num"
                " FROM sse_event_log"
                " WHERE project_id = ? AND emitted_at > ?"
                " ORDER BY sequence_num ASC, id ASC LIMIT ?",
                (project_id, since, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, project_id, event_type, payload_json, emitted_at, sequence_num"
                " FROM sse_event_log"
                " WHERE project_id = ? AND sequence_num > ?"
                " ORDER BY sequence_num ASC, id ASC LIMIT ?",
                (project_id, since_sequence, limit),
            ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, project_id, event_type, payload_json, emitted_at, sequence_num"
            " FROM sse_event_log"
            " WHERE project_id = ?"
            " ORDER BY sequence_num ASC, id ASC LIMIT ?",
            (project_id, limit),
        ).fetchall()

    results: list[dict[str, Any]] = []
    for row in rows:
        try:
            payload = json.loads(row["payload_json"])
        except (json.JSONDecodeError, TypeError):
            payload = {"_raw": row["payload_json"]}
        results.append({
            "id": row["id"],
            "project_id": row["project_id"],
            "event_type": row["event_type"],
            "payload": payload,
            "emitted_at": row["emitted_at"],
            "sequence_num": row["sequence_num"],
        })
    return results


def cleanup_old_sse_events(hours: int = 168) -> int:
    """Delete SSE event log entries older than *hours* hours.

    Args:
        hours: Retention window in hours.  Events emitted more than this many
            hours ago are deleted.  Defaults to 168 (7 days).  Must be >= 1.

    Returns:
        Number of rows deleted.

    Raises:
        ValueError: If hours is less than 1.
    """
    if hours < 1:
        raise ValueError("hours must be >= 1")

    from vetinari.database import get_connection

    conn = get_connection()
    cursor = conn.execute(
        "DELETE FROM sse_event_log WHERE emitted_at < datetime('now', ? || ' hours')",
        (f"-{hours}",),
    )
    conn.commit()
    deleted = cursor.rowcount
    logger.info("cleanup_old_sse_events: deleted %d rows older than %d hours", deleted, hours)
    return deleted


# Alias used by cli_startup.py scheduler callback
cleanup_stale_sse_events = cleanup_old_sse_events
