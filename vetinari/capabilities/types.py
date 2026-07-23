"""Types for governed capability install-on-demand flows."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from vetinari.exceptions import CapabilityNotAvailable


class CapabilityKind(str, Enum):
    """Concrete optional capabilities Vetinari can install on demand."""

    LOCAL_LLAMA_CPP = "local_llama_cpp"
    LOCAL_VLLM = "local_vllm"
    LOCAL_SGLANG = "local_sglang"
    LOCAL_COMFYUI = "local_comfyui"
    EMBEDDINGS_LOCAL = "embeddings_local"
    IMAGE_GENERATION = "image_generation"
    DPO_TRAINING = "dpo_training"
    SPECULATIVE_DECODING = "speculative_decoding"
    LLMLINGUA_COMPRESSION = "llmlingua_compression"
    DYNAMIC_BROWSER_SCRAPING = "dynamic_browser_scraping"
    AUDIO_TRANSCRIPTION = "audio_transcription"
    VIDEO_PROCESSING = "video_processing"
    CLOUD_ADAPTERS = "cloud_adapters"
    GUARDRAILS_RUNTIME = "guardrails_runtime"
    OBSERVABILITY_OTLP = "observability_otlp"
    EVAL_WORKFLOW = "eval_workflow"
    RUNTIME_CONTROL = "runtime_control"
    SAFETY_POLICY = "safety_policy"


class CapabilityInstallState(str, Enum):
    """Install lifecycle of a capability on the local host."""

    NOT_INSTALLED = "not_installed"
    INSTALLING = "installing"
    INSTALLED = "installed"
    INSTALL_FAILED = "install_failed"
    UNINSTALLING = "uninstalling"
    DECLINED_FOR_NOW = "declined_for_now"


class CapabilityHealthState(str, Enum):
    """Runtime health after request-time detection has run."""

    UNKNOWN = "unknown"
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    BROKEN = "broken"


class CapabilityRiskLevel(str, Enum):
    """Cost/risk classification surfaced before install."""

    LOW = "low"
    MODERATE = "moderate"
    HIGH = "high"
    CRITICAL = "critical"


class DetectionRuleKind(str, Enum):
    """How the detector checks whether a capability is reachable."""

    IMPORT_PROBE = "import_probe"
    BINARY_PROBE = "binary_probe"
    HTTP_PROBE = "http_probe"
    CALLABLE_PROBE = "callable_probe"


@dataclass(frozen=True, slots=True)
class DetectionRule:
    """Request-time detection rule for one capability."""

    kind: DetectionRuleKind
    target: str
    timeout_s: float = 2.0
    additional_kwargs: tuple[tuple[str, str], ...] = ()

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"DetectionRule(kind={self.kind!r}, target={self.target!r}, timeout_s={self.timeout_s!r})"


@dataclass(frozen=True, slots=True)
class CapabilityMetadata:
    """Static metadata for one governed capability."""

    kind: CapabilityKind
    display_name: str
    description: str
    target_environment: str
    pip_extra: str
    extra_packages: tuple[str, ...]
    disk_impact_mb: int
    network_impact_mb: int
    requires_native_binary: bool
    requires_wsl: bool
    requires_credentials: tuple[str, ...]
    risk_level: CapabilityRiskLevel
    degraded_fallback: str
    uninstall_note: str
    detection_rule: DetectionRule

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"CapabilityMetadata(kind={self.kind!r}, display_name={self.display_name!r}, description={self.description!r})"


@dataclass(frozen=True, slots=True)
class CapabilityState:
    """Per-capability runtime state."""

    kind: CapabilityKind
    install_state: CapabilityInstallState
    health_state: CapabilityHealthState
    last_checked_utc: str | None
    last_install_attempt_utc: str | None
    last_decline_utc: str | None
    install_failure_reason: str | None

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"CapabilityState(kind={self.kind!r}, install_state={self.install_state!r}, health_state={self.health_state!r})"


@dataclass(frozen=True, slots=True)
class CapabilityProbeResult:
    """Outcome of running one detection rule."""

    kind: CapabilityKind
    reachable: bool
    health_state: CapabilityHealthState
    probed_at_utc: str
    error: str | None
    latency_ms: int

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"CapabilityProbeResult(kind={self.kind!r}, reachable={self.reachable!r}, health_state={self.health_state!r})"


@dataclass(frozen=True, slots=True)
class CapabilityInstallRequest:
    """Request to install a capability after a missing-capability gate."""

    kind: CapabilityKind
    requested_by_user: bool
    requested_at_utc: str
    request_context: str

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"CapabilityInstallRequest(kind={self.kind!r}, requested_by_user={self.requested_by_user!r}, requested_at_utc={self.requested_at_utc!r})"


@dataclass(frozen=True, slots=True)
class CapabilityInstallApproval:
    """Explicit user approval token for one capability install."""

    request_id: str
    kind: CapabilityKind
    approved_at_utc: str
    approver_session_id: str

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"CapabilityInstallApproval(request_id={self.request_id!r}, kind={self.kind!r}, approved_at_utc={self.approved_at_utc!r})"


class CapabilityRegistryError(Exception):
    """Base class for capability registry and lifecycle errors."""


class CapabilityNotFound(CapabilityRegistryError):
    """Raised when a capability id is not registered."""


class CapabilityNotInstalled(CapabilityNotAvailable):
    """Raised when a needed capability is absent at request time."""

    def __init__(
        self,
        message: str,
        *,
        kind: CapabilityKind,
        metadata: CapabilityMetadata | None = None,
    ) -> None:
        super().__init__(
            message,
            agent_type="N/A",
            required_capability=kind.value,
            available_capabilities=[],
        )
        self.kind = kind
        self.metadata = metadata


class CapabilityInstallError(CapabilityRegistryError):
    """Raised when a capability install fails."""

    def __init__(
        self,
        message: str,
        *,
        kind: CapabilityKind | None = None,
        pip_extra: str | None = None,
        command: tuple[str, ...] = (),
        stdout: str = "",
        stderr: str = "",
        returncode: int | None = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.pip_extra = pip_extra
        self.command = command
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


class CapabilityApprovalRequired(CapabilityRegistryError):
    """Raised when install is attempted without a valid user approval."""

    def __init__(
        self,
        message: str,
        *,
        kind: CapabilityKind | None = None,
        install_command: tuple[str, ...] = (),
        target_environment: str = "",
        extra_packages: tuple[str, ...] = (),
        disk_impact_mb: int = 0,
        network_impact_mb: int = 0,
        risk_level: CapabilityRiskLevel | None = None,
        degraded_fallback: str = "",
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.install_command = install_command
        self.target_environment = target_environment
        self.extra_packages = extra_packages
        self.disk_impact_mb = disk_impact_mb
        self.network_impact_mb = network_impact_mb
        self.risk_level = risk_level
        self.degraded_fallback = degraded_fallback


__all__ = [
    "CapabilityApprovalRequired",
    "CapabilityHealthState",
    "CapabilityInstallApproval",
    "CapabilityInstallError",
    "CapabilityInstallRequest",
    "CapabilityInstallState",
    "CapabilityKind",
    "CapabilityMetadata",
    "CapabilityNotFound",
    "CapabilityNotInstalled",
    "CapabilityProbeResult",
    "CapabilityRegistryError",
    "CapabilityRiskLevel",
    "CapabilityState",
    "DetectionRule",
    "DetectionRuleKind",
]
