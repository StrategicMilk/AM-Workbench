"""Append-only JSONL store for core-loop eval cases."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from vetinari.workbench.spine_consumers import record_eval_written
from vetinari.workbench.trace_eval_core.case import _FEED_TARGETS, EvalCaseRecord, EvalCaseRecordError


class EvalCaseStoreError(RuntimeError):
    """Raised when the eval-case JSONL store cannot be trusted."""


class EvalCaseStore:
    """Append-only JSONL persistence for eval cases."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        if self.path.suffix != ".jsonl":
            raise EvalCaseStoreError("eval-case store path must end with .jsonl")
        if self.path.is_symlink():
            raise EvalCaseStoreError(f"eval-case store path must not be a symlink: {self.path}")
        if self.path.parent.exists() and self.path.parent.is_symlink():
            raise EvalCaseStoreError(f"eval-case store parent must not be a symlink: {self.path.parent}")
        self._lock = threading.Lock()

    def append(self, record: EvalCaseRecord) -> EvalCaseRecord:
        """Execute the append operation.

        Returns:
            EvalCaseRecord value produced by append().

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        if not isinstance(record, EvalCaseRecord):
            raise EvalCaseStoreError("record must be EvalCaseRecord")
        parent = self.path.parent
        if not parent.exists() or not parent.is_dir():
            raise EvalCaseStoreError(f"eval-case store parent unavailable: {parent}")
        payload = record.to_dict()
        EvalCaseRecord.from_mapping(payload)
        line = json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
        try:
            with self._lock, self.path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(line)
                handle.flush()
                os.fsync(handle.fileno())
            # spine_consumers invokes get_spine() and absorbs observability failures.
            record_eval_written(
                eval_id=record.case_id,
                project_id="default",
                score=None,
            )
        except OSError as exc:
            raise EvalCaseStoreError(f"failed to append eval case: {exc}") from exc
        return record

    def read_all(self) -> tuple[EvalCaseRecord, ...]:
        """Execute the read all operation.

        Returns:
            Resolved all value.

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        if not self.path.exists():
            return ()
        records: list[EvalCaseRecord] = []
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                for line_number, line in enumerate(handle, start=1):
                    if not line.strip():
                        continue
                    try:
                        payload = json.loads(line)
                        if not isinstance(payload, dict):
                            raise TypeError("eval-case line is not an object")
                        records.append(EvalCaseRecord.from_mapping(payload))
                    except (json.JSONDecodeError, KeyError, TypeError, ValueError, EvalCaseRecordError) as exc:
                        raise EvalCaseStoreError(f"corrupt eval-case at line {line_number}: {exc}") from exc
        except OSError as exc:
            raise EvalCaseStoreError(f"failed to read eval-case store: {exc}") from exc
        return tuple(records)

    def list_by_consumer_feed_target(self, target: str) -> tuple[EvalCaseRecord, ...]:
        """Execute the list by consumer feed target operation.

        Returns:
            Collection of by consumer feed target values.

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        if target not in _FEED_TARGETS:
            raise EvalCaseStoreError(f"unknown consumer feed target: {target}")
        return tuple(record for record in self.read_all() if target in record.consumer_feed_targets)


__all__ = ["EvalCaseStore", "EvalCaseStoreError"]
