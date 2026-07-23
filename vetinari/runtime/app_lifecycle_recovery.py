"""Shutdown and crash-recovery mixin for the AM Workbench lifecycle controller.

Extracted from ``app_lifecycle.py`` to keep that module under the 500-LOC
target.  ``AppLifecycleRecoveryMixin`` is mixed into ``AppLifecycleController``
and relies on attributes/methods provided by the concrete class:

    * ``self._state_lock``  — ``threading.RLock``
    * ``self._resource_releasers``  — ``dict[str, Callable[[], None]]``
    * ``self._state_path``  — ``pathlib.Path``
    * ``self._transition(state, event=, payload=)``
    * ``self._append_jsonl(record)``
    * ``self.record_event(event_id, payload)``
    * ``self.state``  — ``LifecycleState`` property
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from vetinari.desktop.contracts import CrashRecoveryReport, ShutdownProtocol
from vetinari.runtime._lifecycle_types import LifecycleState, ShutdownReport

logger = logging.getLogger(__name__)


class AppLifecycleRecoveryMixin:
    """Shutdown and crash-recovery methods for ``AppLifecycleController``.

    This mixin adds three methods that form the shutdown / recovery surface of
    the lifecycle controller.  The concrete class must provide the attributes
    listed in the module docstring.
    """

    if TYPE_CHECKING:
        _resource_releasers: Any
        _state_lock: Any
        _state_path: Any
        _transition: Any
        record_event: Any
        state: Any

    def shutdown(
        self,
        *,
        grace_window_seconds: float | None = None,
        force_after_seconds: float | None = None,
    ) -> ShutdownReport:
        """Execute a graceful shutdown, escalating to forced if releasers time out.

        Args:
            grace_window_seconds: Per-releaser grace window in seconds.
                Defaults to ``ShutdownProtocol.grace_window_seconds``.
            force_after_seconds: Wall-clock budget for all releasers combined.
                Defaults to ``ShutdownProtocol.force_after_seconds``.

        Returns:
            A ``ShutdownReport`` capturing state transitions, released/failed
            resources, whether shutdown escalated to forced, and the receipt ID.
        """
        protocol = ShutdownProtocol()
        grace = grace_window_seconds if grace_window_seconds is not None else protocol.grace_window_seconds
        force_after = force_after_seconds if force_after_seconds is not None else protocol.force_after_seconds
        state_before = self.state  # type: ignore[attr-defined]
        self._transition(LifecycleState.STOPPING_GRACEFUL, event="shutdown_started")  # type: ignore[attr-defined]
        released: list[str] = []
        failed: list[str] = []
        started = time.monotonic()
        with self._state_lock:  # type: ignore[attr-defined]
            releasers = dict(self._resource_releasers)  # type: ignore[attr-defined]
        for name, releaser in releasers.items():
            remaining = max(0.01, min(grace, force_after - (time.monotonic() - started)))
            executor = ThreadPoolExecutor(max_workers=1)
            future = executor.submit(releaser)
            try:
                future.result(timeout=remaining)
                released.append(name)
            except TimeoutError:
                future.cancel()
                failed.append(name)
                executor.shutdown(
                    wait=False, cancel_futures=True
                )  # leak-rule-3-ok: single-worker releaser executor; future already cancelled, lifecycle shutdown cannot block on a stuck releaser.
            except Exception:
                failed.append(name)
                executor.shutdown(
                    wait=False, cancel_futures=True
                )  # leak-rule-3-ok: single-worker releaser executor; recoverable error path, lifecycle shutdown continues.
            else:
                executor.shutdown(
                    wait=False
                )  # leak-rule-3-ok: single-worker releaser executor; future already returned, worker thread is exiting.
            if (time.monotonic() - started) >= force_after:
                failed.extend(name for name in releasers if name not in released and name not in failed)
                break
        escalated = bool(failed)
        final_state = LifecycleState.STOPPING_FORCED if escalated else LifecycleState.STOPPED
        self._transition(final_state, event="shutdown_completed", payload={"failed_releasers": failed})  # type: ignore[attr-defined]
        receipt_id = self._emit_shutdown_receipt(
            state_before, final_state, released, failed, escalated, grace, force_after
        )
        return ShutdownReport(
            state_before=state_before,
            state_after=final_state,
            released_resources=tuple(released),
            failed_releasers=tuple(failed),
            escalated_to_force=escalated,
            checkpointed=True,
            receipt_id=receipt_id,
        )

    def _emit_shutdown_receipt(
        self,
        state_before: LifecycleState,
        state_after: LifecycleState,
        released: list[str],
        failed: list[str],
        escalated: bool,
        grace: float,
        force_after: float,
    ) -> str:
        """Emit a ``WorkReceipt`` for a completed shutdown and return the receipt ID.

        Args:
            state_before: Lifecycle state before shutdown began.
            state_after: Lifecycle state after shutdown completed.
            released: Names of resource releasers that succeeded.
            failed: Names of resource releasers that failed or timed out.
            escalated: Whether shutdown escalated to forced mode.
            grace: Per-releaser grace window used.
            force_after: Wall-clock budget used.

        Returns:
            A hex receipt ID string (UUID4 without hyphens).
        """
        receipt_id = uuid.uuid4().hex
        try:
            from vetinari.agents.contracts import OutcomeSignal
            from vetinari.receipts.record import WorkReceipt, WorkReceiptKind
            from vetinari.receipts.store import WorkReceiptStore
            from vetinari.types import AgentType, EvidenceBasis

            receipt = WorkReceipt(
                receipt_id=receipt_id,
                project_id="workbench-launcher",
                agent_id="app-lifecycle-controller",
                agent_type=AgentType.WORKER,
                kind=WorkReceiptKind.SPINE_EVENT,
                inputs_summary=f"shutdown {state_before.value}",
                outputs_summary=f"{state_after.value}; released={len(released)} failed={len(failed)}",
                outcome=OutcomeSignal(
                    passed=not escalated,
                    score=0.0 if escalated else 1.0,
                    basis=EvidenceBasis.TOOL_EVIDENCE,
                ),
            )
            WorkReceiptStore().append(receipt)
        except Exception:
            self.record_event(  # type: ignore[attr-defined]
                "shutdown_receipt_emit_failed",
                {
                    "receipt_id": receipt_id,
                    "state_before": state_before.value,
                    "state_after": state_after.value,
                    "released": released,
                    "failed": failed,
                    "grace_window_seconds": grace,
                    "force_after_seconds": force_after,
                },
            )
        return receipt_id

    def recover_from_crash(self) -> CrashRecoveryReport:
        """Inspect the persisted JSONL state and classify the last shutdown.

        Reads the lifecycle JSONL from ``self._state_path``, locates the most
        recent state-transition record, and classifies the prior run as one of:
        ``"clean_shutdown"``, ``"crash_detected"``, or ``"partial"``.

        Returns:
            A ``CrashRecoveryReport`` with the detection timestamp, last run ID,
            classification, and an optional detail message.
        """
        detected = datetime.now(timezone.utc)
        if not self._state_path.exists():  # type: ignore[attr-defined]
            return CrashRecoveryReport(detected, None, "clean_shutdown", None)
        records: list[dict[str, Any]] = []
        malformed_line = 0
        try:
            for line_no, line in enumerate(
                self._state_path.read_text(encoding="utf-8").splitlines(),  # type: ignore[attr-defined]
                start=1,
            ):
                malformed_line = line_no
                if not line.strip():
                    continue
                records.append(json.loads(line))
        except json.JSONDecodeError as exc:
            logger.warning("Handled recoverable failure before fallback.", exc_info=True)
            return CrashRecoveryReport(
                detected, None, "partial", f"malformed lifecycle JSONL at line {malformed_line}: {exc.msg}"
            )
        if not records:
            return CrashRecoveryReport(detected, None, "clean_shutdown", None)
        state_records = [record for record in records if record.get("state_after") or record.get("state")]
        if not state_records:
            return CrashRecoveryReport(
                detected, None, "partial", "lifecycle JSONL contained no state transition records"
            )
        tail = state_records[-1]
        tail_state = str(tail.get("state_after") or tail.get("state") or "")
        last_run_id = tail.get("run_id") or (tail.get("payload") or {}).get("run_id")
        if tail_state in {LifecycleState.STOPPED.value, LifecycleState.STOPPING_GRACEFUL.value}:
            return CrashRecoveryReport(detected, last_run_id, "clean_shutdown", None)
        if tail_state.startswith("running_") or tail_state in {
            LifecycleState.STARTING.value,
            LifecycleState.WAITING_FOR_HEALTH.value,
        }:
            return CrashRecoveryReport(detected, last_run_id, "crash_detected", f"last tail state was {tail_state}")
        return CrashRecoveryReport(detected, last_run_id, "partial", f"unexpected tail state {tail_state!r}")


__all__ = ["AppLifecycleRecoveryMixin"]
