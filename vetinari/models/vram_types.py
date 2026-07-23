"""VRAM manager value objects and identifiers.

This module owns the lightweight data contracts used by the local model memory
budgeting path.  The manager re-exports these names so existing callers can keep
importing them from ``vetinari.models.vram_manager``.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field


class ExecutionPhase:
    """Pipeline execution phase used to choose the resident model set."""

    PLANNING = "planning"
    EXECUTION = "execution"
    REVIEW = "review"


@dataclass(frozen=True, slots=True)
class ModelLease:
    """Active claim on a loaded model, preventing eviction during inference.

    When an agent begins inference against a model, it acquires a lease.  The
    VRAM manager refuses to evict models with active leases.  Leases auto-expire
    after ``max_duration_s`` to prevent leaked claims from permanently pinning
    models.
    """

    model_id: str
    holder_id: str  # e.g. "worker:task-42"
    acquired_at: float = field(default_factory=time.time)
    max_duration_s: float = 300.0  # Five-minute default expiry.

    def __repr__(self) -> str:
        """Show the lease owner and whether it has expired."""
        return f"ModelLease(model_id={self.model_id!r}, holder_id={self.holder_id!r}, expired={self.is_expired})"

    @property
    def is_expired(self) -> bool:
        """True when the lease has exceeded its max duration."""
        return (time.time() - self.acquired_at) > self.max_duration_s


class GPUArchitecture:
    """GPU architecture family detected via CUDA compute capability."""

    UNKNOWN = "unknown"
    ADA_LOVELACE = "ada_lovelace"  # sm_89 (RTX 40xx)
    BLACKWELL = "blackwell"  # sm_120 (RTX 50xx)


@dataclass(frozen=True, slots=True)
class VRAMSnapshot:
    """Point-in-time GPU memory reading."""

    gpu_index: int
    total_gb: float
    used_gb: float
    free_gb: float
    timestamp: float = field(default_factory=time.time)

    def __repr__(self) -> str:
        """Show key memory fields for debugging."""
        return f"VRAMSnapshot(gpu_index={self.gpu_index!r}, free_gb={self.free_gb!r}, used_gb={self.used_gb!r})"


@dataclass
class ModelVRAMEstimate:
    """VRAM and RAM usage estimate for a loaded model."""

    model_id: str
    gpu_gb: float  # VRAM used by this model's weights.
    cpu_gb: float  # System RAM used by CPU offload.
    last_used: float  # Epoch timestamp.
    priority: int = 5  # Lower values sort earlier in eviction ranking.
    kv_cache_gb: float = 0.0  # VRAM used by KV cache for this model.
    is_pinned: bool = False  # True means eviction must skip this model.

    @property
    def total_gpu_gb(self) -> float:
        """Total GPU memory including KV cache."""
        return self.gpu_gb + self.kv_cache_gb

    def __repr__(self) -> str:
        """Show key identifying fields for debugging."""
        return (
            f"ModelVRAMEstimate(model_id={self.model_id!r},"
            f" gpu_gb={self.gpu_gb!r}, kv={self.kv_cache_gb:.1f}, "
            f"priority={self.priority!r})"
        )


@dataclass(frozen=True, slots=True)
class VRAMPreflightResult:
    """Result of a fail-closed model VRAM admission check."""

    model_id: str
    passed: bool
    required_vram_gb: float | None
    available_vram_gb: float | None
    reason: str

    def __repr__(self) -> str:
        """Show the admission outcome and compared capacity values."""
        return (
            "VRAMPreflightResult("
            f"model_id={self.model_id!r}, passed={self.passed!r}, "
            f"required_vram_gb={self.required_vram_gb!r}, "
            f"available_vram_gb={self.available_vram_gb!r})"
        )


__all__ = [
    "ExecutionPhase",
    "GPUArchitecture",
    "ModelLease",
    "ModelVRAMEstimate",
    "VRAMPreflightResult",
    "VRAMSnapshot",
]
