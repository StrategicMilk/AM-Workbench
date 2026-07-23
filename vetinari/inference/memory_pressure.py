"""Memory-pressure handoff monitor for the resident CPU tier."""

from __future__ import annotations

import logging
import sys
import threading

from vetinari.inference.cpu_tier import CpuTierInterface
from vetinari.inference.request import MemoryPressureConfig

logger = logging.getLogger(__name__)


# Side effects:
#   - Module-level _monitor_instance holds the single active MemoryPressureMonitor.
#   - _monitor_lock guards _monitor_instance reads and writes (singleton pattern).
_monitor_instance: MemoryPressureMonitor | None = None
_monitor_lock = threading.Lock()


class MemoryPressureMonitor:
    """Polls memory headroom and drives CPU-tier release/reload handoff."""

    def __init__(self, cpu_tier: CpuTierInterface, config: MemoryPressureConfig | dict) -> None:
        self._cpu_tier = cpu_tier
        self._config = config
        self._poll_interval_s = _cfg(config, "poll_interval_s", 1.0)
        self._release_threshold_mb = int(_cfg(config, "release_threshold_mb", 2048))
        self._reload_threshold_mb = int(_cfg(config, "reload_threshold_mb", 4096))
        self._release_timeout_s = float(_cfg(config, "release_timeout_s", 30.0))
        self._released_by_monitor = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Start the non-blocking background poller."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="memory-pressure-monitor", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop the background poller and join within five seconds."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)

    def _run(self) -> None:
        while not self._stop.is_set():
            self._poll_once()
            self._stop.wait(self._poll_interval_s)

    def _poll_once(self) -> None:
        free_mb = _get_system_free_mb()
        if free_mb < self._release_threshold_mb:
            ok = self._cpu_tier.request_release(reason="gpu_memory_pressure", timeout_s=self._release_timeout_s)
            if ok:
                self._released_by_monitor = True
            else:
                logger.warning("CPU tier did not release before memory-pressure timeout")
            return
        if self._released_by_monitor and free_mb > self._reload_threshold_mb:
            self._cpu_tier.load()
            self._released_by_monitor = False


def _cfg(config: MemoryPressureConfig | dict, key: str, default: float | int) -> float | int:
    return getattr(config, key) if isinstance(config, MemoryPressureConfig) else config.get(key, default)


def _get_system_free_mb() -> int:
    """Return system RAM free memory in MiB, or a never-triggering sentinel if unavailable.

    The memory-pressure protocol intentionally watches *system* free RAM rather
    than GPU VRAM. The CPU tier and training entry points share host RAM, so a
    drop in system RAM is the actionable signal for release/reload handoff.
    A future iteration can add a separate GPU-VRAM probe alongside this one.
    """
    try:
        import psutil

        return int(psutil.virtual_memory().available / (1024 * 1024))
    except Exception:
        logger.warning("system memory probe unavailable; memory-pressure release disabled", exc_info=True)
        return sys.maxsize


def get_memory_pressure_monitor(
    cpu_tier: CpuTierInterface, config: MemoryPressureConfig | dict
) -> MemoryPressureMonitor:
    """Return the process singleton memory-pressure monitor.

    Args:
        cpu_tier: Cpu tier value consumed by get_memory_pressure_monitor().
        config: Config value consumed by get_memory_pressure_monitor().

    Returns:
        Resolved memory pressure monitor value.
    """
    global _monitor_instance
    if _monitor_instance is not None:
        return _monitor_instance
    with _monitor_lock:
        if _monitor_instance is None:
            _monitor_instance = MemoryPressureMonitor(cpu_tier, config)
    return _monitor_instance


__all__ = ["MemoryPressureMonitor", "get_memory_pressure_monitor"]
