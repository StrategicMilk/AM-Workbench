"""Local runtime onboarding records and typed blockers."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class LocalRuntimeOnboardingError(RuntimeError):
    """Raised when onboarding state cannot be trusted."""


class LocalRuntimeProbeError(LocalRuntimeOnboardingError):
    """Raised by direct probe callers that request exception semantics."""


class LocalRuntimeKind(str, Enum):
    """Supported local OpenAI-compatible runtime servers."""

    LMSTUDIO = "lmstudio"
    JAN = "jan"
    OPENWEBUI = "openwebui"


class BlockerKind(str, Enum):
    """Typed onboarding blockers surfaced to API and UI consumers."""

    RUNTIME_NOT_INSTALLED = "runtime_not_installed"
    PORT_COLLISION = "port_collision"
    MODEL_DOWNLOAD_FAILURE = "model_download_failure"
    NETWORK_UNREACHABLE = "network_unreachable"
    HTTP_ERROR = "http_error"
    MALFORMED_RESPONSE = "malformed_response"
    HARDWARE_INSUFFICIENT = "hardware_insufficient"
    SCHEDULER_LANE_NOT_READY = "scheduler_lane_not_ready"
    DEPS_00_HOOK_MISSING = "deps_00_hook_missing"


@dataclass(frozen=True, slots=True)
class LocalRuntimeBlocker:
    """Actionable reason a local runtime is not ready."""

    kind: BlockerKind
    message: str
    remediation: str
    runtime_kind: LocalRuntimeKind | None = None
    model_id: str | None = None

    def __post_init__(self) -> None:
        if not self.message.strip():
            raise ValueError("LocalRuntimeBlocker.message must be non-empty")
        if not self.remediation.strip():
            raise ValueError("LocalRuntimeBlocker.remediation must be non-empty")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"LocalRuntimeBlocker(kind={self.kind!r}, message={self.message!r}, remediation={self.remediation!r})"


@dataclass(frozen=True, slots=True)
class LocalRuntimeProbeResult:
    """Result of one runtime endpoint probe."""

    runtime_kind: LocalRuntimeKind
    base_url: str
    reachable: bool
    discovered_models: tuple[dict[str, Any], ...] = ()
    http_status: int | None = None
    error: str | None = None
    latency_ms: int = 0
    checked_at_utc: str = ""

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"LocalRuntimeProbeResult(runtime_kind={self.runtime_kind!r}, base_url={self.base_url!r}, reachable={self.reachable!r})"


@dataclass(frozen=True, slots=True)
class HardwareFit:
    """Hardware fit assessment for one model registry entry."""

    model_id: str
    fits: bool
    required_memory_gb: int
    available_memory_gb: int
    requires_cpu_offload: bool
    selected_target: str
    reason: str

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"HardwareFit(model_id={self.model_id!r}, fits={self.fits!r}, required_memory_gb={self.required_memory_gb!r})"


@dataclass(frozen=True, slots=True)
class OnboardingReadiness:
    """Full onboarding snapshot returned by health and refresh calls."""

    probes: tuple[LocalRuntimeProbeResult, ...]
    hardware_fit_by_model: dict[str, HardwareFit]
    scheduler_lanes_ready: dict[str, bool]
    blockers: tuple[LocalRuntimeBlocker, ...]
    deps_00_hook_present: bool
    dry_run: bool
    state_path: str

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"OnboardingReadiness(probes={self.probes!r}, hardware_fit_by_model={self.hardware_fit_by_model!r}, scheduler_lanes_ready={self.scheduler_lanes_ready!r})"


@dataclass(frozen=True, slots=True)
class LocalRuntimeWriteback:
    """Read-only payload passed to the DEPS-00 config writeback hook."""

    runtime_kind: LocalRuntimeKind
    endpoint: str
    discovered_models: tuple[dict[str, Any], ...]
    requires_cpu_offload_models: tuple[str, ...]
    port_in_use_blocker: LocalRuntimeBlocker | None
    provenance: dict[str, str]

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"LocalRuntimeWriteback(runtime_kind={self.runtime_kind!r}, endpoint={self.endpoint!r}, discovered_models={self.discovered_models!r})"
