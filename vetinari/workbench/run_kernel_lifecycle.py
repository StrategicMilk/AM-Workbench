"""Run-kernel lifecycle operations mixed into the service facade."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Any

from vetinari.workbench.run_kernel_records import _TERMINAL_STATUSES, _handle_from_snapshot, _stream_id
from vetinari.workbench.session_kernel import (
    RecoveryAction,
    RunCheckpoint,
    RunEventRecord,
    RunHandle,
    RunKernelError,
    RunKernelResult,
    RunKernelStatus,
    RunSessionSnapshot,
    RunSessionStart,
    RunStepReceipt,
    RunStreamReplay,
    canonicalize_id,
)


class RunKernelLifecycleMixin:
    """Public run lifecycle, recovery, and event-recording operations."""

    if TYPE_CHECKING:
        _config: Any
        _is_stale: Any
        _load_snapshot: Any
        _lock: Any
        _now_iso: Any
        _read_event_log: Any
        _with_event: Any
        _write_snapshot: Any

    def start_run(self, request: RunSessionStart) -> RunKernelResult:
        """Start a new run or recover a stale existing run before restart.

        Returns:
            RunKernelResult value produced by start_run().
        """
        with self._lock:
            loaded = self._load_snapshot(request.project_id, request.run_id)
            if isinstance(loaded, RunKernelResult):
                return loaded
            now = self._now_iso()
            if loaded is None:
                snapshot = RunSessionSnapshot(
                    project_id=request.project_id,
                    run_id=request.run_id,
                    workload_kind=request.workload_kind,
                    status=RunKernelStatus.RUNNING,
                    created_at_utc=now,
                    updated_at_utc=now,
                    heartbeat_at_utc=now,
                    evidence_links=request.evidence_links,
                    lease_id=request.lease_id,
                    policy_decision_ref=request.policy_decision_ref,
                    dry_run_ref=request.dry_run_ref,
                    shadow_run_ref=request.shadow_run_ref,
                    context_manifest_ref=request.context_manifest_ref,
                    artifacts=request.artifacts,
                    events=("started",),
                )
                self._write_snapshot(snapshot)
                return RunKernelResult(RunKernelStatus.RUNNING, RecoveryAction.NONE, ("started",), snapshot)

            if loaded.status in _TERMINAL_STATUSES:
                return RunKernelResult(
                    RunKernelStatus.BLOCKED,
                    RecoveryAction.BLOCK,
                    (f"run-already-terminal:{loaded.status.value}",),
                    loaded,
                )
            if loaded.status is RunKernelStatus.RUNNING and not self._is_stale(loaded):
                return RunKernelResult(
                    RunKernelStatus.BLOCKED,
                    RecoveryAction.BLOCK,
                    ("active-heartbeat-present",),
                    loaded,
                )
            if (
                loaded.status is RunKernelStatus.RUNNING
                and self._config.stale_heartbeat_strategy is RecoveryAction.BLOCK
            ):
                blocked = self._with_event(
                    loaded,
                    now,
                    status=RunKernelStatus.BLOCKED,
                    recovery_action=RecoveryAction.BLOCK,
                    event="stale-heartbeat-blocked-before-restart",
                )
                self._write_snapshot(blocked)
                return RunKernelResult(
                    RunKernelStatus.BLOCKED,
                    RecoveryAction.BLOCK,
                    ("stale-heartbeat-blocked-before-restart",),
                    blocked,
                )

            restarted = replace(
                loaded,
                status=RunKernelStatus.RUNNING,
                updated_at_utc=now,
                heartbeat_at_utc=now,
                recovery_action=RecoveryAction.REAP,
                restart_count=loaded.restart_count + 1,
                events=(*loaded.events, "stale-heartbeat-reaped-before-restart"),
            )
            self._write_snapshot(restarted)
            return RunKernelResult(
                RunKernelStatus.RUNNING,
                RecoveryAction.REAP,
                ("stale-heartbeat-reaped-before-restart",),
                restarted,
            )

    def inspect_run(self, *, project_id: str, run_id: str) -> RunKernelResult:
        """Return a typed view of one run without mutating corrupt snapshots.

        Returns:
            RunKernelResult value produced by inspect_run().
        """
        project = canonicalize_id(project_id, field_name="project_id")
        run = canonicalize_id(run_id, field_name="run_id")
        with self._lock:
            loaded = self._load_snapshot(project, run)
            if isinstance(loaded, RunKernelResult):
                return loaded
            if loaded is None:
                return RunKernelResult(RunKernelStatus.BLOCKED, RecoveryAction.BLOCK, ("run-not-found",), None)
            if loaded.status is RunKernelStatus.RUNNING and self._is_stale(loaded):
                return RunKernelResult(
                    RunKernelStatus.BLOCKED,
                    RecoveryAction.BLOCK,
                    ("stale-heartbeat-requires-restart",),
                    loaded,
                )
            return RunKernelResult(loaded.status, loaded.recovery_action, ("snapshot-loaded",), loaded)

    def handle_for_run(self, *, project_id: str, run_id: str) -> RunHandle | RunKernelResult:
        """Return a rejoinable handle for a trusted run snapshot.

        Returns:
            RunHandle | RunKernelResult value produced by handle_for_run().
        """
        result = self.inspect_run(project_id=project_id, run_id=run_id)
        if result.snapshot is None:
            return result
        return _handle_from_snapshot(result.snapshot)

    def rejoin_stream(
        self,
        *,
        project_id: str,
        run_id: str,
        stream_id: str,
        after_sequence: int = 0,
    ) -> RunStreamReplay:
        """Replay bounded ordered events for an existing stream id without mutating state.

        Returns:
            RunStreamReplay value produced by rejoin_stream().
        """
        project = canonicalize_id(project_id, field_name="project_id")
        run = canonicalize_id(run_id, field_name="run_id")
        expected_stream_id = _stream_id(project, run)
        if stream_id != expected_stream_id:
            return RunStreamReplay("blocked", stream_id or "unknown", (), ("stream-id-mismatch",))
        loaded = self._load_snapshot(project, run)
        if isinstance(loaded, RunKernelResult):
            return RunStreamReplay("unavailable", stream_id, (), loaded.reasons)
        if loaded is None:
            return RunStreamReplay("unavailable", stream_id, (), ("run-not-found",))
        events = tuple(event for event in self._read_event_log(project, run) if event.sequence > after_sequence)
        if not events:
            events = tuple(
                RunEventRecord(
                    sequence=index + 1,
                    stream_id=stream_id,
                    run_id=run,
                    event_type=event,
                    status=loaded.status.value,
                    occurred_at_utc=loaded.updated_at_utc,
                )
                for index, event in enumerate(loaded.events)
                if index + 1 > after_sequence
            )
        retained = events[-self._config.event_retention_count :]
        if after_sequence and events and events[0].sequence > after_sequence + 1:
            return RunStreamReplay("expired", stream_id, retained, ("stream-replay-expired",))
        return RunStreamReplay("available", stream_id, retained, ("stream-replayed",))

    def record_heartbeat(self, *, project_id: str, run_id: str) -> RunKernelResult:
        """Refresh the heartbeat for a running snapshot.

        Returns:
            Outcome produced by record_heartbeat().
        """
        with self._lock:
            loaded = self._load_snapshot(project_id, run_id)
            if isinstance(loaded, RunKernelResult):
                return loaded
            if loaded is None:
                return RunKernelResult(RunKernelStatus.BLOCKED, RecoveryAction.BLOCK, ("run-not-found",), None)
            if loaded.status is not RunKernelStatus.RUNNING:
                return RunKernelResult(loaded.status, RecoveryAction.BLOCK, ("run-not-running",), loaded)
            now = self._now_iso()
            snapshot = replace(loaded, updated_at_utc=now, heartbeat_at_utc=now)
            self._write_snapshot(snapshot)
            return RunKernelResult(RunKernelStatus.RUNNING, RecoveryAction.NONE, ("heartbeat-recorded",), snapshot)

    def stop_run(self, *, project_id: str, run_id: str, reason: str = "operator-stop") -> RunKernelResult:
        """Request an orderly stop without deleting or overwriting run state.

        Returns:
            RunKernelResult value produced by stop_run().
        """
        with self._lock:
            loaded = self._load_snapshot(project_id, run_id)
            if isinstance(loaded, RunKernelResult):
                return loaded
            if loaded is None:
                return RunKernelResult(RunKernelStatus.BLOCKED, RecoveryAction.BLOCK, ("run-not-found",), None)
            if loaded.status in _TERMINAL_STATUSES:
                return RunKernelResult(loaded.status, RecoveryAction.NONE, ("run-already-terminal",), loaded)
            now = self._now_iso()
            token = f"stop-requested:{reason.strip() or 'operator-stop'}"
            snapshot = replace(
                loaded,
                status=RunKernelStatus.INTERRUPTED,
                updated_at_utc=now,
                recovery_action=RecoveryAction.ASK,
                events=(*loaded.events, token),
            )
            self._write_snapshot(snapshot)
            return RunKernelResult(RunKernelStatus.INTERRUPTED, RecoveryAction.ASK, (token,), snapshot)

    def wait_for_run(self, *, project_id: str, run_id: str) -> RunKernelResult:
        """Return terminal result state or a typed still-running response.

        Returns:
            RunKernelResult value produced by wait_for_run().
        """
        result = self.inspect_run(project_id=project_id, run_id=run_id)
        if result.snapshot is None:
            return result
        if result.snapshot.status in _TERMINAL_STATUSES:
            return RunKernelResult(result.snapshot.status, RecoveryAction.NONE, ("run-terminal",), result.snapshot)
        return RunKernelResult(result.snapshot.status, RecoveryAction.ASK, ("run-not-terminal",), result.snapshot)

    def result_for_run(self, *, project_id: str, run_id: str) -> RunKernelResult:
        """Return the final result only for completed successful runs.

        Returns:
            RunKernelResult value produced by result_for_run().
        """
        result = self.inspect_run(project_id=project_id, run_id=run_id)
        if result.snapshot is None:
            return result
        if result.snapshot.status is RunKernelStatus.SUCCEEDED and result.snapshot.final_verdict_ref:
            return RunKernelResult(RunKernelStatus.SUCCEEDED, RecoveryAction.NONE, ("result-ready",), result.snapshot)
        return RunKernelResult(result.snapshot.status, RecoveryAction.BLOCK, ("result-not-ready",), result.snapshot)

    def seal_checkpoint(
        self,
        *,
        project_id: str,
        run_id: str,
        checkpoint_id: str,
        payload_ref: str,
        payload_hash: str,
        mark_interrupted: bool = True,
    ) -> RunKernelResult:
        """Seal a checkpoint and optionally mark the run interrupted.

        Returns:
            RunKernelResult value produced by seal_checkpoint().
        """
        with self._lock:
            loaded = self._load_snapshot(project_id, run_id)
            if isinstance(loaded, RunKernelResult):
                return loaded
            if loaded is None:
                return RunKernelResult(RunKernelStatus.BLOCKED, RecoveryAction.BLOCK, ("run-not-found",), None)
            if loaded.status in _TERMINAL_STATUSES:
                return RunKernelResult(
                    RunKernelStatus.BLOCKED,
                    RecoveryAction.BLOCK,
                    (f"run-already-terminal:{loaded.status.value}",),
                    loaded,
                )
            now = self._now_iso()
            checkpoint = RunCheckpoint(
                checkpoint_id=checkpoint_id,
                sealed=True,
                created_at_utc=now,
                payload_ref=payload_ref,
                payload_hash=payload_hash,
            )
            status = RunKernelStatus.INTERRUPTED if mark_interrupted else loaded.status
            snapshot = replace(
                loaded,
                status=status,
                updated_at_utc=now,
                checkpoint=checkpoint,
                recovery_action=RecoveryAction.NONE,
                events=(*loaded.events, "sealed-checkpoint-recorded"),
            )
            self._write_snapshot(snapshot)
            return RunKernelResult(status, RecoveryAction.NONE, ("sealed-checkpoint-recorded",), snapshot)

    def resume_run(self, *, project_id: str, run_id: str) -> RunKernelResult:
        """Resume an interrupted run from its sealed checkpoint.

        Returns:
            RunKernelResult value produced by resume_run().
        """
        with self._lock:
            loaded = self._load_snapshot(project_id, run_id)
            if isinstance(loaded, RunKernelResult):
                return loaded
            if loaded is None:
                return RunKernelResult(RunKernelStatus.BLOCKED, RecoveryAction.BLOCK, ("run-not-found",), None)
            if loaded.status in _TERMINAL_STATUSES:
                return RunKernelResult(
                    RunKernelStatus.BLOCKED,
                    RecoveryAction.BLOCK,
                    (f"run-already-terminal:{loaded.status.value}",),
                    loaded,
                )
            if self._config.require_sealed_checkpoint_for_resume and not loaded.checkpoint.sealed:
                return RunKernelResult(
                    RunKernelStatus.RECOVERY_NEEDED,
                    RecoveryAction.ASK,
                    ("sealed-checkpoint-required",),
                    loaded,
                )
            now = self._now_iso()
            snapshot = replace(
                loaded,
                status=RunKernelStatus.RUNNING,
                updated_at_utc=now,
                heartbeat_at_utc=now,
                recovery_action=RecoveryAction.RESUME,
                events=(*loaded.events, "resumed-from-sealed-checkpoint"),
            )
            self._write_snapshot(snapshot)
            return RunKernelResult(
                RunKernelStatus.RUNNING,
                RecoveryAction.RESUME,
                ("resumed-from-sealed-checkpoint",),
                snapshot,
            )

    def complete_run(
        self, *, project_id: str, run_id: str, final_verdict_ref: str, succeeded: bool = True
    ) -> RunKernelResult:
        """Seal a terminal verdict ref for the run.

        Returns:
            RunKernelResult value produced by complete_run().

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        if not isinstance(succeeded, bool):
            raise RunKernelError("succeeded-invalid", "succeeded must be a boolean")
        if succeeded and not final_verdict_ref.strip():
            raise RunKernelError("final-verdict-ref-required", "successful runs require final_verdict_ref")
        with self._lock:
            loaded = self._load_snapshot(project_id, run_id)
            if isinstance(loaded, RunKernelResult):
                return loaded
            if loaded is None:
                return RunKernelResult(RunKernelStatus.BLOCKED, RecoveryAction.BLOCK, ("run-not-found",), None)
            if loaded.status in _TERMINAL_STATUSES:
                return RunKernelResult(
                    RunKernelStatus.BLOCKED,
                    RecoveryAction.BLOCK,
                    (f"run-already-terminal:{loaded.status.value}",),
                    loaded,
                )
            now = self._now_iso()
            status = RunKernelStatus.SUCCEEDED if succeeded else RunKernelStatus.FAILED
            snapshot = replace(
                loaded,
                status=status,
                updated_at_utc=now,
                final_verdict_ref=final_verdict_ref,
                recovery_action=RecoveryAction.NONE,
                events=(*loaded.events, "final-verdict-recorded"),
            )
            self._write_snapshot(snapshot)
            return RunKernelResult(status, RecoveryAction.NONE, ("final-verdict-recorded",), snapshot)

    def record_step_receipt(self, *, project_id: str, run_id: str, receipt: RunStepReceipt) -> RunKernelResult:
        """Record one idempotent workflow-step receipt reference.

        Returns:
            Outcome produced by record_step_receipt().
        """
        with self._lock:
            loaded = self._load_snapshot(project_id, run_id)
            if isinstance(loaded, RunKernelResult):
                return loaded
            if loaded is None:
                return RunKernelResult(RunKernelStatus.BLOCKED, RecoveryAction.BLOCK, ("run-not-found",), None)
            if loaded.status in _TERMINAL_STATUSES:
                return RunKernelResult(
                    RunKernelStatus.BLOCKED,
                    RecoveryAction.BLOCK,
                    (f"run-already-terminal:{loaded.status.value}",),
                    loaded,
                )
            prefix = f"step-receipt:{receipt.step_id}:{receipt.attempt_id}:{receipt.idempotency_key}:"
            existing = tuple(event for event in loaded.events if event.startswith(prefix))
            if existing:
                return RunKernelResult(loaded.status, RecoveryAction.NONE, ("step-receipt-already-committed",), loaded)
            now = self._now_iso()
            snapshot = replace(
                loaded,
                updated_at_utc=now,
                events=(*loaded.events, receipt.event_token),
            )
            self._write_snapshot(snapshot)
            return RunKernelResult(snapshot.status, RecoveryAction.NONE, ("step-receipt-committed",), snapshot)

    def assert_replay_agreement(
        self,
        *,
        project_id: str,
        run_id: str,
        expected_tokens: tuple[str, ...],
    ) -> RunKernelResult:
        """Check replay step tokens against committed step receipts.

        Returns:
            RunKernelResult value produced by assert_replay_agreement().
        """
        loaded = self._load_snapshot(project_id, run_id)
        if isinstance(loaded, RunKernelResult):
            return loaded
        if loaded is None:
            return RunKernelResult(RunKernelStatus.BLOCKED, RecoveryAction.BLOCK, ("run-not-found",), None)
        actual = tuple(event for event in loaded.events if event.startswith("step-receipt:"))
        if actual != expected_tokens:
            return RunKernelResult(
                RunKernelStatus.BLOCKED, RecoveryAction.BLOCK, ("replay-agreement-mismatch",), loaded
            )
        return RunKernelResult(loaded.status, RecoveryAction.NONE, ("replay-agreement-ok",), loaded)

    def record_evidence_decision(
        self, *, project_id: str, run_id: str, evidence_ref: str, status: str
    ) -> RunKernelResult:
        """Attach provider, knowledge, sandbox, or trace decision evidence to a run.

        Returns:
            Outcome produced by record_evidence_decision().
        """
        with self._lock:
            loaded = self._load_snapshot(project_id, run_id)
            if isinstance(loaded, RunKernelResult):
                return loaded
            if loaded is None:
                return RunKernelResult(RunKernelStatus.BLOCKED, RecoveryAction.BLOCK, ("run-not-found",), None)
            if loaded.status in _TERMINAL_STATUSES:
                return RunKernelResult(
                    RunKernelStatus.BLOCKED,
                    RecoveryAction.BLOCK,
                    (f"run-already-terminal:{loaded.status.value}",),
                    loaded,
                )
            clean_status = status.strip() or "unknown"
            token = f"evidence-decision:{clean_status}:{evidence_ref.strip()}"
            now = self._now_iso()
            snapshot = replace(loaded, updated_at_utc=now, events=(*loaded.events, token))
            self._write_snapshot(snapshot)
            return RunKernelResult(snapshot.status, RecoveryAction.NONE, ("evidence-decision-recorded",), snapshot)
