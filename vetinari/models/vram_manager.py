"""Vetinari VRAM Manager.

======================
Tracks GPU memory budget for llama-cpp-python local inference and advises on model
load/unload decisions.

Hardware target: RTX 5090 (32 GB VRAM) + 9950X3D + 64 GB DDR5.

Features
--------
- Real-time VRAM monitoring via ``pynvml`` (optional) with fallback estimates
- Tracks which models the registry believes are loaded + their VRAM footprint
- Checks whether a model fits before routing to it
- Recommends which model to evict (LRU + lowest priority) to make room
- Respects CPU-offload flag: models >32 GB can run with split GPU+RAM

Usage::

    from vetinari.models.vram_manager import get_vram_manager

    manager = get_vram_manager()
    manager.refresh()

    if manager.can_load("qwen3-vl-32b"):
        logger.debug("Fits in VRAM")
    else:
        evict = manager.recommend_eviction("qwen3-vl-32b")
        logger.debug("Unload %s first", evict)

    logger.debug(manager.status_summary())
"""

from __future__ import annotations

import importlib
import logging
import math
import os
import threading
import time
from functools import cache
from typing import Any

from vetinari.models.vram_capacity import _KV_BYTES_PER_TOKEN as _KV_BYTES_PER_TOKEN
from vetinari.models.vram_capacity import _VRAMCapacityMixin, check_model_vram_capacity
from vetinari.models.vram_leases import _VRAMLeaseMixin
from vetinari.models.vram_phase import _VRAMPhaseMixin
from vetinari.models.vram_types import (
    ExecutionPhase,
    GPUArchitecture,
    ModelLease,
    ModelVRAMEstimate,
    VRAMPreflightResult,
    VRAMSnapshot,
)

logger = logging.getLogger(__name__)

__all__ = [
    "ExecutionPhase",
    "GPUArchitecture",
    "ModelLease",
    "ModelVRAMEstimate",
    "VRAMManager",
    "VRAMPreflightResult",
    "VRAMSnapshot",
    "check_model_vram_capacity",
    "detect_gpu_architecture",
    "get_vram_manager",
]


# ---------------------------------------------------------------------------
# Optional nvidia-ml-py import - lazy init to avoid module-level I/O
# ---------------------------------------------------------------------------
_pynvml = None  # Lazy: initialized on first GPU query
_PYNVML_AVAILABLE: bool | None = None  # None = not yet checked


@cache
def _load_nvml() -> bool:
    """Lazily initialize NVML on first use, avoiding module-level I/O.

    Returns:
        True if pynvml is available and initialized.
    """
    global _pynvml, _PYNVML_AVAILABLE
    if _PYNVML_AVAILABLE is not None:
        return _PYNVML_AVAILABLE
    try:
        pynvml: Any = importlib.import_module("pynvml")

        pynvml.nvmlInit()
        _pynvml = pynvml
        _PYNVML_AVAILABLE = True
    except Exception:
        _PYNVML_AVAILABLE = False
    return _PYNVML_AVAILABLE


_ensure_nvml = _load_nvml


def _get_nvml() -> Any | None:
    """Return the initialized NVML module, or None when unavailable."""
    if not _ensure_nvml():
        return None
    return _pynvml


# Safety margin fraction - reserve 10% of VRAM for CUDA context + KV cache growth
_SAFETY_MARGIN = 0.10

# Thermal thresholds (Celsius)
_THERMAL_WARN_C = 80
_THERMAL_THROTTLE_C = 85

# Maximum concurrent model loads
_MAX_CONCURRENT_LOADS = 2


def detect_gpu_architecture(gpu_index: int = 0) -> str:
    """Detect GPU architecture via CUDA compute capability.

    Args:
        gpu_index: GPU device index.

    Returns:
        GPUArchitecture string value.
    """
    pynvml = _get_nvml()
    if pynvml is None:
        return GPUArchitecture.UNKNOWN
    try:
        handle = pynvml.nvmlDeviceGetHandleByIndex(gpu_index)
        major, minor = pynvml.nvmlDeviceGetCudaComputeCapability(handle)
        sm = major * 10 + minor
        if sm >= 120:
            return GPUArchitecture.BLACKWELL
        if sm >= 89:
            return GPUArchitecture.ADA_LOVELACE
        return GPUArchitecture.UNKNOWN
    except Exception as exc:
        logger.warning("GPU architecture detection failed: %s", exc)
        return GPUArchitecture.UNKNOWN


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------


class VRAMManager(_VRAMLeaseMixin, _VRAMPhaseMixin, _VRAMCapacityMixin):
    """GPU memory budget manager for local llama-cpp-python models."""

    _instance: VRAMManager | None = None
    _lock = threading.Lock()

    def __init__(self):
        self._gpu_total_gb: float = float(os.environ.get("VETINARI_GPU_VRAM_GB", "32"))
        self._cpu_offload_gb: float = float(os.environ.get("VETINARI_CPU_OFFLOAD_GB", "30"))
        self._overhead_gb: float = self._gpu_total_gb * _SAFETY_MARGIN  # 10% safety margin
        self._estimates: dict[str, ModelVRAMEstimate] = {}
        self._reservations: dict[str, float] = {}  # model_id -> reserved GB (pre-load claim)
        self._lock_rw = threading.RLock()
        self._last_snapshot: VRAMSnapshot | None = None
        self._current_phase: str = ExecutionPhase.EXECUTION
        self._gpu_arch: str | None = None  # Lazy detected
        self._load_semaphore = threading.Semaphore(_MAX_CONCURRENT_LOADS)
        self._leases: dict[str, ModelLease] = {}  # holder_id -> active lease

        # Try to load hardware config from models.yaml
        self._load_hardware_config()

    # ------------------------------------------------------------------
    # Singleton
    # ------------------------------------------------------------------

    @classmethod
    def get_instance(cls) -> VRAMManager:
        """Get instance.

        Returns:
            The VRAMManager result.
        """
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls()
        return cls._instance

    # ------------------------------------------------------------------
    # Config
    # ------------------------------------------------------------------

    def _load_hardware_config(self) -> None:
        try:
            from pathlib import Path

            from vetinari.utils import load_yaml

            cfg_path = Path(__file__).parent.parent / "config" / "models.yaml"
            if cfg_path.exists():
                cfg = load_yaml(str(cfg_path))
                hw = cfg.get("hardware", {})
                if hw.get("gpu_vram_gb"):
                    self._gpu_total_gb = float(hw["gpu_vram_gb"])
                    self._overhead_gb = self._gpu_total_gb * _SAFETY_MARGIN
                if hw.get("max_cpu_offload_gb"):
                    self._cpu_offload_gb = float(hw["max_cpu_offload_gb"])
        except Exception as e:
            logger.warning("[VRAMManager] Could not load hardware config: %s", e)

    # ------------------------------------------------------------------
    # Real-time VRAM reading
    # ------------------------------------------------------------------

    def get_gpu_snapshot(self, gpu_index: int = 0) -> VRAMSnapshot | None:
        """Return live GPU memory snapshot via nvidia-ml-py if available.

        Args:
            gpu_index: GPU device index (default: 0).

        Returns:
            VRAMSnapshot with current GPU memory readings, or None if unavailable.
        """
        pynvml = _get_nvml()
        if pynvml is None:
            return None
        try:
            handle = pynvml.nvmlDeviceGetHandleByIndex(gpu_index)
            mem = pynvml.nvmlDeviceGetMemoryInfo(handle)
            snap = VRAMSnapshot(
                gpu_index=gpu_index,
                total_gb=mem.total / 1e9,
                used_gb=mem.used / 1e9,
                free_gb=mem.free / 1e9,
            )
            self._last_snapshot = snap
            return snap
        except Exception as e:
            logger.warning("VRAMManager GPU snapshot failed: %s", e)
            return None

    # ------------------------------------------------------------------
    # Thermal monitoring
    # ------------------------------------------------------------------

    @staticmethod
    def get_gpu_temperature(gpu_index: int = 0) -> int | None:
        """Read GPU core temperature in Celsius via nvidia-ml-py.

        Args:
            gpu_index: GPU device index.

        Returns:
            Temperature in Celsius, or None if unavailable.
        """
        pynvml = _get_nvml()
        if pynvml is None:
            return None
        try:
            handle = pynvml.nvmlDeviceGetHandleByIndex(gpu_index)
            temp = int(pynvml.nvmlDeviceGetTemperature(handle, pynvml.NVML_TEMPERATURE_GPU))
            if temp >= _THERMAL_WARN_C:
                logger.warning("GPU temperature %d\u00b0C \u2014 approaching thermal limit", temp)
            return temp
        except Exception as exc:
            logger.warning(
                "GPU temperature read failed for GPU %d: %s - temperature monitoring unavailable", gpu_index, exc
            )
            return None

    def is_thermal_throttled(self, gpu_index: int = 0) -> bool:
        """Return True when GPU temp is at or above throttle threshold (85C).

        Args:
            gpu_index: GPU device index.

        Returns:
            True if thermal throttling should be applied.
        """
        temp = self.get_gpu_temperature(gpu_index)
        return temp is not None and temp >= _THERMAL_THROTTLE_C

    def get_gpu_architecture(self, gpu_index: int = 0) -> str:
        """Return the detected GPU architecture (cached after first detection).

        Args:
            gpu_index: GPU device index.

        Returns:
            GPUArchitecture string value.
        """
        if self._gpu_arch is None:
            self._gpu_arch = detect_gpu_architecture(gpu_index)
        return self._gpu_arch

    def get_free_vram_gb(self) -> float:
        """Return estimated free VRAM in GB (accounts for reservations and KV cache).

        Returns:
            Estimated free VRAM considering loaded models, reservations, and KV caches.
        """
        snap = self.get_gpu_snapshot()
        reserved = sum(self._reservations.values())
        if snap:
            return max(0.0, snap.free_gb - reserved)

        # Fallback: estimate from tracked loads + reservations
        used = sum(e.total_gpu_gb for e in self._estimates.values())
        return max(0.0, self._gpu_total_gb - self._overhead_gb - used - reserved)

    def get_used_vram_gb(self) -> float:
        """Return estimated used VRAM in GB.

        Returns:
            Resolved used vram gb value.
        """
        snap = self.get_gpu_snapshot()
        if snap:
            return snap.used_gb
        used = sum(e.gpu_gb for e in self._estimates.values())
        return self._overhead_gb + used

    # ------------------------------------------------------------------
    # Model tracking
    # ------------------------------------------------------------------

    def refresh(self) -> None:
        """Sync tracked model state with the ModelRegistry."""
        try:
            from vetinari.models.model_registry import get_model_registry

            registry = get_model_registry()
            loaded = registry.get_loaded_local_models()
            with self._lock_rw:
                # Remove entries no longer loaded
                loaded_ids = {m.model_id for m in loaded}
                stale = [mid for mid in self._estimates if mid not in loaded_ids]
                for mid in stale:
                    del self._estimates[mid]
                # Add new entries
                for m in loaded:
                    if m.model_id not in self._estimates:
                        self._estimates[m.model_id] = ModelVRAMEstimate(
                            model_id=m.model_id,
                            gpu_gb=min(m.memory_requirements_gb, self._gpu_total_gb),
                            cpu_gb=max(0.0, m.memory_requirements_gb - self._gpu_total_gb),
                            last_used=time.time(),
                        )
        except Exception as e:
            logger.warning("[VRAMManager] refresh() failed: %s", e)

    def mark_used(self, model_id: str) -> None:
        """Update last-used timestamp for a model (call before inference)."""
        with self._lock_rw:
            if model_id in self._estimates:
                estimate = self._estimates[model_id]
                now = time.time()
                estimate.last_used = now if now > estimate.last_used else math.nextafter(estimate.last_used, math.inf)

    def register_load(self, model_id: str, vram_gb: float, cpu_gb: float = 0.0) -> None:
        """Register that a model has been loaded, clearing any pending reservation.

        Args:
            model_id: Model identifier.
            vram_gb: VRAM consumed by model weights in GB.
            cpu_gb: System RAM consumed by CPU-offloaded layers in GB.
        """
        with self._lock_rw:
            self._reservations.pop(model_id, None)
            self._estimates[model_id] = ModelVRAMEstimate(
                model_id=model_id,
                gpu_gb=vram_gb,
                cpu_gb=cpu_gb,
                last_used=time.time(),
            )

    def register_unload(self, model_id: str) -> None:
        """Register that a model has been unloaded."""
        with self._lock_rw:
            self._estimates.pop(model_id, None)
            self._reservations.pop(model_id, None)

    # ------------------------------------------------------------------
    # Atomic reservations
    # ------------------------------------------------------------------

    def reserve(self, model_id: str, gpu_gb: float) -> bool:
        """Atomically reserve VRAM before loading starts, preventing TOCTOU races.

        Args:
            model_id: Model to reserve VRAM for.
            gpu_gb: VRAM to reserve in GB.

        Returns:
            True if reservation succeeded, False if insufficient VRAM.
        """
        with self._lock_rw:
            if model_id in self._reservations:
                return True
            free = self.get_free_vram_gb()
            if gpu_gb > free:
                return False
            self._reservations[model_id] = gpu_gb
            logger.debug("VRAM reserved %.1f GB for %s", gpu_gb, model_id)
            return True

    def release_reservation(self, model_id: str) -> None:
        """Release a VRAM reservation (e.g., if load fails).

        Args:
            model_id: Model whose reservation to release.
        """
        with self._lock_rw:
            removed = self._reservations.pop(model_id, None)
            if removed is not None:
                logger.debug("VRAM reservation released for %s (%.1f GB)", model_id, removed)

    def acquire_load_slot(self) -> bool:
        """Acquire a concurrent model load slot (blocks up to 30s).

        Returns:
            True if slot acquired, False on timeout.
        """
        return self._load_semaphore.acquire(timeout=30.0)

    def release_load_slot(self) -> None:
        """Release a concurrent model load slot."""
        self._load_semaphore.release()

    # ------------------------------------------------------------------
    # Pinned models
    # ------------------------------------------------------------------

    def pin(self, model_id: str) -> None:
        """Mark a model as pinned - never evicted by recommend_eviction.

        Args:
            model_id: Model to pin.
        """
        with self._lock_rw:
            if model_id in self._estimates:
                self._estimates[model_id].is_pinned = True
                logger.info("Model %s pinned", model_id)

    def unpin(self, model_id: str) -> None:
        """Remove the pin from a model, making it eligible for eviction.

        Args:
            model_id: Model to unpin.
        """
        with self._lock_rw:
            if model_id in self._estimates:
                self._estimates[model_id].is_pinned = False

    def update_kv_cache(self, model_id: str, kv_cache_gb: float) -> None:
        """Update KV cache size tracking for a loaded model.

        Args:
            model_id: Model identifier.
            kv_cache_gb: Current KV cache size in GB.
        """
        with self._lock_rw:
            if model_id in self._estimates:
                self._estimates[model_id].kv_cache_gb = kv_cache_gb

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def status_summary(self) -> dict[str, Any]:
        """Return a summary dict for dashboard / logging.

        Returns:
            A dict with GPU totals (``gpu_total_gb``, ``gpu_used_gb``,
            ``gpu_free_gb``), CPU offload budget and usage, whether pynvml
            is available, and a ``loaded_models`` sub-dict mapping each
            tracked model_id to its current ``gpu_gb`` and ``cpu_gb``
            allocations.
        """
        self.refresh()
        snap = self.get_gpu_snapshot()
        used_by_models = sum(e.gpu_gb for e in self._estimates.values())
        free_est = self.get_free_vram_gb()

        temp = self.get_gpu_temperature()
        return {
            "gpu_total_gb": snap.total_gb if snap else self._gpu_total_gb,
            "gpu_used_gb": snap.used_gb if snap else (used_by_models + self._overhead_gb),
            "gpu_free_gb": snap.free_gb if snap else free_est,
            "cpu_offload_budget_gb": self._cpu_offload_gb,
            "cpu_offload_used_gb": sum(e.cpu_gb for e in self._estimates.values()),
            "pynvml_available": _ensure_nvml(),
            "gpu_architecture": self.get_gpu_architecture(),
            "gpu_temperature_c": temp,
            "thermal_throttled": temp is not None and temp >= _THERMAL_THROTTLE_C,
            "current_phase": self._current_phase,
            "pending_reservations": dict(self._reservations),
            "active_leases": {
                hid: {"model_id": lease.model_id, "age_s": round(time.time() - lease.acquired_at, 1)}
                for hid, lease in self._leases.items()
                if not lease.is_expired
            },
            "loaded_models": {
                mid: {"gpu_gb": e.gpu_gb, "cpu_gb": e.cpu_gb, "kv_cache_gb": e.kv_cache_gb, "pinned": e.is_pinned}
                for mid, e in self._estimates.items()
            },
        }


# ---------------------------------------------------------------------------
# Module-level accessor
# ---------------------------------------------------------------------------

_vram_manager: VRAMManager | None = None
_vram_lock = threading.Lock()


def get_vram_manager() -> VRAMManager:
    """Return the global VRAMManager singleton (created lazily).

    Returns:
        The VRAMManager result.
    """
    global _vram_manager
    if _vram_manager is None:
        with _vram_lock:
            if _vram_manager is None:
                _vram_manager = VRAMManager.get_instance()
    return _vram_manager
