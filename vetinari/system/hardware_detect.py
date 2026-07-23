"""Hardware detection — CPU, RAM, GPU, and VRAM discovery across platforms.

This is the first step in the setup pipeline: **Hardware Detection** →
Model Recommendation → Init Wizard → Configuration.

Provides a unified ``HardwareProfile`` dataclass and ``detect_hardware()``
entry point that probes the system for compute resources. Used by the init
wizard (``vetinari init``) and the doctor command (``vetinari doctor``).

GPU detection strategies:
  - NVIDIA: via pynvml (nvidia-ml-py)
  - AMD: via rocm-smi subprocess call
  - Apple Silicon: via sysctl + Metal framework detection
  - Fallback: CPU-only profile when no GPU detected or probe times out
"""

from __future__ import annotations

import importlib
import logging
import os
import platform
import shutil
import subprocess
import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


# ── Constants ─────────────────────────────────────────────────────────────────

GPU_PROBE_TIMEOUT_SECONDS = 30  # Max time to spend probing GPU hardware
GPU_PROBE_TIMEOUT_WINDOWS = 90  # Windows GPU probing is slower due to driver init
APPLE_SILICON_UNIFIED_MEMORY_RATIO = 0.75  # ~75% of system RAM available for models


class GpuVendor(Enum):
    """GPU hardware vendor classification."""

    NONE = "none"
    NVIDIA = "nvidia"
    AMD = "amd"
    APPLE = "apple"
    INTEL = "intel"


@dataclass(frozen=True, slots=True)
class GpuInfo:
    """Detected GPU hardware details.

    Attributes:
        name: Human-readable GPU name (e.g. "NVIDIA RTX 4090").
        vendor: GPU vendor classification.
        vram_gb: Dedicated VRAM in gigabytes (or estimated for unified memory).
        cuda_available: Whether CUDA runtime is available.
        metal_available: Whether Apple Metal is available.
        driver_version: GPU driver version string, if detected.
    """

    name: str = ""
    vendor: GpuVendor = GpuVendor.NONE
    vram_gb: float = 0.0
    cuda_available: bool = False
    metal_available: bool = False
    driver_version: str = ""

    def __repr__(self) -> str:
        return "GpuInfo(...)"


@dataclass(frozen=True, slots=True)
class HardwareProfile:
    """Complete hardware profile for a system.

    Immutable snapshot of detected compute resources, used by the model
    recommender to select appropriate GGUF models and quantization levels.

    Attributes:
        cpu_count: Number of logical CPU cores.
        ram_gb: Total system RAM in gigabytes.
        gpu: Detected GPU information (empty GpuInfo if no GPU).
        os_name: Operating system name (Linux, Darwin, Windows).
        arch: CPU architecture (x86_64, arm64, etc.).
    """

    cpu_count: int = 1
    ram_gb: float = 0.0
    gpu: GpuInfo = field(default_factory=GpuInfo)
    os_name: str = ""
    arch: str = ""

    def __repr__(self) -> str:
        return "HardwareProfile(...)"

    @property
    def gpu_name(self) -> str | None:
        """GPU display name, or None if no GPU detected."""
        return self.gpu.name or None

    @property
    def vram_gb(self) -> float:
        """Available VRAM in gigabytes (0.0 if no GPU)."""
        return self.gpu.vram_gb

    @property
    def gpu_vendor(self) -> GpuVendor:
        """GPU vendor classification."""
        return self.gpu.vendor

    @property
    def cuda_available(self) -> bool:
        """Whether CUDA is available for GPU offloading."""
        return self.gpu.cuda_available

    @property
    def metal_available(self) -> bool:
        """Whether Apple Metal is available for GPU offloading."""
        return self.gpu.metal_available

    @property
    def has_gpu(self) -> bool:
        """Whether any GPU was detected."""
        return self.gpu.vendor != GpuVendor.NONE

    @property
    def effective_vram_gb(self) -> float:
        """Effective VRAM for model loading, accounting for overhead.

        Returns 90% of detected VRAM to leave headroom for the OS and
        other processes.
        """
        return round(self.vram_gb * 0.9, 1)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dictionary for JSON output.

        Returns:
            Dictionary with all hardware profile fields.
        """
        return {
            "cpu_count": self.cpu_count,
            "ram_gb": self.ram_gb,
            "gpu_name": self.gpu_name,
            "gpu_vendor": self.gpu_vendor.value,
            "vram_gb": self.vram_gb,
            "cuda_available": self.cuda_available,
            "metal_available": self.metal_available,
            "driver_version": self.gpu.driver_version,
            "os_name": self.os_name,
            "arch": self.arch,
            "has_gpu": self.has_gpu,
            "effective_vram_gb": self.effective_vram_gb,
        }


# ── GPU Detection Strategies ─────────────────────────────────────────────────


def _detect_nvidia_gpu() -> GpuInfo | None:
    """Detect NVIDIA GPU via pynvml (nvidia-ml-py).

    Returns:
        GpuInfo if an NVIDIA GPU is found, None otherwise.
    """
    try:
        pynvml = importlib.import_module("pynvml")

        pynvml.nvmlInit()
        try:
            handle = pynvml.nvmlDeviceGetHandleByIndex(0)
            name = pynvml.nvmlDeviceGetName(handle)
            if isinstance(name, bytes):
                name = name.decode("utf-8", errors="replace")

            mem_info = pynvml.nvmlDeviceGetMemoryInfo(handle)
            vram_gb = round(mem_info.total / (1024**3), 1)

            driver_version = ""
            try:
                dv = pynvml.nvmlSystemGetDriverVersion()
                driver_version = dv.decode("utf-8") if isinstance(dv, bytes) else str(dv)
            except Exception:
                logger.warning("Could not read NVIDIA driver version — leaving empty")

            return GpuInfo(
                name=str(name),
                vendor=GpuVendor.NVIDIA,
                vram_gb=vram_gb,
                cuda_available=True,
                metal_available=False,
                driver_version=driver_version,
            )
        finally:
            pynvml.nvmlShutdown()
    except ImportError:
        logger.warning("pynvml not installed — NVIDIA detection skipped")
        return None
    except Exception:
        logger.warning("pynvml GPU detection failed — NVIDIA GPU may not be present", exc_info=True)
        return None


def _detect_amd_gpu() -> GpuInfo | None:
    """Detect AMD GPU via rocm-smi command-line tool.

    Returns:
        GpuInfo if an AMD GPU is found, None otherwise.
    """
    rocm_smi = shutil.which("rocm-smi")
    if not rocm_smi:
        return None

    try:
        result = subprocess.run(
            [rocm_smi, "--showproductname", "--showmeminfo", "vram", "--csv"],
            capture_output=True,
            text=True,
            timeout=GPU_PROBE_TIMEOUT_SECONDS,
            check=False,
        )
        if result.returncode != 0:
            return None

        lines = result.stdout.strip().splitlines()
        gpu_name = "AMD GPU"
        vram_gb = 0.0

        for line in lines:
            if "card" in line.lower() and "," in line:
                parts = line.split(",")
                if len(parts) >= 2:
                    gpu_name = parts[1].strip() or gpu_name
            if "total" in line.lower() and "," in line:
                parts = line.split(",")
                for part in parts:
                    part = part.strip()
                    try:
                        vram_bytes = int(part)
                        if vram_bytes > 1_000_000:  # Plausible VRAM in bytes
                            vram_gb = round(vram_bytes / (1024**3), 1)
                    except ValueError:
                        logger.warning("Could not parse VRAM value %r from rocm-smi output — skipping token", part)
                        continue

        if vram_gb > 0:
            return GpuInfo(
                name=gpu_name,
                vendor=GpuVendor.AMD,
                vram_gb=vram_gb,
                cuda_available=False,
                metal_available=False,
            )
        return None
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        logger.warning("rocm-smi AMD detection failed — AMD GPU will not be available", exc_info=True)
        return None


def _detect_apple_silicon() -> GpuInfo | None:
    """Detect Apple Silicon GPU via platform checks and sysctl.

    Apple Silicon uses unified memory — the GPU shares system RAM.
    We estimate ~75% of total RAM is available for model loading.

    Returns:
        GpuInfo if Apple Silicon is detected, None otherwise.
    """
    if platform.system() != "Darwin":
        return None

    # Check for Apple Silicon (arm64 architecture on macOS)
    if platform.machine() not in ("arm64", "aarch64"):
        return None

    try:
        result = subprocess.run(
            ["sysctl", "-n", "hw.memsize"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if result.returncode != 0:
            return None

        total_bytes = int(result.stdout.strip())
        total_gb = total_bytes / (1024**3)
        # Unified memory: ~75% available for model loading
        model_vram_gb = round(total_gb * APPLE_SILICON_UNIFIED_MEMORY_RATIO, 1)

        # Detect chip name
        chip_name = "Apple Silicon"
        try:
            chip_result = subprocess.run(
                ["sysctl", "-n", "machdep.cpu.brand_string"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            if chip_result.returncode == 0 and chip_result.stdout.strip():
                chip_name = chip_result.stdout.strip()
        except (subprocess.TimeoutExpired, OSError):
            logger.warning("Could not read Apple Silicon chip name — using default '%s'", chip_name)

        return GpuInfo(
            name=chip_name,
            vendor=GpuVendor.APPLE,
            vram_gb=model_vram_gb,
            cuda_available=False,
            metal_available=True,
        )
    except (subprocess.TimeoutExpired, ValueError, OSError):
        logger.warning("Apple Silicon detection failed — Apple GPU will not be available", exc_info=True)
        return None


def _detect_gpu_with_timeout() -> GpuInfo:
    """Run GPU detection strategies with a timeout to prevent hangs.

    Tries NVIDIA → AMD → Apple Silicon in order, with an overall timeout
    to prevent hanging on driver init (common on Windows).

    Returns:
        GpuInfo with detected GPU, or empty GpuInfo if nothing found.
    """
    timeout = GPU_PROBE_TIMEOUT_WINDOWS if platform.system() == "Windows" else GPU_PROBE_TIMEOUT_SECONDS
    result: GpuInfo | None = None

    def _probe() -> None:
        nonlocal result
        # Try NVIDIA first (most common for ML)
        result = _detect_nvidia_gpu()
        if result:
            return

        # Try AMD
        result = _detect_amd_gpu()
        if result:
            return

        # Try Apple Silicon
        result = _detect_apple_silicon()

    thread = threading.Thread(target=_probe, daemon=True, name="gpu-probe")
    thread.start()
    thread.join(timeout=timeout)

    if thread.is_alive():
        logger.warning(
            "GPU detection timed out after %ds — falling back to CPU-only profile",
            timeout,
        )
        return GpuInfo()

    return result or GpuInfo()


# ── Main Entry Point ─────────────────────────────────────────────────────────


def detect_hardware() -> HardwareProfile:
    """Detect all available hardware and return a unified profile.

    Probes CPU, RAM, and GPU (with timeout). Safe to call from any
    platform — always returns a valid HardwareProfile, falling back
    to CPU-only when GPU detection fails or times out.

    Returns:
        HardwareProfile with detected compute resources.
    """
    cpu_count = os.cpu_count() or 1
    ram_gb = 0.0

    try:
        import psutil

        ram_gb = round(psutil.virtual_memory().total / (1024**3), 1)
    except ImportError:
        logger.debug("psutil not installed — RAM detection skipped, using 0.0")

    gpu = _detect_gpu_with_timeout()

    return HardwareProfile(
        cpu_count=cpu_count,
        ram_gb=ram_gb,
        gpu=gpu,
        os_name=platform.system(),
        arch=platform.machine(),
    )
