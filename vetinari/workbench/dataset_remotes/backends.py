"""Fail-closed dataset revision remote backends.

The first implementation deliberately models DVC, lakeFS, and Lance remotes as
typed local adapters. They do not contact external services; they enforce auth
references, offline behavior, conflict detection, durable receipts, and audit
records so the higher-level revision store can exercise the same contract
without depending on optional remote libraries.
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from vetinari.workbench.dataset_revision_records import (
    DatasetRemoteKind,
    DatasetRevision,
    DatasetRevisionAuthFailed,
    DatasetRevisionError,
    DatasetRevisionRemoteUnavailable,
    _to_jsonable,
)

_SUPPORTED_REMOTE_KINDS = frozenset(DatasetRemoteKind)
_MAX_AUDIT_LINES = 1_000


class DatasetRemoteConflict(DatasetRevisionError):
    """Raised when a remote revision already exists with different content."""


@dataclass(frozen=True, slots=True)
class DatasetRemoteConfig:
    """Configuration for a dataset remote backend."""

    kind: DatasetRemoteKind
    endpoint: str
    auth_ref: str
    root_path: Path
    offline: bool = False

    def __repr__(self) -> str:
        return (
            "DatasetRemoteConfig("
            f"kind={self.kind.value!r}, endpoint={self.endpoint!r}, root_path={str(self.root_path)!r}, "
            f"offline={self.offline!r})"
        )

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", DatasetRemoteKind(getattr(self.kind, "value", self.kind)))
        if not self.endpoint or not self.endpoint.strip():
            raise DatasetRevisionRemoteUnavailable("dataset remote endpoint must be configured")
        if not self.auth_ref or not self.auth_ref.strip():
            raise DatasetRevisionAuthFailed("dataset remote auth_ref must be configured; raw secrets are not accepted")
        if any(marker in self.auth_ref.lower() for marker in ("token=", "password=", "secret=", "api_key=")):
            raise DatasetRevisionAuthFailed(
                "dataset remote auth_ref must be an opaque reference, not credential material"
            )
        root = self.root_path.expanduser().resolve()
        object.__setattr__(self, "root_path", root)


@dataclass(frozen=True, slots=True)
class DatasetRemoteReceipt:
    """Receipt emitted for one dataset remote action."""

    action: str
    kind: str
    revision_id: str
    status: str
    created_at_utc: str
    audit_path: str
    conflict_id: str | None = None
    message: str = ""

    def __repr__(self) -> str:
        return (
            "DatasetRemoteReceipt("
            f"action={self.action!r}, kind={self.kind!r}, revision_id={self.revision_id!r}, "
            f"status={self.status!r})"
        )

    @property
    def passed(self) -> bool:
        """Whether the remote action succeeded."""
        return self.status == "accepted"

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible receipt payload."""
        return asdict(self)


class DatasetRemoteBackend:
    """Typed local implementation of a dataset remote contract."""

    def __init__(self, config: DatasetRemoteConfig) -> None:
        if config.kind not in _SUPPORTED_REMOTE_KINDS:
            raise DatasetRevisionRemoteUnavailable(f"unsupported dataset remote kind {config.kind!r}")
        self.config = config
        self._lock = threading.RLock()
        self._remote_dir = (config.root_path / config.kind.value).resolve()
        if not self._remote_dir.is_relative_to(config.root_path):
            raise DatasetRevisionRemoteUnavailable("dataset remote path escapes configured root")
        self._audit_path = self._remote_dir / "audit.jsonl"

    def push(self, revision: DatasetRevision) -> DatasetRemoteReceipt:
        """Push one revision to the remote store.

        Returns:
            Accepted receipt when the remote write succeeds.

        Raises:
            DatasetRevisionRemoteUnavailable: If the remote is offline or unavailable.
            DatasetRemoteConflict: If the remote already has different content.
        """
        self._require_online("push", revision.revision_id)
        payload = _to_jsonable(revision)
        payload_hash = _stable_hash(payload)
        with self._lock:
            self._remote_dir.mkdir(parents=True, exist_ok=True)
            target = self._revision_path(revision.revision_id)
            if target.exists():
                existing = json.loads(target.read_text(encoding="utf-8"))
                if existing.get("payload_hash") != payload_hash:
                    conflict_id = f"conflict-{revision.revision_id}"
                    receipt = self._receipt(
                        "push",
                        revision.revision_id,
                        "conflict",
                        f"remote revision {revision.revision_id!r} already exists with different content",
                        conflict_id=conflict_id,
                    )
                    raise DatasetRemoteConflict(receipt.message)
            _write_json_atomic(target, {"payload_hash": payload_hash, "revision": payload})
            return self._receipt("push", revision.revision_id, "accepted", "revision pushed")

    def pull(self, revision_id: str) -> tuple[dict[str, Any], DatasetRemoteReceipt]:
        """Pull one revision payload from the remote store.

        Returns:
            The revision payload and accepted receipt.

        Raises:
            DatasetRevisionRemoteUnavailable: If the revision is absent or the remote is unavailable.
            DatasetRevisionError: If the revision id is invalid.
        """
        self._require_online("pull", revision_id)
        with self._lock:
            target = self._revision_path(revision_id)
            if not target.exists():
                receipt = self._receipt("pull", revision_id, "rejected", "remote revision not found")
                raise DatasetRevisionRemoteUnavailable(receipt.message)
            payload = json.loads(target.read_text(encoding="utf-8"))
            return payload["revision"], self._receipt("pull", revision_id, "accepted", "revision pulled")

    def sync(self, revision: DatasetRevision) -> DatasetRemoteReceipt:
        """Synchronize one local revision by pushing it and reading it back.

        Returns:
            Accepted receipt after push and verification readback.
        """
        pushed = self.push(revision)
        self.pull(revision.revision_id)
        return self._receipt("sync", revision.revision_id, pushed.status, "revision synchronized")

    def _require_online(self, action: str, revision_id: str) -> None:
        if self.config.offline:
            receipt = self._receipt(action, revision_id, "offline", "remote is offline; mutation refused")
            raise DatasetRevisionRemoteUnavailable(receipt.message)

    def _revision_path(self, revision_id: str) -> Path:
        if not revision_id or ".." in revision_id or "/" in revision_id or "\\" in revision_id:
            raise DatasetRevisionError(f"invalid remote revision id {revision_id!r}")
        return self._remote_dir / f"{revision_id}.json"

    def _receipt(
        self,
        action: str,
        revision_id: str,
        status: str,
        message: str,
        *,
        conflict_id: str | None = None,
    ) -> DatasetRemoteReceipt:
        receipt = DatasetRemoteReceipt(
            action=action,
            kind=self.config.kind.value,
            revision_id=revision_id,
            status=status,
            created_at_utc=datetime.now(timezone.utc).isoformat(),
            audit_path=str(self._audit_path),
            conflict_id=conflict_id,
            message=message,
        )
        self._append_audit(receipt)
        return receipt

    def _append_audit(self, receipt: DatasetRemoteReceipt) -> None:
        with self._lock:
            self._remote_dir.mkdir(parents=True, exist_ok=True)
            line = json.dumps(receipt.to_dict(), sort_keys=True) + "\n"
            with self._audit_path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(line)
                handle.flush()
                os.fsync(handle.fileno())
            _prune_jsonl(self._audit_path)


def remote_backend_for(kind: DatasetRemoteKind | str, config: DatasetRemoteConfig) -> DatasetRemoteBackend:
    """Return a backend for a declared remote kind.

    Args:
        kind: Requested remote kind.
        config: Validated remote backend config.

    Returns:
        Dataset remote backend for the requested kind.

    Raises:
        DatasetRevisionRemoteUnavailable: If requested kind and config kind differ.
    """
    coerced = DatasetRemoteKind(getattr(kind, "value", kind))
    if coerced != config.kind:
        raise DatasetRevisionRemoteUnavailable(
            f"remote config kind {config.kind.value!r} does not match {coerced.value!r}"
        )
    return DatasetRemoteBackend(config)


def supported_remote_kinds() -> tuple[DatasetRemoteKind, ...]:
    """Return every dataset remote kind implemented by this package."""
    return tuple(sorted(_SUPPORTED_REMOTE_KINDS, key=lambda item: item.value))


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = _atomic_temp_path(path)
    try:
        with tmp_path.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(payload, indent=2, sort_keys=True))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def _prune_jsonl(path: Path, *, max_lines: int = _MAX_AUDIT_LINES) -> None:
    lines = path.read_text(encoding="utf-8").splitlines()
    if len(lines) <= max_lines:
        return
    _write_text_atomic(path, "\n".join(lines[-max_lines:]) + "\n")


def _write_text_atomic(path: Path, text: str) -> None:
    tmp_path = _atomic_temp_path(path)
    try:
        with tmp_path.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def _stable_hash(payload: dict[str, Any]) -> str:
    import hashlib

    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _atomic_temp_path(path: Path) -> Path:
    return path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex}.tmp")
