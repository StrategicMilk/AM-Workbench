"""DraftPairResolver — Speculative decoding draft model pairing.

Finds the optimal draft model for speculative decoding with a given main
model.  Draft models must be from the same architecture family, 4-10x
smaller, and both models must fit in available VRAM+RAM.

When a valid pair is found, the main model's Llama() constructor is passed
``draft_model=LlamaDraftModel(model_path=...)`` for native speculative
decoding support in llama.cpp.

Usage::

    from vetinari.models.draft_pair_resolver import get_draft_pair_resolver

    resolver = get_draft_pair_resolver()
    pair = resolver.find_pair(main_model_path, available_models)
    if pair:
        llama_kwargs["draft_model"] = pair.to_llama_draft_model()
"""

from __future__ import annotations

import ctypes
import logging
import platform
import subprocess
import threading
import time
from dataclasses import dataclass, field
from importlib import import_module
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# -- Configuration constants ---------------------------------------------------

MIN_SIZE_RATIO = 4.0  # Draft must be at least 4x smaller than main
MAX_SIZE_RATIO = 10.0  # Draft must be at most 10x smaller than main
ACCEPTANCE_RATE_DISABLE_THRESHOLD = 0.3  # Disable pair when acceptance drops below 30%
ACCEPTANCE_RATE_WINDOW = 50  # Rolling window for acceptance rate tracking
DEFAULT_VRAM_GB = 24.0
DEFAULT_RAM_GB = 32.0


def _detect_system_ram_gb() -> float:
    """Return installed system RAM in GiB using psutil or stdlib fallbacks."""
    try:
        import psutil

        return float(psutil.virtual_memory().total / (1024**3))
    except ImportError as exc:
        logger.info("psutil RAM detection unavailable: %s", exc)
    except (AttributeError, OSError) as exc:
        logger.warning("psutil RAM detection failed: %s", exc)

    system = platform.system().lower()
    if system == "linux":
        return _detect_linux_ram_gb()
    if system == "windows":
        return _detect_windows_ram_gb()
    if system == "darwin":
        return _detect_macos_ram_gb()
    raise OSError(f"unsupported platform for RAM auto-detection: {platform.system()!r}")


def _detect_linux_ram_gb() -> float:
    with Path("/proc/meminfo").open(encoding="utf-8") as handle:
        for line in handle:
            if line.startswith("MemTotal:"):
                parts = line.split()
                return float(int(parts[1]) * 1024 / (1024**3))
    raise OSError("MemTotal not found in /proc/meminfo")


def _detect_windows_ram_gb() -> float:
    class _MemoryStatus(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_ulong),
            ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    status = _MemoryStatus()
    status.dwLength = ctypes.sizeof(_MemoryStatus)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):  # type: ignore[attr-defined]
        raise OSError("GlobalMemoryStatusEx failed")
    return float(status.ullTotalPhys / (1024**3))


def _detect_macos_ram_gb() -> float:
    output = subprocess.check_output(["/usr/sbin/sysctl", "-n", "hw.memsize"], text=True, timeout=5)
    return float(int(output.strip()) / (1024**3))


# -- Data classes --------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DraftPair:
    """A validated main + draft model pair for speculative decoding.

    The pair is guaranteed to share the same architecture family and have
    a size ratio within the acceptable range.
    """

    main_model_path: Path
    draft_model_path: Path
    main_family: str
    draft_family: str
    main_size_gb: float
    draft_size_gb: float
    size_ratio: float
    draft_gpu_layers: int  # GPU layers for the draft model (-1 = all)
    main_on_gpu: bool  # Whether the main model is fully on GPU
    draft_on_gpu: bool  # Whether the draft model is fully on GPU

    def __repr__(self) -> str:
        return (
            f"DraftPair(main={self.main_model_path.stem!r}, "
            f"draft={self.draft_model_path.stem!r}, "
            f"ratio={self.size_ratio:.1f}x)"
        )

    def to_llama_draft_model(self) -> Any:
        """Build a LlamaDraftModel instance for the Llama() constructor.

        Returns:
            A ``llama_cpp.LlamaDraftModel`` instance, or None if llama-cpp-python
            does not support speculative decoding.
        """
        try:
            llama_cpp: Any = import_module("llama_cpp")
            llama_draft_model = llama_cpp.LlamaDraftModel

            return llama_draft_model(
                model_path=str(self.draft_model_path),
                n_gpu_layers=self.draft_gpu_layers,
            )
        except ImportError:
            logger.warning("LlamaDraftModel not available in this llama-cpp-python version")
            return None
        except Exception as exc:
            logger.warning("Failed to create LlamaDraftModel: %s", exc)
            return None


# -- Acceptance rate tracker ---------------------------------------------------


@dataclass
class _PairStats:
    """Rolling acceptance rate statistics for a draft pair."""

    accepted: int = 0
    total: int = 0
    history: list[bool] = field(default_factory=list)
    is_disabled: bool = False
    disabled_at: float = 0.0

    @property
    def acceptance_rate(self) -> float:
        """Current acceptance rate over the rolling window."""
        if not self.history:
            return 1.0  # Optimistic prior
        window = self.history[-ACCEPTANCE_RATE_WINDOW:]
        return sum(window) / len(window)

    def record(self, accepted: bool) -> None:
        """Record an acceptance/rejection event.

        Args:
            accepted: Whether the draft token was accepted.
        """
        self.history.append(accepted)
        self.total += 1
        if accepted:
            self.accepted += 1

        # Trim history to window size
        if len(self.history) > ACCEPTANCE_RATE_WINDOW * 2:
            self.history = self.history[-ACCEPTANCE_RATE_WINDOW:]

        # Check disable threshold
        if len(self.history) >= ACCEPTANCE_RATE_WINDOW and self.acceptance_rate < ACCEPTANCE_RATE_DISABLE_THRESHOLD:
            self.is_disabled = True
            self.disabled_at = time.time()
            logger.info(
                "Draft pair disabled: acceptance rate %.1f%% below threshold %.1f%%",
                self.acceptance_rate * 100,
                ACCEPTANCE_RATE_DISABLE_THRESHOLD * 100,
            )

    def __repr__(self) -> str:
        return f"_PairStats(rate={self.acceptance_rate:.2f}, total={self.total}, disabled={self.is_disabled})"


# -- DraftPairResolver ---------------------------------------------------------


class DraftPairResolver:
    """Finds optimal draft models for speculative decoding.

    Scans available models to find ones that share the same architecture
    family as the main model and fall within the 4-10x size ratio window.
    The draft model must also fit in available memory alongside the main model.
    """

    def __init__(self, vram_gb: float | None = None, ram_gb: float | None = None):
        """Configure the resolver with system resource limits.

        Args:
            vram_gb: Available GPU VRAM in GB, or None to use the default.
            ram_gb: Available system RAM in GB, or None to auto-detect.
        """
        if vram_gb is None:
            vram_gb = DEFAULT_VRAM_GB
            logger.info(
                "DraftPairResolver: GPU VRAM auto-detection requires a GPU inspection tool; using default vram_gb=%.1f",
                vram_gb,
            )
        if ram_gb is None:
            try:
                ram_gb = _detect_system_ram_gb()
            except Exception as exc:
                ram_gb = DEFAULT_RAM_GB
                logger.warning(
                    "DraftPairResolver: could not auto-detect system resources; using defaults "
                    "vram_gb=%.1f ram_gb=%.1f: %s",
                    vram_gb,
                    ram_gb,
                    exc,
                )
        self._vram_gb = float(vram_gb)
        self._ram_gb = float(ram_gb)
        self._pair_stats: dict[str, _PairStats] = {}
        self._lock = threading.Lock()

    def _is_pair_disabled(self, main_model_path: Path, draft_model_path: Path) -> bool:
        """Return whether a speculative pair is disabled by acceptance stats."""
        pair_key = f"{main_model_path.stem}:{draft_model_path.stem}"
        with self._lock:
            stats = self._pair_stats.get(pair_key)
            return bool(stats and stats.is_disabled)

    @staticmethod
    def _main_model_profile(main_model_path: Path) -> tuple[str, float] | None:
        """Return family and size for a valid main model."""
        try:
            from vetinari.models.model_profiler import detect_family, read_metadata
        except ImportError:
            logger.debug("ModelProfiler not available; cannot resolve draft pairs")
            return None
        main_meta = read_metadata(main_model_path)
        main_family = detect_family(main_meta.architecture)
        main_size = main_meta.file_size_gb
        if main_family == "unknown" or main_size <= 0:
            logger.debug("Cannot resolve draft pair: unknown family or zero-size main model")
            return None
        return main_family, main_size

    def _draft_candidate(
        self,
        main_model_path: Path,
        draft_model_path: Path,
        main_family: str,
        main_size: float,
    ) -> tuple[float, Path, float, str] | None:
        """Return a candidate tuple when a draft model satisfies pairing rules."""
        if draft_model_path == main_model_path or self._is_pair_disabled(main_model_path, draft_model_path):
            return None
        try:
            from vetinari.models.model_profiler import detect_family, read_metadata

            draft_meta = read_metadata(draft_model_path)
            draft_family = detect_family(draft_meta.architecture)
            draft_size = draft_meta.file_size_gb
        except Exception as exc:
            logger.warning("Failed to evaluate draft candidate %s: %s", draft_model_path.stem, exc)
            return None
        if draft_family != main_family or draft_size <= 0:
            return None
        ratio = main_size / draft_size
        if not (MIN_SIZE_RATIO <= ratio <= MAX_SIZE_RATIO):
            return None
        if main_size + draft_size > (self._vram_gb + self._ram_gb):
            return None
        return ratio, draft_model_path, draft_size, draft_family

    def _build_pair(
        self,
        main_model_path: Path,
        main_family: str,
        main_size: float,
        candidate: tuple[float, Path, float, str],
    ) -> DraftPair:
        """Build a DraftPair from the best candidate tuple."""
        best_ratio, best_path, best_size, best_family = candidate
        main_on_gpu = main_size <= self._vram_gb
        remaining_vram = max(0, self._vram_gb - main_size)
        draft_on_gpu = best_size <= remaining_vram
        return DraftPair(
            main_model_path=main_model_path,
            draft_model_path=best_path,
            main_family=main_family,
            draft_family=best_family,
            main_size_gb=main_size,
            draft_size_gb=best_size,
            size_ratio=round(best_ratio, 1),
            draft_gpu_layers=-1 if draft_on_gpu else 0,
            main_on_gpu=main_on_gpu,
            draft_on_gpu=draft_on_gpu,
        )

    def find_pair(
        self,
        main_model_path: Path,
        available_models: list[Path],
    ) -> DraftPair | None:
        """Find the best draft model for speculative decoding with the main model.

        Searches available models for candidates that:
        1. Share the same architecture family
        2. Are 4-10x smaller than the main model
        3. Fit in available VRAM+RAM alongside the main model
        4. Have not been disabled due to low acceptance rate

        Args:
            main_model_path: Path to the main model's .gguf file.
            available_models: List of paths to all available .gguf files.

        Returns:
            The best DraftPair, or None if no suitable draft model exists.
        """
        main_profile = self._main_model_profile(main_model_path)
        if main_profile is None:
            return None
        main_family, main_size = main_profile

        candidates: list[tuple[float, Path, float, str]] = []
        for model_path in available_models:
            candidate = self._draft_candidate(main_model_path, model_path, main_family, main_size)
            if candidate is not None:
                candidates.append(candidate)

        if not candidates:
            return None

        candidates.sort(key=lambda c: abs(c[0] - 6.0))
        pair = self._build_pair(main_model_path, main_family, main_size, candidates[0])
        logger.info(
            "Found draft pair: %s (%.1fGB) -> %s (%.1fGB), ratio=%.1fx, draft_gpu=%s",
            main_model_path.stem,
            main_size,
            pair.draft_model_path.stem,
            pair.draft_size_gb,
            pair.size_ratio,
            "GPU" if pair.draft_on_gpu else "CPU",
        )
        return pair

    def record_acceptance(self, main_model_id: str, draft_model_id: str, accepted: bool) -> None:
        """Record whether a speculative draft token was accepted.

        Args:
            main_model_id: Main model identifier.
            draft_model_id: Draft model identifier.
            accepted: Whether the draft token matched the main model's output.
        """
        pair_key = f"{main_model_id}:{draft_model_id}"
        with self._lock:
            if pair_key not in self._pair_stats:
                self._pair_stats[pair_key] = _PairStats()
            self._pair_stats[pair_key].record(accepted)

    def get_pair_stats(self, main_model_id: str, draft_model_id: str) -> dict[str, Any]:
        """Get acceptance statistics for a draft pair.

        Args:
            main_model_id: Main model identifier.
            draft_model_id: Draft model identifier.

        Returns:
            Dict with acceptance_rate, total, is_disabled fields.
        """
        pair_key = f"{main_model_id}:{draft_model_id}"
        with self._lock:
            stats = self._pair_stats.get(pair_key)
            if stats is None:
                return {"acceptance_rate": 1.0, "total": 0, "is_disabled": False}
            return {
                "acceptance_rate": stats.acceptance_rate,
                "total": stats.total,
                "is_disabled": stats.is_disabled,
            }

    def get_all_stats(self) -> dict[str, dict[str, Any]]:
        """Get acceptance statistics for all tracked pairs.

        Returns:
            Dict mapping pair key to stats dict.
        """
        with self._lock:
            return {
                key: {
                    "acceptance_rate": stats.acceptance_rate,
                    "total": stats.total,
                    "is_disabled": stats.is_disabled,
                }
                for key, stats in self._pair_stats.items()
            }


# -- Singleton -----------------------------------------------------------------

_resolver: DraftPairResolver | None = None
_resolver_lock = threading.Lock()


def get_draft_pair_resolver(vram_gb: float | None = None, ram_gb: float | None = None) -> DraftPairResolver:
    """Return the singleton DraftPairResolver.

    Args:
        vram_gb: Override VRAM (only on first call).
        ram_gb: Override RAM (only on first call).

    Returns:
        The shared DraftPairResolver instance.
    """
    global _resolver
    if _resolver is None:
        with _resolver_lock:
            if _resolver is None:
                _vram = vram_gb if vram_gb is not None else 24.0
                _ram = ram_gb if ram_gb is not None else 32.0
                _resolver = DraftPairResolver(vram_gb=_vram, ram_gb=_ram)
    return _resolver


def reset_draft_pair_resolver() -> None:
    """Reset the singleton (for testing)."""
    global _resolver
    with _resolver_lock:
        _resolver = None
