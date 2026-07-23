"""Disk and system resource monitoring for Vetinari.

Monitors available disk space and applies tiered thresholds to protect
against running out of storage. Threshold tiers:
OK (<80%), WARN (80-89%), PAUSE (90-94%), READ_ONLY (>=95%).
Safety buffer is proportional to total capacity (2% or 200MB minimum),
so small volumes are not overwhelmed by a flat 500MB reserve.
"""

from __future__ import annotations

import logging
import shutil
import threading
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from vetinari.constants import _PROJECT_ROOT

logger = logging.getLogger(__name__)


# Safety buffer: 2% of total volume, capped at 2GB, never below 200MB.
# A flat 500MB reserve overwhelms small (<25GB) volumes.
_SAFETY_BUFFER_RATIO = 0.02  # 2%
_SAFETY_BUFFER_MIN_BYTES = 200 * 1024 * 1024  # 200 MB floor
_SAFETY_BUFFER_MAX_BYTES = 2 * 1024 * 1024 * 1024  # 2 GB ceiling


class DiskThreshold(Enum):
    """Tiered disk usage thresholds controlling system behavior."""

    OK = "ok"
    WARN = "warn"
    PAUSE = "pause"
    READ_ONLY = "read_only"


@dataclass(frozen=True, slots=True)
class DiskStatus:
    """Snapshot of disk space at a point in time."""

    total_bytes: int
    used_bytes: int
    free_bytes: int
    usage_percent: float
    threshold: DiskThreshold
    path: str

    def __repr__(self) -> str:
        return (
            f"DiskStatus(usage={self.usage_percent:.1f}%, "
            f"free={self.free_bytes / (1024**3):.1f}GB, "
            f"threshold={self.threshold.value})"
        )

    @property
    def is_ok(self) -> bool:
        return self.threshold == DiskThreshold.OK

    @property
    def should_pause_writes(self) -> bool:
        return self.threshold in (DiskThreshold.PAUSE, DiskThreshold.READ_ONLY)

    @property
    def is_read_only(self) -> bool:
        return self.threshold == DiskThreshold.READ_ONLY


def _classify_usage(usage_percent: float) -> DiskThreshold:
    """Map a usage percentage to the appropriate threshold tier."""
    if usage_percent >= 95.0:
        return DiskThreshold.READ_ONLY
    if usage_percent >= 90.0:
        return DiskThreshold.PAUSE
    if usage_percent >= 80.0:
        return DiskThreshold.WARN
    return DiskThreshold.OK


def _safety_buffer(total_bytes: int) -> int:
    """Compute a proportional safety buffer for a volume of the given size.

    Uses 2% of total capacity, bounded between 200 MB and 2 GB. A flat 500 MB
    buffer overwhelms small volumes (e.g. a 10 GB test volume loses 5% to the
    buffer alone) while providing too little headroom on large ones.

    Args:
        total_bytes: Total volume capacity in bytes.

    Returns:
        Safety buffer in bytes.
    """
    proportional = int(total_bytes * _SAFETY_BUFFER_RATIO)
    return max(_SAFETY_BUFFER_MIN_BYTES, min(_SAFETY_BUFFER_MAX_BYTES, proportional))


def check_disk_space(path: str | Path | None = None) -> DiskStatus:
    """Check disk space for the filesystem containing the given path.

    Fails closed to READ_ONLY (not WARN) when the disk is unreadable, so the
    system halts writes rather than proceeding optimistically with stale data.

    Args:
        path: Filesystem path to inspect. Defaults to the project root.

    Returns:
        DiskStatus with usage percentage and threshold classification.
    """
    check_path = str(path or _PROJECT_ROOT)
    try:
        usage = shutil.disk_usage(check_path)
    except OSError:
        # Fail closed at the write boundary — READ_ONLY prevents further writes
        # while the disk state is unknown. WARN would allow writes, which risks
        # data loss if the disk really is full.
        logger.warning(
            "Could not read disk usage for %s — failing closed to READ_ONLY threshold",
            check_path,
        )
        return DiskStatus(
            total_bytes=0,
            used_bytes=0,
            free_bytes=0,
            usage_percent=100.0,
            threshold=DiskThreshold.READ_ONLY,
            path=check_path,
        )

    buffer = _safety_buffer(usage.total)
    effective_free = max(0, usage.free - buffer)
    effective_used = usage.total - effective_free
    usage_percent = (effective_used / usage.total * 100) if usage.total > 0 else 0.0
    threshold = _classify_usage(usage_percent)

    # Keep used_bytes/free_bytes consistent with the effective (buffer-adjusted)
    # usage_percent so callers get an internally coherent snapshot.
    return DiskStatus(
        total_bytes=usage.total,
        used_bytes=effective_used,
        free_bytes=effective_free,
        usage_percent=round(usage_percent, 1),
        threshold=threshold,
        path=check_path,
    )


class ResourceMonitor:
    """Singleton monitor that periodically checks system resources.

    Caches per-path disk status so that different call sites monitoring
    different mount points don't collide. The cache is invalidated early
    when severe pressure (PAUSE or READ_ONLY) is detected — stale OK status
    must not persist for the full 60-second interval when the disk is almost
    full.
    """

    _instance: ResourceMonitor | None = None
    _class_lock = threading.Lock()

    def __new__(cls) -> ResourceMonitor:
        if cls._instance is None:
            with cls._class_lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._setup()
        return cls._instance

    def _setup(self) -> None:
        self._lock = threading.Lock()
        # Per-path cache: maps normalised path string -> (status, monotonic timestamp)
        self._cache: dict[str, tuple[DiskStatus, float]] = {}
        self._check_interval: float = 60.0
        # Under severe pressure, re-check every 5 s so the caller sees
        # the updated state quickly without waiting the full minute.
        self._severe_interval: float = 5.0
        self._total_checks: int = 0
        self._total_warnings: int = 0

    @staticmethod
    def _cache_key(path: str | Path | None) -> str:
        """Return a stable string key for the given path (or project root)."""
        return str(path or _PROJECT_ROOT)

    def check(self, path: str | Path | None = None) -> DiskStatus:
        """Check disk space, returning a cached result if checked recently.

        Each distinct path gets its own cache entry so that monitoring
        multiple mount points does not produce cross-contaminated results.
        Under severe pressure the cache TTL is shortened to 5 s so the
        caller sees relief (or further escalation) promptly.

        Thread-safe: the entire check-update cycle is held under one lock.

        Args:
            path: Filesystem path to check. Defaults to the project root.

        Returns:
            Current DiskStatus, served from cache when fresh.
        """
        now = time.monotonic()
        key = self._cache_key(path)

        with self._lock:
            cached = self._cache.get(key)
            if cached is not None:
                last_status, last_time = cached
                # Use shorter TTL under severe pressure so stale-OK cache
                # cannot mask an almost-full disk for the full interval.
                is_severe = last_status.threshold in (DiskThreshold.PAUSE, DiskThreshold.READ_ONLY)
                ttl = self._severe_interval if is_severe else self._check_interval
                if (now - last_time) < ttl:
                    return last_status

            status = check_disk_space(path)
            self._cache[key] = (status, now)
            self._total_checks += 1

            if status.threshold != DiskThreshold.OK:
                self._total_warnings += 1
                logger.warning(
                    "Disk space %s: %.1f%% used, %.1f GB free on %s",
                    status.threshold.value.upper(),
                    status.usage_percent,
                    status.free_bytes / (1024**3),
                    status.path,
                )

            return status

    def get_stats(self) -> dict[str, Any]:
        """Return monitoring statistics for all monitored paths.

        Returns:
            Dict with total_checks, total_warnings, and per-path last_status strings.
        """
        with self._lock:
            return {
                "total_checks": self._total_checks,
                "total_warnings": self._total_warnings,
                "paths": {k: repr(v[0]) for k, v in self._cache.items()},
            }

    def get_usage_percent(self) -> float:
        """Return the current project-root disk usage percentage.

        Returns:
            Disk usage percentage from the canonical disk-space check.
        """
        return self.check().usage_percent


def get_resource_monitor() -> ResourceMonitor:
    """Return the shared ResourceMonitor singleton."""
    return ResourceMonitor()


def reset_resource_monitor() -> None:
    """Destroy the singleton — intended for use in tests."""
    with ResourceMonitor._class_lock:
        ResourceMonitor._instance = None
