"""Append-only JSONL route-decision ledger."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

from vetinari.workbench.spine_consumers import record_trace_written

from .decision import RouteDecisionError, RouteDecisionRecord


class RouteDecisionLedgerError(RuntimeError):
    """Raised when the route-decision ledger cannot be trusted."""


class RouteDecisionLedger:
    """Append-only JSONL ledger for route decisions at a caller-provided path."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        if self.path.suffix != ".jsonl":
            raise RouteDecisionLedgerError("route decision ledger path must end with .jsonl")
        if self.path.exists() and self.path.is_symlink():
            raise RouteDecisionLedgerError("route decision ledger path cannot be a symlink")
        parent = self.path.parent
        if parent.exists() and parent.is_symlink():
            raise RouteDecisionLedgerError("route decision ledger parent cannot be a symlink")
        self._lock = threading.Lock()

    def record(self, decision: RouteDecisionRecord) -> RouteDecisionRecord:
        """Append one route decision and verify the serialized shape before returning.

        Returns:
            RouteDecisionRecord value produced by record().

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        if not isinstance(decision, RouteDecisionRecord):
            raise RouteDecisionLedgerError("decision must be RouteDecisionRecord")
        parent = self.path.parent
        if not parent.exists() or not parent.is_dir():
            raise RouteDecisionLedgerError(f"ledger parent is unavailable: {parent}")
        payload = decision.to_dict()
        try:
            RouteDecisionRecord.from_mapping(payload)
            line = json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
        except (KeyError, TypeError, ValueError, RouteDecisionError) as exc:
            raise RouteDecisionLedgerError(f"invalid route decision payload: {exc}") from exc
        with self._lock:
            try:
                with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                    handle.write(line)
                    handle.flush()
                    os.fsync(handle.fileno())
                    # spine_consumers invokes get_spine() and absorbs observability failures.
                    record_trace_written(
                        trace_id=decision.decision_id,
                        query_hash="route_decision",
                        project_id="default",
                    )
            except OSError as exc:
                raise RouteDecisionLedgerError(f"failed to append route decision: {exc}") from exc
        return decision

    def read_all(self) -> tuple[RouteDecisionRecord, ...]:
        """Read all records, rejecting corrupt JSONL instead of returning partial evidence.

        Returns:
            Resolved all value.

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        if not self.path.exists():
            return ()
        records: list[RouteDecisionRecord] = []
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                for line_number, line in enumerate(handle, start=1):
                    if not line.strip():
                        continue
                    try:
                        payload: Any = json.loads(line)
                        if not isinstance(payload, dict):
                            raise RouteDecisionLedgerError("record is not a JSON object")
                        records.append(RouteDecisionRecord.from_mapping(payload))
                    except (json.JSONDecodeError, KeyError, TypeError, ValueError, RouteDecisionError) as exc:
                        raise RouteDecisionLedgerError(f"corrupt route decision at line {line_number}: {exc}") from exc
        except OSError as exc:
            raise RouteDecisionLedgerError(f"failed to read route decision ledger: {exc}") from exc
        return tuple(records)


__all__ = ["RouteDecisionLedger", "RouteDecisionLedgerError"]
