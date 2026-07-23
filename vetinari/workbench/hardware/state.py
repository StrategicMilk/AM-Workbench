"""Locked, project-scoped snapshot persistence."""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from vetinari.constants import OUTPUTS_DIR
from vetinari.workbench.hardware.contracts import (
    HardwareTwinError,
    HardwareTwinSnapshot,
    MeasurementObservation,
    MeasurementStatus,
    RuntimeFingerprint,
)

logger = logging.getLogger(__name__)


DEFAULT_HARDWARE_STATE_ROOT = OUTPUTS_DIR / "workbench" / "spine" / "hardware"
_SAFE_PROJECT_ID = re.compile(r"^[A-Za-z0-9_.-]+$")


@dataclass(frozen=True, slots=True)
class HardwareStateReadResult:
    """Fail-closed result from reading hardware twin state."""

    status: MeasurementStatus
    snapshot: HardwareTwinSnapshot | None
    reasons: tuple[str, ...]

    @property
    def trusted(self) -> bool:
        """Whether the read returned a ready snapshot."""
        return self.status is MeasurementStatus.READY and self.snapshot is not None and self.snapshot.ready


class HardwareTwinStateStore:
    """Persist snapshots under a project-scoped root with a lock and atomic replace."""

    def __init__(
        self, *, state_root: Path | str = DEFAULT_HARDWARE_STATE_ROOT, lock_timeout_seconds: float = 2.0
    ) -> None:
        self.state_root = Path(state_root)
        self.lock_timeout_seconds = lock_timeout_seconds
        if lock_timeout_seconds <= 0:
            raise HardwareTwinError("lock-timeout-invalid")

    def snapshot_path(self, project_id: str) -> Path:
        """Return the scoped snapshot path, rejecting traversal or absolute ids.

        Returns:
            Path value produced by snapshot_path().

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        clean_project_id = _sanitize_project_id(project_id)
        root = self.state_root.resolve()
        path = (root / clean_project_id / "hardware_twin.json").resolve()
        if root != path and root not in path.parents:
            raise HardwareTwinError("state-path-outside-root", str(path))
        return path

    def write_snapshot(self, snapshot: HardwareTwinSnapshot) -> Path:
        """Write a snapshot atomically while holding a sibling lock file.

        Returns:
            Path value produced by write_snapshot().
        """
        path = self.snapshot_path(snapshot.project_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = path.with_suffix(path.suffix + ".lock")
        with _HardwareFileLock(lock_path, self.lock_timeout_seconds):
            payload = json.dumps(snapshot.to_dict(), sort_keys=True, indent=2)
            fd, tmp_name = tempfile.mkstemp(prefix=f"{path.name}.", suffix=".tmp", dir=path.parent)
            tmp_path = Path(tmp_name)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(payload)
                    handle.write("\n")
                os.replace(tmp_path, path)
            finally:
                with contextlib_suppress_oserror():
                    tmp_path.unlink()
        return path

    def read_snapshot(self, project_id: str) -> HardwareStateReadResult:
        """Read a snapshot, returning degraded state on missing/corrupt data.

        Returns:
            Resolved snapshot value.
        """
        path = self.snapshot_path(project_id)
        if not path.exists():
            return HardwareStateReadResult(MeasurementStatus.DEGRADED, None, ("snapshot-missing",))
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            snapshot = _snapshot_from_dict(raw)
        except (OSError, json.JSONDecodeError, KeyError, TypeError, HardwareTwinError) as exc:
            logger.warning("Handled recoverable failure before fallback.", exc_info=True)
            return HardwareStateReadResult(
                MeasurementStatus.DEGRADED,
                None,
                (f"snapshot-unreadable:{type(exc).__name__}",),
            )
        if _sanitize_project_id(snapshot.project_id) != _sanitize_project_id(project_id):
            return HardwareStateReadResult(MeasurementStatus.DEGRADED, None, ("snapshot-project-id-mismatch",))
        if not snapshot.ready:
            return HardwareStateReadResult(MeasurementStatus.DEGRADED, snapshot, snapshot.degradation_reasons)
        return HardwareStateReadResult(MeasurementStatus.READY, snapshot, ())


class contextlib_suppress_oserror:
    """Tiny context manager to avoid importing contextlib for one cleanup path."""

    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> bool:
        return isinstance(exc, OSError)


class _HardwareFileLock:
    def __init__(self, path: Path, timeout_seconds: float) -> None:
        self.path = path
        self.timeout_seconds = timeout_seconds
        self.fd: int | None = None

    def __enter__(self) -> None:
        deadline = time.monotonic() + self.timeout_seconds
        while True:
            try:
                self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(self.fd, str(os.getpid()).encode("ascii"))
                return None
            except FileExistsError as exc:
                if time.monotonic() >= deadline:
                    raise HardwareTwinError("state-lock-timeout", str(self.path)) from exc
                time.sleep(0.02)

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if self.fd is not None:
            os.close(self.fd)
        with contextlib_suppress_oserror():
            self.path.unlink()


def _sanitize_project_id(project_id: str) -> str:
    if not isinstance(project_id, str) or not project_id.strip():
        raise HardwareTwinError("project-id-missing")
    if "/" in project_id or "\\" in project_id or ".." in project_id or Path(project_id).is_absolute():
        raise HardwareTwinError("project-id-traversal", project_id)
    if not _SAFE_PROJECT_ID.match(project_id):
        raise HardwareTwinError("project-id-invalid", project_id)
    return project_id


def _snapshot_from_dict(raw: dict[str, Any]) -> HardwareTwinSnapshot:
    observations = tuple(MeasurementObservation(**item) for item in raw["observations"])
    fingerprint_payload = raw.get("fingerprint")
    fingerprint = RuntimeFingerprint(**fingerprint_payload) if isinstance(fingerprint_payload, dict) else None
    return HardwareTwinSnapshot(
        snapshot_id=str(raw["snapshot_id"]),
        project_id=str(raw["project_id"]),
        generated_at_utc=str(raw["generated_at_utc"]),
        observations=observations,
        fingerprint=fingerprint,
        evidence_ids=tuple(str(item) for item in raw["evidence_ids"]),
        status=str(raw["status"]),
        degradation_reasons=tuple(str(item) for item in raw.get("degradation_reasons", ())),
        schema_version=int(raw.get("schema_version", 1)),
    )


__all__ = [
    "DEFAULT_HARDWARE_STATE_ROOT",
    "HardwareStateReadResult",
    "HardwareTwinStateStore",
]
