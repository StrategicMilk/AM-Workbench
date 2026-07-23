"""Run-kernel snapshot and event-log persistence helpers."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import replace
from datetime import UTC
from pathlib import Path
from typing import TYPE_CHECKING, Any

from vetinari.workbench.run_kernel_records import _parse_utc, _stream_id
from vetinari.workbench.session_kernel import (
    RecoveryAction,
    RunEventRecord,
    RunKernelError,
    RunKernelResult,
    RunKernelStatus,
    RunSessionSnapshot,
    canonicalize_id,
)
from vetinari.workbench.spine_consumers import record_run_completed, record_trace_written

logger = logging.getLogger(__name__)


class RunKernelPersistenceMixin:
    """Snapshot file, event-log, and time helpers for run-kernel services."""

    if TYPE_CHECKING:
        _config: Any
        _now: Any

    def snapshot_path(self, *, project_id: str, run_id: str) -> Path:
        """Return the canonical snapshot path for tests and support bundles.

        Returns:
            Path value produced by snapshot_path().
        """
        project = canonicalize_id(project_id, field_name="project_id")
        run = canonicalize_id(run_id, field_name="run_id")
        return self._snapshot_path(project, run)

    def _load_snapshot(self, project_id: str, run_id: str) -> RunSessionSnapshot | RunKernelResult | None:
        project = canonicalize_id(project_id, field_name="project_id")
        run = canonicalize_id(run_id, field_name="run_id")
        path = self._snapshot_path(project, run)
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return RunSessionSnapshot.from_mapping(payload)
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError, RunKernelError) as exc:
            logger.warning("Handled recoverable failure before fallback.", exc_info=True)
            return RunKernelResult(
                RunKernelStatus.RECOVERY_NEEDED,
                RecoveryAction.ASK,
                (f"snapshot-corrupt:{exc.__class__.__name__}",),
                None,
            )

    def _write_snapshot(self, snapshot: RunSessionSnapshot) -> None:
        path = self._snapshot_path(snapshot.project_id, snapshot.run_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(snapshot.to_dict(), sort_keys=True, indent=2) + "\n"
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        with tmp_path.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        tmp_path.replace(path)
        # spine_consumers invokes get_spine() and absorbs observability failures.
        record_run_completed(
            run_id=snapshot.run_id,
            kind="agent_run",
            project_id=snapshot.project_id,
            status=snapshot.status.value,
        )
        self._sync_event_log(snapshot)

    def _snapshot_path(self, project_id: str, run_id: str) -> Path:
        return self._config.state_dir / project_id / f"{run_id}.json"

    def _event_log_path(self, project_id: str, run_id: str) -> Path:
        return self._config.state_dir / project_id / f"{run_id}.events.jsonl"

    def _read_event_log(self, project_id: str, run_id: str) -> tuple[RunEventRecord, ...]:
        return tuple(self._read_all_event_log(project_id, run_id)[-self._config.event_retention_count :])

    def _read_all_event_log(self, project_id: str, run_id: str) -> tuple[RunEventRecord, ...]:
        path = self._event_log_path(project_id, run_id)
        if not path.exists():
            return ()
        events: list[RunEventRecord] = []
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                payload = json.loads(line)
                events.append(
                    RunEventRecord(
                        sequence=int(payload["sequence"]),
                        stream_id=str(payload["stream_id"]),
                        run_id=str(payload["run_id"]),
                        event_type=str(payload["event_type"]),
                        status=str(payload["status"]),
                        occurred_at_utc=str(payload["occurred_at_utc"]),
                        detail=str(payload.get("detail", "")),
                    )
                )
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
            logger.warning("Handled recoverable failure before fallback.", exc_info=True)
            return ()
        return tuple(events)

    def _sync_event_log(self, snapshot: RunSessionSnapshot) -> None:
        path = self._event_log_path(snapshot.project_id, snapshot.run_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        existing = self._read_all_event_log(snapshot.project_id, snapshot.run_id)
        existing_count = max((event.sequence for event in existing), default=0)
        rows = []
        for index, event in enumerate(snapshot.events[existing_count:], start=existing_count + 1):
            rows.append(
                json.dumps(
                    RunEventRecord(
                        sequence=index,
                        stream_id=_stream_id(snapshot.project_id, snapshot.run_id),
                        run_id=snapshot.run_id,
                        event_type=event,
                        status=snapshot.status.value,
                        occurred_at_utc=snapshot.updated_at_utc,
                    ).to_dict(),
                    sort_keys=True,
                )
            )
        if not rows:
            return
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            for row in rows:
                handle.write(row + "\n")
        # spine_consumers invokes get_spine() and absorbs observability failures.
        record_trace_written(
            trace_id=f"run-events-{snapshot.project_id}-{snapshot.run_id}",
            query_hash="run_kernel_events",
            project_id=snapshot.project_id,
        )

    def _is_stale(self, snapshot: RunSessionSnapshot) -> bool:
        heartbeat = None
        try:
            heartbeat = _parse_utc(snapshot.heartbeat_at_utc)
        except ValueError:
            logger.warning("Handled recoverable failure before fallback.", exc_info=True)
        if heartbeat is None:
            return True
        age = (self._now() - heartbeat).total_seconds()
        return age > self._config.heartbeat_timeout_seconds

    def _now_iso(self) -> str:
        return self._now().astimezone(UTC).isoformat().replace("+00:00", "Z")

    @staticmethod
    def _with_event(
        snapshot: RunSessionSnapshot,
        now: str,
        *,
        status: RunKernelStatus,
        recovery_action: RecoveryAction,
        event: str,
    ) -> RunSessionSnapshot:
        return replace(
            snapshot,
            status=status,
            updated_at_utc=now,
            recovery_action=recovery_action,
            events=(*snapshot.events, event),
        )
