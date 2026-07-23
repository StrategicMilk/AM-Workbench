"""Locked persistence for redacted network transport state."""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from vetinari.workbench.network.contracts import NetworkEvidenceStatus, NetworkObservation, NetworkTransportError
from vetinari.workbench.network.redaction import assert_redacted, redact_network_evidence
from vetinari.workbench.spine_consumers import record_trace_written

logger = logging.getLogger(__name__)


DEFAULT_NETWORK_STATE_ROOT = Path("outputs") / "workbench" / "spine" / "network_transport"
_SAFE_PROJECT_ID = re.compile(r"^[A-Za-z0-9_.-]+$")


@dataclass(frozen=True, slots=True)
class NetworkStateReadResult:
    """Fail-closed result from reading network state."""

    status: NetworkEvidenceStatus
    observations: tuple[NetworkObservation, ...]
    reasons: tuple[str, ...]

    @property
    def trusted(self) -> bool:
        return self.status is NetworkEvidenceStatus.READY and bool(self.observations) and not self.reasons


class NetworkTransportStateStore:
    """Persist redacted observations under a project-scoped root."""

    def __init__(
        self, *, state_root: Path | str = DEFAULT_NETWORK_STATE_ROOT, lock_timeout_seconds: float = 2.0
    ) -> None:
        self.state_root = Path(state_root)
        self.lock_timeout_seconds = lock_timeout_seconds
        if lock_timeout_seconds <= 0:
            raise NetworkTransportError("lock-timeout-invalid")

    def state_path(self, project_id: str) -> Path:
        """Execute the state path operation.

        Returns:
            Path value produced by state_path().

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        clean_project_id = _sanitize_project_id(project_id)
        root = self.state_root.resolve()
        path = (root / clean_project_id / "network_transport.json").resolve()
        if root != path and root not in path.parents:
            raise NetworkTransportError("state-path-outside-root", str(path))
        return path

    def write_observations(self, project_id: str, observations: tuple[NetworkObservation, ...]) -> Path:
        """Execute the write observations operation.

        Args:
            project_id: Project identifier that scopes the operation.
            observations: Observations value consumed by write_observations().

        Returns:
            Path value produced by write_observations().

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        if not observations:
            raise NetworkTransportError("observations-missing")
        payload = {
            "project_id": _sanitize_project_id(project_id),
            "observations": [item.to_dict() for item in observations],
        }
        payload = redact_network_evidence(payload)
        assert_redacted(payload)
        path = self.state_path(project_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with _NetworkFileLock(path.with_suffix(path.suffix + ".lock"), self.lock_timeout_seconds):
            fd, tmp_name = tempfile.mkstemp(prefix=f"{path.name}.", suffix=".tmp", dir=path.parent)
            tmp_path = Path(tmp_name)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(payload, handle, sort_keys=True, indent=2)
                    handle.write("\n")
                os.replace(tmp_path, path)
                # spine_consumers invokes get_spine() and absorbs observability failures.
                record_trace_written(
                    trace_id=f"network-state-{project_id}",
                    query_hash="network_transport",
                    project_id=project_id,
                )
            finally:
                with _NetworkSuppressOSError():
                    tmp_path.unlink()
        return path

    def read_observations(self, project_id: str) -> NetworkStateReadResult:
        """Execute the read observations operation.

        Returns:
            Resolved observations value.
        """
        path = self.state_path(project_id)
        if not path.exists():
            return NetworkStateReadResult(NetworkEvidenceStatus.DEGRADED, (), ("network-state-missing",))
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            assert_redacted(raw)
            observations = tuple(NetworkObservation(**item) for item in raw["observations"])
        except (OSError, json.JSONDecodeError, KeyError, TypeError, NetworkTransportError) as exc:
            logger.warning("Handled recoverable failure before fallback.", exc_info=True)
            return NetworkStateReadResult(
                NetworkEvidenceStatus.DEGRADED, (), (f"network-state-unreadable:{type(exc).__name__}",)
            )
        if _sanitize_project_id(str(raw.get("project_id", ""))) != _sanitize_project_id(project_id):
            return NetworkStateReadResult(NetworkEvidenceStatus.DEGRADED, observations, ("project-id-mismatch",))
        if any(item.status is not NetworkEvidenceStatus.READY for item in observations):
            return NetworkStateReadResult(NetworkEvidenceStatus.DEGRADED, observations, ("observation-not-ready",))
        return NetworkStateReadResult(NetworkEvidenceStatus.READY, observations, ())


class _NetworkSuppressOSError:
    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> bool:
        return isinstance(exc, OSError)


class _NetworkFileLock:
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
                    raise NetworkTransportError("network-state-lock-timeout", str(self.path)) from exc
                time.sleep(0.02)

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if self.fd is not None:
            os.close(self.fd)
        with _NetworkSuppressOSError():
            self.path.unlink()


def _sanitize_project_id(project_id: str) -> str:
    if not isinstance(project_id, str) or not project_id.strip():
        raise NetworkTransportError("project-id-missing")
    if "/" in project_id or "\\" in project_id or ".." in project_id or Path(project_id).is_absolute():
        raise NetworkTransportError("project-id-traversal", project_id)
    if not _SAFE_PROJECT_ID.match(project_id):
        raise NetworkTransportError("project-id-invalid", project_id)
    return project_id


__all__ = ["DEFAULT_NETWORK_STATE_ROOT", "NetworkStateReadResult", "NetworkTransportStateStore"]
