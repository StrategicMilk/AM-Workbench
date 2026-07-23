"""Durable Workbench run/session kernel runtime."""

from __future__ import annotations

import os
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from vetinari.constants import OUTPUTS_DIR, PROJECT_ROOT
from vetinari.workbench.run_kernel_lifecycle import RunKernelLifecycleMixin
from vetinari.workbench.run_kernel_persistence import RunKernelPersistenceMixin
from vetinari.workbench.session_kernel import RecoveryAction, RunKernelError

CONFIG_PATH = PROJECT_ROOT / "config" / "workbench" / "run_kernel.yaml"
DEFAULT_RUN_KERNEL_STATE_DIR = OUTPUTS_DIR / "workbench" / "spine" / "run-kernel"
_LEGACY_RUN_KERNEL_STATE_DIR = OUTPUTS_DIR / "workbench" / "run-kernel"
_SERVICE_CACHE_LOCK = threading.Lock()
_SERVICE_CACHE: dict[tuple[str, int, str, bool, int], Any] = {}
_RUN_KERNEL_SPINE_CONSUMER_HELPERS = ("record_run_completed", "record_trace_written")


@dataclass(frozen=True, slots=True)
class RunKernelConfig:
    """Operator-tunable run-kernel persistence settings."""

    state_dir: Path
    heartbeat_timeout_seconds: int = 300
    stale_heartbeat_strategy: RecoveryAction = RecoveryAction.REAP
    require_sealed_checkpoint_for_resume: bool = True
    event_retention_count: int = 256

    def __post_init__(self) -> None:
        if self.heartbeat_timeout_seconds <= 0:
            raise RunKernelError("heartbeat-timeout-invalid", "heartbeat timeout must be positive")
        if self.event_retention_count < 1:
            raise RunKernelError("event-retention-invalid", "event retention must be positive")
        if self.stale_heartbeat_strategy not in {RecoveryAction.REAP, RecoveryAction.BLOCK}:
            raise RunKernelError("stale-strategy-invalid", "strategy must be reap or block")

    def __repr__(self) -> str:
        return f"RunKernelConfig(state_dir={self.state_dir!r}, heartbeat_timeout_seconds={self.heartbeat_timeout_seconds!r}, stale_heartbeat_strategy={self.stale_heartbeat_strategy!r})"


def load_run_kernel_config(
    path: str | Path = CONFIG_PATH,
    *,
    state_dir_override: str | Path | None = None,
) -> RunKernelConfig:
    """Load run-kernel config from YAML plus an optional test/runtime override.

    Returns:
        Resolved run kernel config value.

    Raises:
        Exception: Propagates validation or runtime failures from the underlying operation.
    """
    config_path = Path(path)
    raw: dict[str, Any] = {}
    if config_path.exists():
        loaded = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        if not isinstance(loaded, dict):
            raise RunKernelError("config-invalid", "run kernel config must be a mapping")
        raw = loaded
    state_dir = Path(
        state_dir_override or os.environ.get("VETINARI_RUN_KERNEL_STATE_DIR", "") or raw.get("state_dir", "")
    )
    if not str(state_dir):
        state_dir = DEFAULT_RUN_KERNEL_STATE_DIR
    if state_dir == _LEGACY_RUN_KERNEL_STATE_DIR:
        state_dir = DEFAULT_RUN_KERNEL_STATE_DIR
    strategy = RecoveryAction(str(raw.get("stale_heartbeat_strategy", RecoveryAction.REAP.value)))
    return RunKernelConfig(
        state_dir=state_dir,
        heartbeat_timeout_seconds=int(raw.get("heartbeat_timeout_seconds", 300)),
        stale_heartbeat_strategy=strategy,
        require_sealed_checkpoint_for_resume=bool(raw.get("require_sealed_checkpoint_for_resume", True)),
        event_retention_count=int(raw.get("event_retention_count", 256)),
    )


class RunKernelService(RunKernelLifecycleMixin, RunKernelPersistenceMixin):
    """Compatibility facade for durable run snapshot writes.

    ``RunKernelPersistenceMixin`` emits the helpers listed in
    ``_RUN_KERNEL_SPINE_CONSUMER_HELPERS`` when snapshots and event logs persist.
    """

    def __init__(
        self,
        config: RunKernelConfig | None = None,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._config = config or load_run_kernel_config()
        self._now = now or (lambda: datetime.now(UTC))
        self._lock = threading.RLock()

    @property
    def state_dir(self) -> Path:
        return self._config.state_dir


def service_from_environment() -> RunKernelService:
    """Build a run-kernel service using config and environment overrides.

    Returns:
        RunKernelService value produced by service_from_environment().
    """
    config = load_run_kernel_config()
    key = (
        str(config.state_dir.resolve()),
        config.heartbeat_timeout_seconds,
        config.stale_heartbeat_strategy.value,
        config.require_sealed_checkpoint_for_resume,
        config.event_retention_count,
    )
    with _SERVICE_CACHE_LOCK:
        service = _SERVICE_CACHE.get(key)
        if service is None:
            service = RunKernelService(config)
            _SERVICE_CACHE[key] = service
        return service


__all__ = ["RunKernelConfig", "RunKernelService", "load_run_kernel_config", "service_from_environment"]
