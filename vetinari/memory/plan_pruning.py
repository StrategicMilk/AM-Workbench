"""Plan-history retention pruning helpers."""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from vetinari import database as dbmod

logger = logging.getLogger(__name__)

PLAN_RETENTION_DAYS = int(os.environ.get("PLAN_RETENTION_DAYS", "90"))
PLAN_RETENTION_OWNER_REF = "plan-retention-policy"


class PlanPruneError(RuntimeError):
    """Raised when plan retention cannot prove deletion provenance."""


def _json_payload_proof(value: Any) -> dict[str, Any]:
    """Return non-restorable proof metadata for a deleted plan payload."""
    raw = json.dumps(value, default=str, sort_keys=True)
    data = raw.encode("utf-8", errors="replace")
    return {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}


def _plan_receipt_refs(plan_rows: Any, subtask_rows: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Build deletion receipts without retaining full plan or subtask payloads."""
    plan_refs = [
        {
            "plan_id": row.get("plan_id"),
            "created_at": row.get("created_at"),
            "status": row.get("status"),
            "payload_proof": _json_payload_proof(row),
        }
        for row in plan_rows
    ]
    subtask_refs = [
        {
            "subtask_id": row.get("subtask_id"),
            "plan_id": row.get("plan_id"),
            "status": row.get("status"),
            "payload_proof": _json_payload_proof(row),
        }
        for row in subtask_rows
    ]
    return plan_refs, subtask_refs


def prune_old_plans(
    store: Any,
    retention_days: int = PLAN_RETENTION_DAYS,
    *,
    dry_run: bool = False,
    owner_ref: str = PLAN_RETENTION_OWNER_REF,
) -> int:
    """Remove plan records older than *retention_days*.

    Args:
        store: MemoryStore-compatible object with SQLite or JSON fallback state.
        retention_days: Age threshold for pruning old plan records.
        dry_run: When true, write a receipt without deleting records.
        owner_ref: Operator or policy reference proving prune ownership.

    Returns:
        Number of plan records covered by the prune operation.

    Raises:
        PlanPruneError: If ownership proof is missing or the prune receipt
            cannot be persisted before deletion.
    """
    if not owner_ref.strip():
        raise PlanPruneError("owner_ref is required before plan retention pruning")
    if store.use_json_fallback:
        return _prune_old_json(store, retention_days, dry_run=dry_run, owner_ref=owner_ref)

    conn: sqlite3.Connection | None = None
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
        conn = dbmod.get_connection()
        old_plan_rows = _old_sql_plan_rows(conn, cutoff)
        old_plan_ids = [row["plan_id"] for row in old_plan_rows]
        old_subtasks = _old_sql_subtasks(conn, old_plan_ids)
        receipt = _build_sql_prune_receipt(
            cutoff=cutoff,
            retention_days=retention_days,
            owner_ref=owner_ref,
            dry_run=dry_run,
            old_plan_rows=old_plan_rows,
            old_subtasks=old_subtasks,
        )
        _write_sql_prune_receipt(conn, receipt, retention_days, owner_ref, dry_run, len(old_plan_rows))

        if not dry_run:
            _delete_sql_plans(conn, old_plan_ids)

        conn.commit()
        logger.info("Pruned %d old plans", 0 if dry_run else len(old_plan_rows))
        return len(old_plan_rows)

    except sqlite3.Error as exc:
        if conn is not None:
            with contextlib.suppress(Exception):
                conn.rollback()
        logger.error("Failed to record plan prune receipt; prune skipped: %s", exc)
        raise PlanPruneError("plan prune receipt failed; prune skipped") from exc


def _old_sql_plan_rows(conn: sqlite3.Connection, cutoff: datetime) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in conn.execute(
            "SELECT * FROM PlanHistory WHERE created_at < ? ORDER BY created_at",
            (cutoff.isoformat(),),
        ).fetchall()
    ]


def _old_sql_subtasks(conn: sqlite3.Connection, old_plan_ids: list[str]) -> list[dict[str, Any]]:
    subtasks: list[dict[str, Any]] = []
    for plan_id in old_plan_ids:
        subtasks.extend(
            dict(row)
            for row in conn.execute(
                "SELECT * FROM SubtaskMemory WHERE plan_id = ?",
                (plan_id,),
            ).fetchall()
        )
    return subtasks


def _build_sql_prune_receipt(
    *,
    cutoff: datetime,
    retention_days: int,
    owner_ref: str,
    dry_run: bool,
    old_plan_rows: list[dict[str, Any]],
    old_subtasks: list[dict[str, Any]],
) -> dict[str, Any]:
    plan_refs, subtask_refs = _plan_receipt_refs(old_plan_rows, old_subtasks)
    now = datetime.now(timezone.utc).isoformat()
    return {
        "receipt_id": f"plan-prune:{now}",
        "created_at": now,
        "cutoff": cutoff.isoformat(),
        "retention_days": retention_days,
        "owner_ref": owner_ref,
        "dry_run": dry_run,
        "pruned_count": len(old_plan_rows),
        "plan_refs": plan_refs,
        "subtask_refs": subtask_refs,
        "restore_contract": "restore from operator backup; retention receipts intentionally exclude deleted plan payloads",
    }


def _write_sql_prune_receipt(
    conn: sqlite3.Connection,
    receipt: dict[str, Any],
    retention_days: int,
    owner_ref: str,
    dry_run: bool,
    pruned_count: int,
) -> None:
    conn.execute(
        """
        INSERT INTO PlanPruneReceipts
        (receipt_id, created_at, cutoff, retention_days, owner_ref, dry_run, pruned_count, payload)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            receipt["receipt_id"],
            receipt["created_at"],
            receipt["cutoff"],
            retention_days,
            owner_ref,
            dry_run,
            pruned_count,
            json.dumps(receipt, default=str, sort_keys=True),
        ),
    )


def _delete_sql_plans(conn: sqlite3.Connection, old_plan_ids: list[str]) -> None:
    for plan_id in old_plan_ids:
        conn.execute("DELETE FROM SubtaskMemory WHERE plan_id = ?", (plan_id,))
        conn.execute("DELETE FROM PlanHistory WHERE plan_id = ?", (plan_id,))


def _prune_old_json(
    store: Any,
    retention_days: int,
    *,
    dry_run: bool = False,
    owner_ref: str = PLAN_RETENTION_OWNER_REF,
) -> int:
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    cutoff_str = cutoff.isoformat()

    to_delete = [pid for pid, p in store._json_data["plans"].items() if p.get("created_at", "") < cutoff_str]
    old_plans = [store._json_data["plans"][pid] for pid in to_delete]
    old_subtasks = [subtask for subtask in store._json_data["subtasks"].values() if subtask.get("plan_id") in to_delete]
    plan_refs, subtask_refs = _plan_receipt_refs(old_plans, old_subtasks)
    receipt_id = f"plan-prune-json:{datetime.now(timezone.utc).isoformat()}"
    store._json_data.setdefault("prune_receipts", {})[receipt_id] = {
        "receipt_id": receipt_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "cutoff": cutoff_str,
        "retention_days": retention_days,
        "owner_ref": owner_ref,
        "dry_run": dry_run,
        "pruned_count": len(to_delete),
        "plan_refs": plan_refs,
        "subtask_refs": subtask_refs,
        "restore_contract": "restore from operator backup; retention receipts intentionally exclude deleted plan payloads",
    }

    if not dry_run:
        for plan_id in to_delete:
            del store._json_data["plans"][plan_id]
            store._json_data["subtasks"] = {
                subtask_id: subtask
                for subtask_id, subtask in store._json_data["subtasks"].items()
                if subtask.get("plan_id") != plan_id
            }

    store._save_json()
    return len(to_delete)


__all__ = ["PLAN_RETENTION_DAYS", "PLAN_RETENTION_OWNER_REF", "PlanPruneError", "prune_old_plans"]
