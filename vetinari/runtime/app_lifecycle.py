"""Process-lifetime AM Workbench launcher lifecycle controller."""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from datetime import datetime, timezone
from enum import Enum
from itertools import starmap
from pathlib import Path
from typing import Any

from vetinari.constants import OUTPUTS_DIR
from vetinari.desktop.contracts import (
    CrashRecoveryReport,
    HealthGateResult,
    LauncherDecision,
    LauncherDecisionAction,
    LauncherRuntimeMode,
    LauncherStatus,
    LifecycleAction,
    LifecycleCommandOrigin,
    LifecycleCommandRequest,
    LifecycleCommandResult,
)
from vetinari.runtime._lifecycle_types import LifecycleState, ShutdownReport
from vetinari.runtime.app_lifecycle_recovery import AppLifecycleRecoveryMixin

logger = logging.getLogger(__name__)


_DEFAULT_STATE_DIR = OUTPUTS_DIR / "workbench" / "launcher"
_STATE_FILENAME = "app_lifecycle_state.jsonl"
_INSTANCE: AppLifecycleController | None = None
_INSTANCE_LOCK = threading.Lock()
_ALLOWLISTED_LIFECYCLE_ACTIONS = frozenset(action.value for action in LifecycleAction)
_ADMIN_ACTIONS = frozenset({
    LifecycleAction.STOP.value,
    LifecycleAction.RESTART.value,
    LifecycleAction.QUIT_COMPLETELY.value,
    LifecycleAction.FORCE_QUIT.value,
    LifecycleAction.SUPPORT_BUNDLE.value,
})
_TRANSITIONAL_BROWSER_ACTIONS = frozenset({
    LifecycleAction.OPEN.value,
    LifecycleAction.CLOSE_WINDOW.value,
    LifecycleAction.KEEP_IN_BACKGROUND.value,
    LifecycleAction.CRASH_RECOVER.value,
})


class AppLifecycleController(AppLifecycleRecoveryMixin):
    """Thread-safe lifecycle controller and JSONL state writer."""

    def __init__(
        self,
        *,
        state_dir: Path | str = _DEFAULT_STATE_DIR,
        mode: LauncherRuntimeMode = LauncherRuntimeMode.DESKTOP_DEFAULT,
        probe_timeout_seconds: float = 0.25,
    ) -> None:
        self._state = LifecycleState.STOPPED
        self._mode = mode
        self._backend_pid: int | None = None
        self._ui_url: str | None = None
        self._state_dir = Path(state_dir)
        self._state_path = self._state_dir / _STATE_FILENAME
        self._state_lock = threading.RLock()
        self._jsonl_lock = threading.Lock()
        self._health_probes: dict[str, Callable[[], HealthGateResult]] = {}
        self._resource_releasers: dict[str, Callable[[], None]] = {}
        self._probe_timeout_seconds = probe_timeout_seconds
        self._subscribers: set[object] = set()
        self._state_dir.mkdir(parents=True, exist_ok=True)

    @property
    def state(self) -> LifecycleState:
        with self._state_lock:
            return self._state

    @property
    def subscriber_count(self) -> int:
        with self._state_lock:
            return len(self._subscribers)

    def register_health_probe(self, name: str, probe: Callable[[], HealthGateResult]) -> None:
        """Execute the register health probe operation.

        Args:
            name: Name used to identify the target object.
            probe: Probe value consumed by register_health_probe().
        """
        with self._state_lock:
            self._health_probes[name] = probe

    def register_resource_releaser(self, name: str, releaser: Callable[[], None]) -> None:
        """Execute the register resource releaser operation.

        Args:
            name: Name used to identify the target object.
            releaser: Releaser value consumed by register_resource_releaser().
        """
        with self._state_lock:
            self._resource_releasers[name] = releaser

    def subscribe(self) -> object:
        """Execute the subscribe operation.

        Returns:
            object value produced by subscribe().
        """
        token = object()
        with self._state_lock:
            self._subscribers.add(token)
        return token

    def unsubscribe(self, token: object) -> None:
        """Execute the unsubscribe operation."""
        with self._state_lock:
            self._subscribers.discard(token)

    def _set_state_for_test(self, state: LifecycleState) -> None:
        self._transition(state, event="test_set_state")

    def _transition(self, state: LifecycleState, *, event: str, payload: dict[str, Any] | None = None) -> None:
        with self._state_lock:
            before = self._state
            self._state = state
            record = {
                "event": event,
                "state_before": before.value,
                "state_after": state.value,
                "state": state.value,
                "run_id": (payload or {}).get("run_id"),
                "payload": payload or {},
                "recorded_at": datetime.now(timezone.utc).isoformat(),
            }
            self._append_jsonl(record)

    def _append_jsonl(self, record: dict[str, Any]) -> None:
        self._state_dir.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, sort_keys=True)
        with self._jsonl_lock, self._state_path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    def record_event(self, event_id: str, payload: dict[str, Any]) -> None:
        """Execute the record event operation.

        Args:
            event_id: Event object recorded or transformed by the operation.
            payload: Payload data validated or transformed by the operation.
        """
        self._append_jsonl({
            "event": event_id,
            "payload": payload,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
        })

    def _run_probe(self, name: str, probe: Callable[[], HealthGateResult]) -> HealthGateResult:
        executor = ThreadPoolExecutor(max_workers=1)
        future = executor.submit(probe)
        try:
            result = future.result(timeout=self._probe_timeout_seconds)
        except TimeoutError:
            logger.warning("Handled recoverable failure before fallback.", exc_info=True)
            future.cancel()
            executor.shutdown(
                wait=False, cancel_futures=True
            )  # leak-rule-3-ok: single-worker probe executor; future already cancelled, do not extend shutdown by blocking on a stuck probe.
            return HealthGateResult(
                name=name,
                passed=False,
                last_checked_at=datetime.now(timezone.utc),
                blockers=(f"{name} probe timeout",),
                remediation=f"Check launcher doctor for {name}.",
            )
        except Exception as exc:
            logger.warning("Handled recoverable failure before fallback.", exc_info=True)
            executor.shutdown(
                wait=False, cancel_futures=True
            )  # leak-rule-3-ok: single-worker probe executor; recoverable error path, do not block shutdown.
            return HealthGateResult(
                name=name,
                passed=False,
                last_checked_at=datetime.now(timezone.utc),
                blockers=(f"{name} probe failed: {exc}",),
                remediation=f"Repair {name} and rerun launcher doctor.",
            )
        executor.shutdown(
            wait=False
        )  # leak-rule-3-ok: single-worker probe executor; future already returned a result, worker thread is exiting.
        if result.name != name:
            return HealthGateResult(
                name=name,
                passed=result.passed,
                last_checked_at=result.last_checked_at or datetime.now(timezone.utc),
                blockers=result.blockers,
                remediation=result.remediation,
            )
        return result

    def _collect_gates(self) -> tuple[HealthGateResult, ...]:
        with self._state_lock:
            probes = dict(self._health_probes)
        if not probes:
            return (
                HealthGateResult(
                    name="backend",
                    passed=False,
                    last_checked_at=datetime.now(timezone.utc),
                    blockers=("backend not started",),
                    remediation="Start backend or run launcher doctor.",
                ),
            )
        return tuple(starmap(self._run_probe, probes.items()))

    def current_status(self) -> LauncherStatus:
        """Execute the current status operation.

        Returns:
            LauncherStatus value produced by current_status().
        """
        gates = self._collect_gates()
        with self._state_lock:
            current_state = self._state
        recovery: CrashRecoveryReport | None = None
        if current_state not in {
            LifecycleState.STARTING,
            LifecycleState.WAITING_FOR_HEALTH,
            LifecycleState.RUNNING_HEALTHY,
            LifecycleState.RUNNING_DEGRADED,
        }:
            recovery = self.recover_from_crash()
        if recovery is not None and recovery.recovered_state in {"crash_detected", "partial"}:
            gates += (
                HealthGateResult(
                    name="crash_recovery",
                    passed=False,
                    last_checked_at=recovery.detected_at,
                    blockers=(recovery.escalation_reason or recovery.recovered_state,),
                    remediation="Restart from the desktop shell to replay lifecycle recovery.",
                ),
            )
        with self._state_lock:
            return LauncherStatus(
                mode=self._mode,
                backend_pid=self._backend_pid,
                ui_url=self._ui_url,
                gates=gates,
                warnings=(),
                errors=(),
            )

    def plan_action(self, action_id: str | LifecycleAction) -> LauncherDecision:
        """Execute the plan action operation.

        Returns:
            LauncherDecision value produced by plan_action().

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        try:
            raw_action = action_id.value if isinstance(action_id, Enum) else action_id
            action = action_id if isinstance(action_id, LifecycleAction) else LifecycleAction(raw_action)
        except ValueError as exc:
            raise ValueError(f"unknown launcher action: {action_id}") from exc
        with self._state_lock:
            state = self._state
        if action is LifecycleAction.OPEN:
            if state is LifecycleState.STOPPED:
                return LauncherDecision(
                    LauncherDecisionAction.START_BACKEND,
                    ("Backend is stopped; start backend before opening the UI.",),
                    retryable=True,
                )
            status = self.current_status()
            if status.is_ready:
                return LauncherDecision(LauncherDecisionAction.OPEN_UI, ("All health gates passed; open UI.",))
            return LauncherDecision(
                LauncherDecisionAction.WAIT_FOR_HEALTH,
                tuple(
                    f"{gate.name}: {', '.join(gate.blockers) or 'not ready'}"
                    for gate in status.gates
                    if not gate.passed
                ),
                retryable=True,
            )
        if action is LifecycleAction.QUIT_COMPLETELY:
            return LauncherDecision(
                LauncherDecisionAction.STOP_GRACEFUL,
                ("Quit requested; checkpoint active runs and release resources first.",),
                retryable=False,
                escalation="force_after seconds if releasers do not finish",
            )
        if action is LifecycleAction.STOP:
            return LauncherDecision(
                LauncherDecisionAction.STOP_GRACEFUL, ("Stop requested; release resources gracefully.",)
            )
        if action is LifecycleAction.RESTART:
            return LauncherDecision(
                LauncherDecisionAction.RESTART, ("Restart requested after graceful stop.",), retryable=True
            )
        if action is LifecycleAction.FORCE_QUIT:
            return LauncherDecision(LauncherDecisionAction.FORCE_QUIT, ("Force quit requested by operator.",))
        if action is LifecycleAction.CRASH_RECOVER:
            return LauncherDecision(
                LauncherDecisionAction.RECOVER_CRASH, ("Recover crash state from lifecycle JSONL.",), retryable=True
            )
        if action is LifecycleAction.CLOSE_WINDOW:
            return LauncherDecision(
                LauncherDecisionAction.KEEP_BACKGROUND, ("Window closed; backend remains available in background.",)
            )
        if action is LifecycleAction.KEEP_IN_BACKGROUND:
            return LauncherDecision(LauncherDecisionAction.KEEP_BACKGROUND, ("Keep backend running in background.",))
        return LauncherDecision(
            LauncherDecisionAction.BLOCK_AWAITING_CONSENT,
            (f"{action.value} requires explicit user confirmation or a desktop client handler.",),
            requires_user_consent=True,
        )

    def execute_lifecycle_command(self, request: LifecycleCommandRequest) -> LifecycleCommandResult:
        """Execute a lifecycle command through the fail-closed allowlist.

        Returns:
            Lifecycle command result with the current status and denial reason when rejected.
        """
        try:
            origin = request.normalized_origin()
            action = request.normalized_action()
        except ValueError as exc:
            reason = f"unknown lifecycle command {request.action!r}: {exc}"
            logger.warning("Denied unknown lifecycle command.", extra={"action": request.action, "reason": reason})
            decision = LauncherDecision(
                LauncherDecisionAction.BLOCK_AWAITING_CONSENT,
                (reason,),
                requires_user_consent=True,
            )
            self.record_event("lifecycle_command_denied", {"request": request.to_dict(), "reason": reason})
            return LifecycleCommandResult(
                accepted=False,
                action=request.action,
                decision=decision,
                status=self.current_status(),
                denial_reason=reason,
            )

        denial = self._command_denial_reason(action, origin, request.admin_equivalent)
        if denial is not None:
            decision = LauncherDecision(
                LauncherDecisionAction.BLOCK_AWAITING_CONSENT,
                (denial,),
                requires_user_consent=True,
            )
            self.record_event("lifecycle_command_denied", {"request": request.to_dict(), "reason": denial})
            return LifecycleCommandResult(
                accepted=False,
                action=action.value,
                decision=decision,
                status=self.current_status(),
                denial_reason=denial,
            )

        decision = self.plan_action(action)
        if decision.action is LauncherDecisionAction.BLOCK_AWAITING_CONSENT:
            reason = decision.reasons[0] if decision.reasons else f"{action.value} has no lifecycle handler"
            self.record_event("lifecycle_command_denied", {"request": request.to_dict(), "reason": reason})
            return LifecycleCommandResult(
                accepted=False,
                action=action.value,
                decision=decision,
                status=self.current_status(),
                denial_reason=reason,
            )
        shutdown: dict[str, Any] | None = None
        recovery: dict[str, Any] | None = None
        self.record_event("lifecycle_command_accepted", {"request": request.to_dict(), "decision": decision.to_dict()})

        if action is LifecycleAction.CLOSE_WINDOW:
            self._mode = LauncherRuntimeMode.BACKGROUND_ONLY
            self._transition(
                LifecycleState.RUNNING_DEGRADED if self.state is LifecycleState.STOPPED else self.state,
                event="window_closed_background_kept",
                payload={"origin": origin.value},
            )
        elif action is LifecycleAction.KEEP_IN_BACKGROUND:
            self._mode = LauncherRuntimeMode.BACKGROUND_ONLY
            self.record_event("background_mode_kept", {"origin": origin.value})
        elif action is LifecycleAction.STOP or action is LifecycleAction.QUIT_COMPLETELY:
            shutdown = self.shutdown().to_dict()
        elif action is LifecycleAction.FORCE_QUIT:
            shutdown = self.shutdown(grace_window_seconds=0.01, force_after_seconds=0.01).to_dict()
        elif action is LifecycleAction.RESTART:
            shutdown = self.shutdown().to_dict()
            self._transition(LifecycleState.STARTING, event="restart_requested", payload={"origin": origin.value})
        elif action is LifecycleAction.CRASH_RECOVER:
            recovery = self.recover_from_crash().to_dict()
            if recovery["recovered_state"] in {"crash_detected", "partial"}:
                self._transition(LifecycleState.CRASHED_RECOVERING, event="crash_recovery_requested", payload=recovery)

        return LifecycleCommandResult(
            accepted=True,
            action=action.value,
            decision=decision,
            status=self.current_status(),
            shutdown=shutdown,
            recovery=recovery,
        )

    @staticmethod
    def _command_denial_reason(
        action: LifecycleAction,
        origin: LifecycleCommandOrigin,
        admin_equivalent: bool,
    ) -> str | None:
        if action.value not in _ALLOWLISTED_LIFECYCLE_ACTIONS:
            return f"{action.value} is not allowlisted"
        if origin is LifecycleCommandOrigin.TAURI:
            return None if admin_equivalent else "Tauri lifecycle command missing admin-equivalent consent"
        if origin is LifecycleCommandOrigin.COMPATIBILITY_API:
            return None if admin_equivalent else "compatibility route missing admin-equivalent guard"
        if origin is LifecycleCommandOrigin.BROWSER_FALLBACK and action.value in _TRANSITIONAL_BROWSER_ACTIONS:
            return None
        if action.value in _ADMIN_ACTIONS:
            return f"{action.value} requires Tauri or admin-equivalent compatibility route"
        return f"origin {origin.value} is not authorized for {action.value}"


def get_app_lifecycle() -> AppLifecycleController:
    """Execute the get app lifecycle operation.

    Returns:
        Resolved app lifecycle value.
    """
    global _INSTANCE
    if _INSTANCE is None:
        with _INSTANCE_LOCK:
            if _INSTANCE is None:
                _INSTANCE = AppLifecycleController()
    return _INSTANCE


def reset_app_lifecycle_for_test() -> None:
    """Execute the reset app lifecycle for test operation."""
    global _INSTANCE
    with _INSTANCE_LOCK:
        _INSTANCE = None


__all__ = [
    "AppLifecycleController",
    "CrashRecoveryReport",
    "LifecycleState",
    "ShutdownReport",
    "get_app_lifecycle",
    "reset_app_lifecycle_for_test",
]
