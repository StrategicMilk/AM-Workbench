"""Private AI appliance runtime cockpit.

This module aggregates local workbench readiness into an operator-facing
snapshot. It intentionally does not import inference routers or adapter
implementations; unknown local state is represented as degraded/action-required
guidance instead of optimistic readiness.
"""

from __future__ import annotations

import importlib.util
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

import yaml

from vetinari.api.responses import json_safe as _json_safe
from vetinari.constants import PROJECT_ROOT

logger = logging.getLogger(__name__)


_DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "workbench" / "private_ai_appliance.yaml"


class PrivateAIApplianceError(RuntimeError):
    """Base error for runtime cockpit assembly."""


class PrivateAIApplianceConfigError(PrivateAIApplianceError):
    """Raised when support-matrix config cannot be trusted."""


class SupportMatrixStatus(str, Enum):
    """Support-matrix state surfaced to the operator."""

    UNSUPPORTED = "unsupported"
    EXPERIMENTAL = "experimental"
    VALIDATED = "validated"
    PROMOTION_ELIGIBLE = "promotion-eligible"
    ACTION_REQUIRED = "action-required"
    DEGRADED = "degraded"


class OperatorAction(str, Enum):
    """Recommended operator action for a support-matrix state."""

    USE_CLOUD_OR_INSTALL_GPU = "use_cloud_or_install_gpu"
    KEEP_LOCAL_RUNTIME_PRIMARY = "keep_local_runtime_primary"
    PROMOTE_LOCAL_FIRST_ROUTING = "promote_local_first_routing"
    VALIDATE_RUNTIME_BEFORE_PROMOTION = "validate_runtime_before_promotion"
    INSTALL_OR_START_LOCAL_RUNTIME = "install_or_start_local_runtime"
    KEEP_CLOUD_FALLBACK_AS_ESCAPE_HATCH = "keep_cloud_fallback_as_escape_hatch"
    CHECK_HARDWARE_PROBE = "check_hardware_probe"
    CHECK_RUNTIME_HEALTH = "check_runtime_health"
    DRAIN_OR_PAUSE_QUEUE = "drain_or_pause_queue"
    RESTORE_MODEL_STORE = "restore_model_store"
    ENABLE_CLOUD_FALLBACK = "enable_cloud_fallback"


@dataclass(frozen=True, slots=True)
class HardwareFacts:
    """Hardware and substrate facts used by the support matrix."""

    gpu_count: int | None
    gpu_vram_gb: int | None
    cpu_cores: int | None
    ram_gb: int | None
    storage_free_gb: int | None
    driver_status: str
    substrate: str
    wsl_ready: bool | None = None

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"HardwareFacts(gpu_count={self.gpu_count!r}, gpu_vram_gb={self.gpu_vram_gb!r}, cpu_cores={self.cpu_cores!r})"


@dataclass(frozen=True, slots=True)
class RuntimeHealth:
    """Local runtime reachability state."""

    runtime_present: bool | None
    runtime_name: str
    health_status: str
    detail: str = ""

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"RuntimeHealth(runtime_present={self.runtime_present!r}, runtime_name={self.runtime_name!r}, health_status={self.health_status!r})"


@dataclass(frozen=True, slots=True)
class QueuePressure:
    """Workbench scheduler pressure shown in the cockpit."""

    active: int | None
    queued: int | None
    capacity: int | None
    saturated: bool | None

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"QueuePressure(active={self.active!r}, queued={self.queued!r}, capacity={self.capacity!r})"


@dataclass(frozen=True, slots=True)
class ModelStoreState:
    """Local model-store readiness and loaded-model state."""

    model_store_present: bool | None
    available_models: int | None
    loaded_model: str | None
    load_state: str

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"ModelStoreState(model_store_present={self.model_store_present!r}, available_models={self.available_models!r}, loaded_model={self.loaded_model!r})"


@dataclass(frozen=True, slots=True)
class RoutingPosture:
    """Local-versus-cloud routing posture."""

    local_enabled: bool | None
    cloud_fallback_enabled: bool | None
    active_route: str


@dataclass(frozen=True, slots=True)
class RuntimeFacts:
    """All facts consumed by the private AI appliance support matrix."""

    hardware: HardwareFacts
    runtime: RuntimeHealth
    queue: QueuePressure
    model_store: ModelStoreState
    routing: RoutingPosture

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"RuntimeFacts(hardware={self.hardware!r}, runtime={self.runtime!r}, queue={self.queue!r})"


@dataclass(frozen=True, slots=True)
class SupportMatrixRow:
    """One support-matrix rule loaded from config."""

    id: str
    label: str
    status: SupportMatrixStatus
    operator_action: OperatorAction
    reason: str
    conditions: dict[str, str]

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"SupportMatrixRow(id={self.id!r}, label={self.label!r}, status={self.status!r})"


@dataclass(frozen=True, slots=True)
class SupportMatrixEvaluation:
    """Evaluation result for one rule or fail-closed degradation cell."""

    row_id: str
    label: str
    status: SupportMatrixStatus
    operator_action: OperatorAction
    reason: str
    matched: bool

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"SupportMatrixEvaluation(row_id={self.row_id!r}, label={self.label!r}, status={self.status!r})"


@dataclass(frozen=True, slots=True)
class PrivateAIApplianceConfig:
    """Parsed private AI appliance support-matrix config."""

    support_matrix: tuple[SupportMatrixRow, ...]
    path: str


@dataclass(frozen=True, slots=True)
class RuntimeCockpitSnapshot:
    """Operator-facing runtime cockpit snapshot."""

    generated_at_utc: str
    overall_status: SupportMatrixStatus
    recommended_actions: tuple[OperatorAction, ...]
    support_matrix: tuple[SupportMatrixEvaluation, ...]
    hardware: HardwareFacts
    runtime: RuntimeHealth
    queue: QueuePressure
    model_store: ModelStoreState
    routing: RoutingPosture
    degradation_reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe snapshot dictionary."""
        return _json_safe(self)

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"RuntimeCockpitSnapshot(generated_at_utc={self.generated_at_utc!r}, overall_status={self.overall_status!r}, recommended_actions={self.recommended_actions!r})"


def load_private_ai_appliance_config(path: str | Path = _DEFAULT_CONFIG_PATH) -> PrivateAIApplianceConfig:
    """Load private AI appliance support rules.

    Raises:
            PrivateAIApplianceConfigError: If the config is missing, unreadable, or
                does not contain a non-empty support matrix.

    Returns:
        Resolved private ai appliance config value.
    """
    config_path = Path(path)
    if not config_path.is_file():
        raise PrivateAIApplianceConfigError(f"private AI appliance config not found: {config_path}")
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise PrivateAIApplianceConfigError(f"private AI appliance config unreadable: {config_path}") from exc
    if not isinstance(raw, dict):
        raise PrivateAIApplianceConfigError("private AI appliance config must be a mapping")
    rows = raw.get("support_matrix")
    if not isinstance(rows, list) or not rows:
        raise PrivateAIApplianceConfigError("private AI appliance config must define a non-empty support_matrix")
    parsed: list[SupportMatrixRow] = []
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            raise PrivateAIApplianceConfigError(f"support_matrix row {index} must be a mapping")
        try:
            row_id = str(row["id"])
            label = str(row["label"])
            status = SupportMatrixStatus(str(row["status"]))
            action = OperatorAction(str(row["operator_action"]))
            reason = str(row["reason"])
            conditions = {str(key): str(value) for key, value in dict(row["conditions"]).items()}
        except (KeyError, TypeError, ValueError) as exc:
            raise PrivateAIApplianceConfigError(f"support_matrix row {index} is invalid") from exc
        if not conditions:
            raise PrivateAIApplianceConfigError(f"support_matrix row {row_id} must declare conditions")
        parsed.append(
            SupportMatrixRow(
                id=row_id,
                label=label,
                status=status,
                operator_action=action,
                reason=reason,
                conditions=conditions,
            )
        )
    return PrivateAIApplianceConfig(support_matrix=tuple(parsed), path=str(config_path))


def support_status_to_operator_action(status: SupportMatrixStatus | str) -> OperatorAction:
    """Return the default operator action for a support status.

    Returns:
        OperatorAction value produced by support_status_to_operator_action().
    """
    status = SupportMatrixStatus(status)
    return {
        SupportMatrixStatus.UNSUPPORTED: OperatorAction.USE_CLOUD_OR_INSTALL_GPU,
        SupportMatrixStatus.EXPERIMENTAL: OperatorAction.VALIDATE_RUNTIME_BEFORE_PROMOTION,
        SupportMatrixStatus.VALIDATED: OperatorAction.KEEP_LOCAL_RUNTIME_PRIMARY,
        SupportMatrixStatus.PROMOTION_ELIGIBLE: OperatorAction.PROMOTE_LOCAL_FIRST_ROUTING,
        SupportMatrixStatus.ACTION_REQUIRED: OperatorAction.INSTALL_OR_START_LOCAL_RUNTIME,
        SupportMatrixStatus.DEGRADED: OperatorAction.CHECK_RUNTIME_HEALTH,
    }[status]


class RuntimeCockpit:
    """Build fail-closed private AI appliance snapshots."""

    def __init__(self, config_path: str | Path = _DEFAULT_CONFIG_PATH) -> None:
        self._config_path = Path(config_path)

    def snapshot(self, facts: RuntimeFacts | None = None) -> RuntimeCockpitSnapshot:
        """Return a cockpit snapshot, marking unknown state as not validated.

        Returns:
            RuntimeCockpitSnapshot value produced by snapshot().
        """
        config = load_private_ai_appliance_config(self._config_path)
        facts = facts or self._collect_read_only_facts()
        evaluations = list(self._evaluate_config_rows(config.support_matrix, facts))
        degradation_reasons: list[str] = []
        issue_evaluations = self._fail_closed_evaluations(facts, degradation_reasons)
        evaluations.extend(issue_evaluations)
        engine_evaluation = self._engine_support_evaluation(degradation_reasons)
        evaluations.append(engine_evaluation)
        if not evaluations:
            degradation_reasons.append("support matrix produced no rows")
            evaluations.append(
                SupportMatrixEvaluation(
                    row_id="support-matrix-empty",
                    label="Support matrix unavailable",
                    status=SupportMatrixStatus.DEGRADED,
                    operator_action=OperatorAction.CHECK_RUNTIME_HEALTH,
                    reason="No support-matrix rows could be evaluated.",
                    matched=True,
                )
            )
        overall_status = _worst_status(item.status for item in evaluations if item.matched)
        actions = tuple(dict.fromkeys(item.operator_action for item in evaluations if item.matched))
        return RuntimeCockpitSnapshot(
            generated_at_utc=datetime.now(timezone.utc).isoformat(),
            overall_status=overall_status,
            recommended_actions=actions,
            support_matrix=tuple(evaluations),
            hardware=facts.hardware,
            runtime=facts.runtime,
            queue=facts.queue,
            model_store=facts.model_store,
            routing=facts.routing,
            degradation_reasons=tuple(degradation_reasons),
        )

    @staticmethod
    def _collect_read_only_facts() -> RuntimeFacts:
        gpu_count = _optional_int_env("VETINARI_GPU_COUNT")
        runtime_present = _optional_bool_env("VETINARI_LOCAL_RUNTIME_PRESENT")
        cloud_fallback = _optional_bool_env("VETINARI_CLOUD_FALLBACK_ENABLED")
        model_store = Path(os.environ.get("VETINARI_MODEL_STORE", "models")).exists()
        scheduler_config_present = Path("config/workbench_scheduler.yaml").is_file()
        backend_installer_present = importlib.util.find_spec("vetinari.setup.backend_installer") is not None
        runtime_detail = "local runtime probe not run"
        if not backend_installer_present:
            runtime_detail = "backend installer unavailable"
        return RuntimeFacts(
            hardware=HardwareFacts(
                gpu_count=gpu_count,
                gpu_vram_gb=_optional_int_env("VETINARI_GPU_VRAM_GB"),
                cpu_cores=os.cpu_count(),
                ram_gb=_optional_int_env("VETINARI_RAM_GB"),
                storage_free_gb=_optional_int_env("VETINARI_STORAGE_FREE_GB"),
                driver_status=os.environ.get("VETINARI_DRIVER_STATUS", "unknown"),
                substrate=os.environ.get("VETINARI_RUNTIME_SUBSTRATE", "windows-native"),
                wsl_ready=_optional_bool_env("VETINARI_WSL_READY"),
            ),
            runtime=RuntimeHealth(
                runtime_present=runtime_present,
                runtime_name=os.environ.get("VETINARI_LOCAL_RUNTIME", "unknown"),
                health_status="unknown" if runtime_present is None else ("ready" if runtime_present else "absent"),
                detail=runtime_detail,
            ),
            queue=QueuePressure(
                active=None,
                queued=None,
                capacity=None,
                saturated=False if scheduler_config_present else None,
            ),
            model_store=ModelStoreState(
                model_store_present=model_store,
                available_models=None,
                loaded_model=None,
                load_state="unknown",
            ),
            routing=RoutingPosture(
                local_enabled=runtime_present,
                cloud_fallback_enabled=cloud_fallback,
                active_route="unknown",
            ),
        )

    @staticmethod
    def _evaluate_config_rows(
        rows: tuple[SupportMatrixRow, ...], facts: RuntimeFacts
    ) -> tuple[SupportMatrixEvaluation, ...]:
        labels = _fact_labels(facts)
        evaluations: list[SupportMatrixEvaluation] = []
        for row in rows:
            matched = all(labels.get(key) == value for key, value in row.conditions.items())
            evaluations.append(
                SupportMatrixEvaluation(
                    row_id=row.id,
                    label=row.label,
                    status=row.status,
                    operator_action=row.operator_action,
                    reason=row.reason,
                    matched=matched,
                )
            )
        return tuple(evaluations)

    @staticmethod
    def _fail_closed_evaluations(
        facts: RuntimeFacts, degradation_reasons: list[str]
    ) -> tuple[SupportMatrixEvaluation, ...]:
        issues: list[SupportMatrixEvaluation] = []
        if facts.hardware.gpu_count is None:
            degradation_reasons.append("GPU facts unavailable")
            issues.append(_issue("unknown-hardware", "GPU facts unavailable", OperatorAction.CHECK_HARDWARE_PROBE))
        if facts.runtime.runtime_present is None or facts.runtime.health_status == "unknown":
            degradation_reasons.append("local runtime health unknown")
            issues.append(
                _issue("unknown-runtime", "Local runtime health unknown", OperatorAction.CHECK_RUNTIME_HEALTH)
            )
        if facts.queue.saturated is True:
            degradation_reasons.append("workbench queue saturated")
            issues.append(
                _issue(
                    "queue-saturated",
                    "Workbench queue is saturated",
                    OperatorAction.DRAIN_OR_PAUSE_QUEUE,
                    status=SupportMatrixStatus.ACTION_REQUIRED,
                )
            )
        if facts.queue.saturated is None:
            degradation_reasons.append("queue pressure unknown")
            issues.append(_issue("unknown-queue", "Queue pressure unknown", OperatorAction.DRAIN_OR_PAUSE_QUEUE))
        if facts.model_store.model_store_present is False:
            degradation_reasons.append("model store missing")
            issues.append(
                _issue(
                    "model-store-missing",
                    "Model store missing",
                    OperatorAction.RESTORE_MODEL_STORE,
                    status=SupportMatrixStatus.ACTION_REQUIRED,
                )
            )
        if facts.routing.cloud_fallback_enabled is False:
            degradation_reasons.append("cloud fallback disabled")
            issues.append(
                _issue(
                    "cloud-fallback-disabled",
                    "Cloud fallback disabled",
                    OperatorAction.ENABLE_CLOUD_FALLBACK,
                    status=SupportMatrixStatus.ACTION_REQUIRED,
                )
            )
        return tuple(issues)

    @staticmethod
    def _engine_support_evaluation(degradation_reasons: list[str]) -> SupportMatrixEvaluation:
        """Add an operator row from the supervisor's read-only state probe."""
        try:
            from vetinari.engine import get_supervisor
            from vetinari.engine.supervisor import EngineState

            engine = get_supervisor().status()
            if engine.healthy:
                return SupportMatrixEvaluation(
                    row_id="am-engine-ready",
                    label="AM Engine pinned runtime",
                    status=SupportMatrixStatus.VALIDATED,
                    operator_action=OperatorAction.KEEP_LOCAL_RUNTIME_PRIMARY,
                    reason="The pinned AM Engine passed its version and readiness handshake.",
                    matched=True,
                )
            if engine.state is EngineState.VERSION_MISMATCH:
                reason = engine.user_message or "AM Engine version does not match the trusted release."
                degradation_reasons.append(reason)
                return _issue(
                    "am-engine-version-mismatch",
                    reason,
                    OperatorAction.INSTALL_OR_START_LOCAL_RUNTIME,
                    status=SupportMatrixStatus.ACTION_REQUIRED,
                )
            if engine.state is EngineState.DEGRADED:
                reason = engine.user_message or "AM Engine exhausted its bounded restart policy."
                degradation_reasons.append(reason)
                return _issue("am-engine-degraded", reason, OperatorAction.CHECK_RUNTIME_HEALTH)
            return SupportMatrixEvaluation(
                row_id="am-engine-on-demand",
                label="AM Engine is stopped until inference is requested",
                status=SupportMatrixStatus.VALIDATED,
                operator_action=OperatorAction.KEEP_LOCAL_RUNTIME_PRIMARY,
                reason="On-demand lifecycle policy is active; health polling does not start the engine.",
                matched=True,
            )
        except Exception as exc:
            reason = f"AM Engine status is unreadable ({type(exc).__name__})"
            logger.warning("%s: %s", reason, exc)
            degradation_reasons.append(reason)
            return _issue("am-engine-status-unreadable", reason, OperatorAction.CHECK_RUNTIME_HEALTH)


def _issue(
    row_id: str,
    label: str,
    action: OperatorAction,
    *,
    status: SupportMatrixStatus = SupportMatrixStatus.DEGRADED,
) -> SupportMatrixEvaluation:
    return SupportMatrixEvaluation(
        row_id=row_id,
        label=label,
        status=status,
        operator_action=action,
        reason=label,
        matched=True,
    )


def _fact_labels(facts: RuntimeFacts) -> dict[str, str]:
    gpu_count = facts.hardware.gpu_count
    if gpu_count is None:
        hardware_profile = "unknown"
    elif gpu_count <= 0:
        hardware_profile = "cpu-only"
    elif gpu_count == 1:
        hardware_profile = "single-gpu"
    else:
        hardware_profile = "multi-gpu"
    if facts.runtime.runtime_present is None:
        local_runtime = "unknown"
    else:
        local_runtime = "present" if facts.runtime.runtime_present else "absent"
    if facts.routing.cloud_fallback_enabled is None:
        cloud_fallback = "unknown"
    else:
        cloud_fallback = "enabled" if facts.routing.cloud_fallback_enabled else "disabled"
    if facts.queue.saturated is None:
        queue = "unknown"
    else:
        queue = "saturated" if facts.queue.saturated else "available"
    if facts.model_store.model_store_present is None:
        model_store = "unknown"
    else:
        model_store = "present" if facts.model_store.model_store_present else "missing"
    substrate = facts.hardware.substrate
    if substrate in {"wsl", "linux", "wsl-linux"} and facts.hardware.wsl_ready is not False:
        substrate = "wsl-linux"
    return {
        "hardware_profile": hardware_profile,
        "substrate": substrate,
        "local_runtime": local_runtime,
        "cloud_fallback": cloud_fallback,
        "queue": queue,
        "model_store": model_store,
    }


def _worst_status(statuses: Any) -> SupportMatrixStatus:
    order = {
        SupportMatrixStatus.UNSUPPORTED: 60,
        SupportMatrixStatus.ACTION_REQUIRED: 50,
        SupportMatrixStatus.DEGRADED: 40,
        SupportMatrixStatus.EXPERIMENTAL: 30,
        SupportMatrixStatus.VALIDATED: 20,
        SupportMatrixStatus.PROMOTION_ELIGIBLE: 10,
    }
    worst = SupportMatrixStatus.DEGRADED
    worst_score = -1
    for status in statuses:
        score = order[SupportMatrixStatus(status)]
        if score > worst_score:
            worst = SupportMatrixStatus(status)
            worst_score = score
    return worst


def _optional_int_env(name: str) -> int | None:
    value = os.environ.get(name)
    if value is None or value == "":
        return None
    try:
        return int(value)
    except ValueError:
        logger.warning("Handled recoverable failure before fallback.", exc_info=True)
        return None


def _optional_bool_env(name: str) -> bool | None:
    value = os.environ.get(name)
    if value is None or value == "":
        return None
    return value.lower() in {"1", "true", "yes", "on"}


__all__ = [
    "HardwareFacts",
    "ModelStoreState",
    "OperatorAction",
    "PrivateAIApplianceConfig",
    "PrivateAIApplianceConfigError",
    "PrivateAIApplianceError",
    "QueuePressure",
    "RoutingPosture",
    "RuntimeCockpit",
    "RuntimeCockpitSnapshot",
    "RuntimeFacts",
    "RuntimeHealth",
    "SupportMatrixEvaluation",
    "SupportMatrixRow",
    "SupportMatrixStatus",
    "load_private_ai_appliance_config",
    "support_status_to_operator_action",
]
