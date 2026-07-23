"""Local runtime onboarding for LM Studio, Jan, and Open WebUI.

The onboarding surface is intentionally fail-closed: network probes, hardware
fit, scheduler readiness, state persistence, and config writeback all return
typed blockers instead of silently treating unknown state as ready. The only
owned write path is the append-only JSONL state file under
``outputs/workbench/onboarding``. Config persistence is delegated to the
DEPS-00 writeback hook when that sibling pack registers it.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import httpx

from vetinari.agents.contracts import OutcomeSignal, Provenance, ToolEvidence
from vetinari.inference import ComputeTarget as ComputeTarget
from vetinari.models.model_registry import ModelRegistry
from vetinari.receipts.record import WorkReceipt, WorkReceiptKind
from vetinari.receipts.store import WorkReceiptStore
from vetinari.runtime.workbench_scheduler import WorkbenchScheduler
from vetinari.types import AgentType, EvidenceBasis
from vetinari.workbench.local_runtime_contracts import (
    BlockerKind,
    HardwareFit,
    LocalRuntimeBlocker,
    LocalRuntimeKind,
    LocalRuntimeOnboardingError,
    LocalRuntimeProbeError,
    LocalRuntimeProbeResult,
    LocalRuntimeWriteback,
    OnboardingReadiness,
)
from vetinari.workbench.local_runtime_probes import (
    _collect_blockers as _collect_blockers,
)
from vetinari.workbench.local_runtime_probes import (
    _compute_hardware_fit as _compute_hardware_fit,
)
from vetinari.workbench.local_runtime_probes import (
    _compute_lane_readiness as _compute_lane_readiness,
)
from vetinari.workbench.local_runtime_probes import (
    _dispatch_probe as _dispatch_probe,
)
from vetinari.workbench.local_runtime_probes import (
    _probe_jan as _probe_jan,
)
from vetinari.workbench.local_runtime_probes import (
    _probe_lmstudio as _probe_lmstudio,
)
from vetinari.workbench.local_runtime_probes import (
    _probe_openwebui as _probe_openwebui,
)
from vetinari.workbench.local_runtime_probes import (
    _utc_now_iso as _utc_now_iso,
)
from vetinari.workbench.local_runtime_probes import (
    detect_system_resources as detect_system_resources,
)
from vetinari.workbench.local_runtime_serialization import json_safe
from vetinari.workbench.security_primitives import BoundaryError, assert_trusted_url
from vetinari.workbench.spine_consumers import record_asset_written, record_run_completed

logger = logging.getLogger(__name__)


_INSTANCE: LocalRuntimeOnboarding | None = None
_INSTANCE_LOCK = threading.Lock()
_DEFAULT_STATE_DIR = Path("outputs") / "workbench" / "onboarding"
_STATE_FILENAME = "onboarding_state.jsonl"
_WRITEBACK_FILENAME = "deps_00_writeback.jsonl"
_DEFAULT_LMSTUDIO_BASE = os.environ.get("VETINARI_LMSTUDIO_BASE_URL", "http://127.0.0.1:1234")
_DEFAULT_JAN_BASE = os.environ.get("VETINARI_JAN_BASE_URL", "http://127.0.0.1:1337")
_DEFAULT_OPENWEBUI_BASE = os.environ.get("VETINARI_OPENWEBUI_BASE_URL", "http://127.0.0.1:3000")
_ALLOWED_LOCAL_RUNTIME_HOSTS = frozenset({"127.0.0.1", "localhost"})
_PROBE_TIMEOUT_S = 5.0
_HARDWARE_FIT_PROBE_MAX_TOKENS = 16


def _append_default_writeback(payload: LocalRuntimeWriteback) -> None:
    path = _DEFAULT_STATE_DIR / _WRITEBACK_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(json_safe(payload), sort_keys=True) + "\n"
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line)
        fh.flush()
        os.fsync(fh.fileno())
    # spine_consumers invokes get_spine() and absorbs observability failures.
    record_asset_written(
        asset_id="local-runtime-writeback",
        kind="tool",
        project_id="default",
        path=str(path),
        redact_fields=["path"],
    )


def register_default_local_runtime_writeback_hook() -> None:
    """Install the DEPS-00 local runtime writeback hook used by the API surface."""
    LocalRuntimeOnboarding.register_writeback_hook(_append_default_writeback)


class LocalRuntimeOnboarding:
    """Probe local runtimes, compute readiness, append state, and emit receipts."""

    _writeback_hook: Callable[[LocalRuntimeWriteback], None] | None = None
    _writeback_hook_lock = threading.Lock()

    def __init__(
        self,
        state_dir: Path | str = _DEFAULT_STATE_DIR,
        *,
        http_client: httpx.Client | None = None,
        model_registry: ModelRegistry | None = None,
        scheduler: WorkbenchScheduler | None = None,
        receipt_store: WorkReceiptStore | None = None,
        base_urls: Mapping[LocalRuntimeKind, str] | None = None,
    ) -> None:
        self._state_dir = Path(state_dir)
        self._state_path = self._state_dir / _STATE_FILENAME
        self._http_client = http_client or httpx.Client()
        self._model_registry = model_registry
        self._scheduler = scheduler
        self._receipt_store = receipt_store
        self._base_urls = dict(base_urls or {})
        self._refresh_lock = threading.RLock()
        self._initialise_state()

    @classmethod
    def register_writeback_hook(cls, hook: Callable[[LocalRuntimeWriteback], None] | None) -> None:
        """Register the DEPS-00 config writeback hook."""
        with cls._writeback_hook_lock:
            cls._writeback_hook = hook

    def health(self) -> OnboardingReadiness:
        """Return current readiness by performing a dry-run refresh."""
        return self.refresh(dry_run=True)

    def refresh(self, *, dry_run: bool = False) -> OnboardingReadiness:
        """Refresh runtime readiness and append a receipt-backed JSONL record.

        Returns:
            OnboardingReadiness value produced by refresh().
        """
        with self._refresh_lock:
            probes = tuple(self.probe_runtime(kind) for kind in LocalRuntimeKind)
            registry = self._model_registry or ModelRegistry.get_instance()
            registry_models = {entry.model_id: entry for entry in registry.get_available_models()}
            discovered_ids = {
                str(model.get("id") or model.get("model") or model.get("name"))
                for probe in probes
                for model in probe.discovered_models
            }
            hardware_fit_by_model = {
                model_id: _compute_hardware_fit(entry)
                for model_id, entry in registry_models.items()
                if not discovered_ids or model_id in discovered_ids
            }
            scheduler_lanes_ready = _compute_lane_readiness(self._scheduler)
            hook = self.__class__._writeback_hook
            blockers = _collect_blockers(
                probes=probes,
                hardware_fit_by_model=hardware_fit_by_model,
                scheduler_lanes_ready=scheduler_lanes_ready,
                deps_00_hook_present=hook is not None,
                dry_run=dry_run,
            )
            readiness = OnboardingReadiness(
                probes=probes,
                hardware_fit_by_model=hardware_fit_by_model,
                scheduler_lanes_ready=scheduler_lanes_ready,
                blockers=blockers,
                deps_00_hook_present=hook is not None,
                dry_run=dry_run,
                state_path=str(self._state_path),
            )
            self._append_state({"kind": "refresh", "readiness": readiness, "recorded_at_utc": _utc_now_iso()})
            # WorkReceiptStore.append is reached through _emit_receipt while self._refresh_lock is held.
            for probe in probes:
                self._emit_receipt(f"refresh:{probe.runtime_kind.value}", probe)
            if hook is not None and not dry_run:
                self._invoke_writeback_hook(hook, probes, hardware_fit_by_model, blockers)
            return readiness

    def smoke_test(
        self,
        *,
        runtime: LocalRuntimeKind,
        sample_prompt: str = "ping",
    ) -> dict[str, Any]:
        """POST a tiny chat-completions request and emit one receipt.

        Returns:
            dict[str, Any] value produced by smoke_test().
        """
        runtime = LocalRuntimeKind(runtime)
        with self._refresh_lock:
            base_url = (
                self._base_urls.get(runtime)
                or {
                    LocalRuntimeKind.LMSTUDIO: _DEFAULT_LMSTUDIO_BASE,
                    LocalRuntimeKind.JAN: _DEFAULT_JAN_BASE,
                    LocalRuntimeKind.OPENWEBUI: _DEFAULT_OPENWEBUI_BASE,
                }[runtime]
            )
            try:
                base_url = assert_trusted_url(
                    base_url,
                    allowed_schemes=frozenset({"http", "https"}),
                    allowed_hosts=_ALLOWED_LOCAL_RUNTIME_HOSTS,
                )
            except BoundaryError as exc:
                logger.warning("Rejected local runtime base URL for %s: %s", runtime.value, exc)
                return {
                    "runtime": runtime.value,
                    "success": False,
                    "status": None,
                    "error": exc.reason,
                    "base_url": base_url,
                }
            path = "/api/chat/completions" if runtime is LocalRuntimeKind.OPENWEBUI else "/v1/chat/completions"
            started = time.perf_counter()
            error: str | None = None
            success = False
            status: int | None = None
            try:
                response = self._http_client.post(
                    f"{base_url.rstrip('/')}{path}",
                    timeout=_PROBE_TIMEOUT_S,
                    json={
                        "model": "local-runtime-smoke",
                        "messages": [{"role": "user", "content": sample_prompt}],
                        "max_tokens": 1,
                    },
                )
                status = response.status_code
                success = status < 400
                if not success:
                    error = f"HTTP {status}"
            except httpx.HTTPError as exc:
                error = f"{exc.__class__.__name__}: {exc}"
            result = {
                "success": success,
                "latency_ms": int((time.perf_counter() - started) * 1000),
                "runtime": runtime.value,
                "http_status": status,
                "error": error,
                "checked_at_utc": _utc_now_iso(),
            }
            self._append_state({"kind": "smoke_test", "result": result, "recorded_at_utc": _utc_now_iso()})
            self._emit_receipt("smoke_test", result)
            return result

    def probe_runtime(self, runtime: LocalRuntimeKind) -> LocalRuntimeProbeResult:
        """Probe one local runtime endpoint."""
        return _dispatch_probe(LocalRuntimeKind(runtime), self._http_client, base_urls=self._base_urls)

    def _initialise_state(self) -> None:
        try:
            if self._state_dir.exists() and not self._state_dir.is_dir():
                raise LocalRuntimeOnboardingError(f"state path is not a directory: {self._state_dir}")
            self._state_dir.mkdir(parents=True, exist_ok=True)
            if not self._state_path.exists():
                self._state_path.touch()
                return
            raw = self._state_path.read_bytes()
            if raw and not raw.endswith(b"\n"):
                raise LocalRuntimeOnboardingError("onboarding_state.jsonl has a truncated final line")
            for line_no, line in enumerate(raw.decode("utf-8").splitlines(), start=1):
                if line.strip():
                    try:
                        json.loads(line)
                    except json.JSONDecodeError as exc:
                        raise LocalRuntimeOnboardingError(
                            f"onboarding_state.jsonl JSONL parse failed at line {line_no}: {exc}"
                        ) from exc
        except LocalRuntimeOnboardingError:
            raise
        except OSError as exc:
            raise LocalRuntimeOnboardingError(f"onboarding state directory is unreadable: {exc}") from exc

    def _append_state(self, payload: Mapping[str, Any]) -> None:
        try:
            line = json.dumps(json_safe(dict(payload)), sort_keys=True) + "\n"
            with self._state_path.open("a", encoding="utf-8") as fh:
                fh.write(line)
                fh.flush()
                os.fsync(fh.fileno())
            # spine_consumers invokes get_spine() and absorbs observability failures.
            record_run_completed(
                run_id=str(payload.get("run_id", "local-runtime-onboarding")),
                kind="agent_run",
                project_id=str(payload.get("project_id", "default")),
            )
        except OSError as exc:
            raise LocalRuntimeOnboardingError(f"failed to append onboarding state: {exc}") from exc

    def _emit_receipt(self, action: str, payload: Any) -> None:
        kind = getattr(WorkReceiptKind, "SPINE_EVENT", WorkReceiptKind.RELEASE_STEP)
        passed = _receipt_payload_passed(payload)
        evidence = ToolEvidence(
            tool_name="local-runtime-onboarding",
            command=f"LocalRuntimeOnboarding.{action}",
            exit_code=0 if passed else 1,
            stdout_snippet=json.dumps(json_safe(payload), sort_keys=True)[:2000],
            passed=passed,
        )
        outcome = OutcomeSignal(
            passed=passed,
            score=1.0 if passed else 0.0,
            basis=EvidenceBasis.TOOL_EVIDENCE,
            tool_evidence=(evidence,),
            provenance=Provenance(source="workbench-onboarding", timestamp_utc=_utc_now_iso(), tool_name=action),
        )
        receipt = WorkReceipt(
            project_id="workbench-onboarding",
            agent_id="local-runtime-onboarding",
            agent_type=AgentType.WORKBENCH,
            kind=kind,
            outcome=outcome,
            inputs_summary=f"local runtime onboarding {action}",
            outputs_summary="receipt-backed onboarding state updated",
        )
        store = self._receipt_store or WorkReceiptStore()
        store.append(receipt)

    @staticmethod
    def _invoke_writeback_hook(
        hook: Callable[[LocalRuntimeWriteback], None],
        probes: tuple[LocalRuntimeProbeResult, ...],
        hardware_fit_by_model: Mapping[str, HardwareFit],
        blockers: tuple[LocalRuntimeBlocker, ...],
    ) -> None:
        port_blockers = {
            blocker.runtime_kind: blocker for blocker in blockers if blocker.kind is BlockerKind.PORT_COLLISION
        }
        cpu_offload = tuple(model_id for model_id, fit in hardware_fit_by_model.items() if fit.requires_cpu_offload)
        for probe in probes:
            if not probe.reachable:
                continue
            hook(
                LocalRuntimeWriteback(
                    runtime_kind=probe.runtime_kind,
                    endpoint=probe.base_url,
                    discovered_models=probe.discovered_models,
                    requires_cpu_offload_models=cpu_offload,
                    port_in_use_blocker=port_blockers.get(probe.runtime_kind),
                    provenance={"source": "workbench-onboarding", "recorded_at_utc": _utc_now_iso()},
                )
            )


def get_local_runtime_onboarding() -> LocalRuntimeOnboarding:
    """Return the process singleton using double-checked locking.

    Returns:
        Resolved local runtime onboarding value.
    """
    global _INSTANCE
    if _INSTANCE is None:
        with _INSTANCE_LOCK:
            if _INSTANCE is None:
                _INSTANCE = LocalRuntimeOnboarding()
    return _INSTANCE


def _receipt_payload_passed(payload: Any) -> bool:
    if isinstance(payload, LocalRuntimeProbeResult):
        return bool(payload.reachable and not payload.error)
    if isinstance(payload, OnboardingReadiness):
        return not payload.blockers and all(probe.reachable and not probe.error for probe in payload.probes)
    if isinstance(payload, Mapping):
        if "success" in payload:
            return bool(payload.get("success"))
        if "passed" in payload:
            return bool(payload.get("passed"))
        if payload.get("error") or payload.get("blockers"):
            return False
    return True


def reset_local_runtime_onboarding_for_test() -> None:
    """Clear singleton and DEPS-00 hook for isolated tests."""
    global _INSTANCE
    with _INSTANCE_LOCK:
        _INSTANCE = None
    LocalRuntimeOnboarding.register_writeback_hook(None)


__all__ = [
    "BlockerKind",
    "HardwareFit",
    "LocalRuntimeBlocker",
    "LocalRuntimeKind",
    "LocalRuntimeOnboarding",
    "LocalRuntimeOnboardingError",
    "LocalRuntimeProbeError",
    "LocalRuntimeProbeResult",
    "LocalRuntimeWriteback",
    "OnboardingReadiness",
    "get_local_runtime_onboarding",
    "register_default_local_runtime_writeback_hook",
    "reset_local_runtime_onboarding_for_test",
]
