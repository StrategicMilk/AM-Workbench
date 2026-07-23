"""Durable Workbench metadata spine.

The spine appends typed Asset, Run, Trace, Eval, Proposal, Lease, and
Promotion records, then emits exactly one SPINE_EVENT WorkReceipt for
each successful write. It opens ``outputs/workbench/spine`` only when a
WorkbenchSpine is constructed; imports are side-effect free.

JSONL is the source of truth. SQLite is a rebuildable index used for
queries and dependency checks.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import threading
import time
from contextlib import suppress
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

from vetinari.constants import OUTPUTS_DIR
from vetinari.receipts.store import WorkReceiptStore
from vetinari.workbench.assets import AssetKind, WorkbenchAsset
from vetinari.workbench.cost.token_cost_split import (
    JsonlRotationSettings,
    PricingConfigError,
    load_rotation_settings,
)
from vetinari.workbench.evals import EvalResult
from vetinari.workbench.leases import WorkbenchLease
from vetinari.workbench.metadata_spine_log import _WorkbenchSpineLogMixin
from vetinari.workbench.metadata_spine_records import (
    WorkbenchSpineCorrupt,
    _to_jsonable,
)
from vetinari.workbench.metadata_spine_sqlite import _WorkbenchSpineSqliteMixin
from vetinari.workbench.metadata_spine_validation import _WorkbenchSpineValidationMixin
from vetinari.workbench.proposals import (
    Promotion,
    ProposalStatus,
    WorkbenchProposal,
    WorkbenchProposalKind,
)
from vetinari.workbench.runs import RunKind, RunStatus, WorkbenchRun
from vetinari.workbench.traces import WorkbenchTrace

logger = logging.getLogger(__name__)


_WORKBENCH_SPINE_DIR_ENV = "VETINARI_WORKBENCH_SPINE_DIR"
_JSONL_FILENAME = "spine.jsonl"
_SQLITE_FILENAME = "spine.sqlite"
_RUST_AUTHORITY_LOG_FILENAME = "spine.rust.jsonl"
_RUST_AUTHORITY_MANIFEST_FILENAME = "spine.rust-authority.json"
_SCHEMA_VERSION = 1
_LOCK_TIMEOUT_SECONDS = 10.0
_LOCK_POLL_SECONDS = 0.05
_ROTATION_KEY = "metadata_spine_jsonl"

# Allowlist for project_id components: alphanumeric, underscore, hyphen only.
# Leading/trailing whitespace, path separators, and parent-traversal markers
# are all rejected to prevent filesystem path-traversal attacks.
_PROJECT_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


class _SpineFileLock:
    """Small cross-process lock for append-log writes."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._fh: Any | None = None

    def __enter__(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self._path.open("a+b")
        self._fh.seek(0, os.SEEK_END)
        if self._fh.tell() == 0:
            self._fh.write(b"\0")
            self._fh.flush()
        self._fh.seek(0)
        if os.name == "nt":
            msvcrt = __import__("msvcrt")
            deadline = time.monotonic() + _LOCK_TIMEOUT_SECONDS
            while True:
                try:
                    msvcrt.locking(self._fh.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError as exc:
                    if time.monotonic() >= deadline:
                        self._fh.close()
                        self._fh = None
                        raise WorkbenchSpineCorrupt("spine lock acquisition timed out", path=self._path) from exc
                    time.sleep(_LOCK_POLL_SECONDS)
        else:
            fcntl = __import__("fcntl")
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX)

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        if self._fh is None:
            return
        try:
            self._fh.seek(0)
            if os.name == "nt":
                msvcrt = __import__("msvcrt")
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl = __import__("fcntl")
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        finally:
            self._fh.close()
            self._fh = None


def _default_spine_dir() -> Path:
    """Return the default spine directory, honoring runtime test/user isolation."""
    override = os.environ.get(_WORKBENCH_SPINE_DIR_ENV)
    if override:
        return Path(override)
    user_dir = os.environ.get("VETINARI_USER_DIR")
    if user_dir:
        return Path(user_dir) / "outputs" / "workbench" / "spine"
    return OUTPUTS_DIR / "workbench" / "spine"


def _spine_rotation_settings() -> JsonlRotationSettings:
    """Return Workbench spine JSONL rotation settings from resource pricing."""
    try:
        return load_rotation_settings(_ROTATION_KEY)
    except PricingConfigError:
        logger.warning("Workbench spine rotation config unavailable; using defaults", exc_info=True)
        return JsonlRotationSettings(max_bytes=1_048_576, max_lines=10_000, backup_count=10)


class WorkbenchSpine(_WorkbenchSpineLogMixin, _WorkbenchSpineSqliteMixin, _WorkbenchSpineValidationMixin):
    """Append-only Workbench metadata spine with a rebuildable SQLite index."""

    def __init__(
        self,
        store_dir: Path | None = None,
        *,
        project_id: str = "default",
        receipt_store: WorkReceiptStore | None = None,
        skip_corrupt_lines: bool = False,
    ) -> None:
        if not project_id or not _PROJECT_ID_RE.fullmatch(project_id):
            raise ValueError(
                f"project_id must match [A-Za-z0-9_-]+ (got {project_id!r}); "
                "leading/trailing whitespace, path separators, and parent-traversal "
                "markers are rejected"
            )
        self._store_dir = Path(store_dir) if store_dir is not None else _default_spine_dir()
        self._jsonl_path = self._store_dir / _JSONL_FILENAME
        self._sqlite_path = self._store_dir / _SQLITE_FILENAME
        self._rust_jsonl_path = self._store_dir / _RUST_AUTHORITY_LOG_FILENAME
        self._rust_manifest_path = self._store_dir / _RUST_AUTHORITY_MANIFEST_FILENAME
        self._lock_path = self._store_dir / "spine.lock"
        self._project_id = project_id
        self._receipt_store = receipt_store if receipt_store is not None else WorkReceiptStore()
        self._write_lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None

        try:
            self._store_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise WorkbenchSpineCorrupt(
                "spine directory unreadable or could not be created",
                path=self._store_dir,
            ) from exc
        if not self._store_dir.is_dir():
            raise WorkbenchSpineCorrupt(
                "spine directory unreadable or could not be created",
                path=self._store_dir,
            )

        if self._sqlite_path.exists() and not self._jsonl_path.exists():
            raise WorkbenchSpineCorrupt(
                "JSONL missing but SQLite present - append-log is the source of truth",
                path=self._jsonl_path,
            )
        if not self._jsonl_path.exists():
            self._jsonl_path.touch()

        records = self._load_jsonl_records(include_archives=True, skip_corrupt_lines=skip_corrupt_lines)
        self._records = records
        self._last_jsonl_size = self._jsonl_path.stat().st_size
        sqlite_existed = self._sqlite_path.exists()
        if sqlite_existed:
            self._check_sqlite_integrity()
        else:
            logger.warning("Rebuilding SQLite spine index from %s", self._jsonl_path)

        self._conn = sqlite3.connect(self._sqlite_path, check_same_thread=False, isolation_level=None)
        self._configure_sqlite()
        self._create_schema()
        self._rebuild_sqlite_from_records(records)
        if self._rust_jsonl_path.exists() or self._rust_manifest_path.exists():
            self._verify_rust_authority(records)
        self._sync_rust_authority(records)

    def append_asset(self, asset: WorkbenchAsset) -> None:
        """Execute the append asset operation."""
        self._append("asset", asset.asset_id, asset)

    def append_run(self, run: WorkbenchRun) -> None:
        """Execute the append run operation."""
        self._append("run", run.run_id, run)

    def append_trace(self, trace: WorkbenchTrace) -> None:
        """Execute the append trace operation."""
        self._append("trace", trace.trace_id, trace)

    def append_eval(self, eval_result: EvalResult) -> None:
        """Execute the append eval operation."""
        self._append("eval", eval_result.eval_id, eval_result)

    def append_proposal(self, proposal: WorkbenchProposal) -> None:
        """Execute the append proposal operation."""
        self._append("proposal", proposal.proposal_id, proposal)

    def append_lease(self, lease: WorkbenchLease) -> None:
        """Execute the append lease operation."""
        self._append("lease", lease.lease_id, lease)

    def record_promotion(self, promotion: Promotion) -> None:
        """Execute the record promotion operation."""
        self._append("promotion", promotion.promotion_id, promotion)

    def delete_record(self, kind: str, record_id: str, *, reason: str = "", deleted_at_utc: str | None = None) -> None:
        """Append a retention tombstone and remove the record from the rebuildable index.

        Args:
            kind: Record family to delete from the active index.
            record_id: Stable record identifier to tombstone.
            reason: Optional operator-visible delete reason.
            deleted_at_utc: Optional UTC timestamp override for deterministic migrations.

        Raises:
            WorkbenchSpineCorrupt: If the target kind or identifier is invalid.
        """
        if kind not in {"asset", "run", "trace", "eval", "proposal", "lease", "promotion"}:
            raise WorkbenchSpineCorrupt(f"unsupported delete target kind {kind!r}")
        if not record_id:
            raise WorkbenchSpineCorrupt("delete target record_id must be non-empty")
        deleted_at = deleted_at_utc or datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        self._append(
            "delete",
            f"delete:{kind}:{record_id}:{time.time_ns()}",
            {
                "target_kind": kind,
                "target_record_id": record_id,
                "reason": reason,
                "deleted_at_utc": deleted_at,
            },
        )

    def list_assets(
        self,
        *,
        kind: AssetKind | None = None,
        taints_present: bool | None = None,
        limit: int | None = None,
    ) -> list[WorkbenchAsset]:
        """Execute the list assets operation.

        Returns:
            Collection of assets values.
        """
        rows = [self._record_from_row(row) for row in self._select("asset", limit=limit)]
        assets = [row for row in rows if isinstance(row, WorkbenchAsset)]
        if kind is not None:
            assets = [asset for asset in assets if asset.kind is kind]
        if taints_present is not None:
            assets = [asset for asset in assets if bool(asset.taints) is taints_present]
        return assets

    def list_runs(
        self,
        *,
        kind: RunKind | None = None,
        status: RunStatus | None = None,
        lease_id: str | None = None,
        limit: int | None = None,
    ) -> list[WorkbenchRun]:
        """Execute the list runs operation.

        Returns:
            Collection of runs values.
        """
        rows = [self._record_from_row(row) for row in self._select("run", limit=limit)]
        runs = [row for row in rows if isinstance(row, WorkbenchRun)]
        if kind is not None:
            runs = [run for run in runs if run.kind is kind]
        if status is not None:
            runs = [run for run in runs if run.status is status]
        if lease_id is not None:
            runs = [run for run in runs if run.lease_id == lease_id]
        return runs

    def list_traces_for_run(self, run_id: str) -> list[WorkbenchTrace]:
        """Execute the list traces for run operation.

        Returns:
            Collection of traces for run values.
        """
        rows = [self._record_from_row(row) for row in self._select("trace")]
        traces = [row for row in rows if isinstance(row, WorkbenchTrace)]
        return [trace for trace in traces if trace.run_id == run_id]

    def list_evals(
        self,
        *,
        asset_id: str | None = None,
        run_id: str | None = None,
        limit: int | None = None,
    ) -> list[EvalResult]:
        """Execute the list evals operation.

        Returns:
            Collection of evals values.
        """
        rows = [self._record_from_row(row) for row in self._select("eval", limit=limit)]
        evals = [row for row in rows if isinstance(row, EvalResult)]
        if asset_id is not None:
            evals = [result for result in evals if result.asset_id == asset_id]
        if run_id is not None:
            evals = [result for result in evals if result.run_id == run_id]
        return evals

    def list_proposals(
        self,
        *,
        status: ProposalStatus | None = None,
        kind: WorkbenchProposalKind | None = None,
        limit: int | None = None,
    ) -> list[WorkbenchProposal]:
        """Execute the list proposals operation.

        Returns:
            Collection of proposals values.
        """
        rows = [self._record_from_row(row) for row in self._select("proposal", limit=limit)]
        proposals = [row for row in rows if isinstance(row, WorkbenchProposal)]
        if status is not None:
            proposals = [proposal for proposal in proposals if proposal.status is status]
        if kind is not None:
            proposals = [proposal for proposal in proposals if proposal.kind is kind]
        return proposals

    def list_leases(
        self,
        *,
        run_id: str | None = None,
        lane: Any | None = None,
    ) -> list[WorkbenchLease]:
        """Execute the list leases operation.

        Returns:
            Collection of leases values.
        """
        rows = [self._record_from_row(row) for row in self._select("lease")]
        leases = [row for row in rows if isinstance(row, WorkbenchLease)]
        if run_id is not None:
            leases = [lease for lease in leases if lease.requested_for_run_id == run_id]
        if lane is not None:
            leases = [lease for lease in leases if lease.lane is lane]
        return leases

    def list_record_payloads(self, kind: str, *, limit: int | None = None) -> tuple[dict[str, Any], ...]:
        """Return decoded spine payloads for one public record kind.

        Returns:
            Decoded payload dictionaries for the requested spine record kind.

        Raises:
            WorkbenchSpineCorrupt: If the requested kind is not a public spine record kind.
        """
        if kind not in {"asset", "run", "trace", "eval", "proposal", "lease", "promotion"}:
            raise WorkbenchSpineCorrupt(f"unsupported record kind {kind!r}", path=self._jsonl_path)
        payloads: list[dict[str, Any]] = []
        for row_kind, record_id, payload_json in self._select(kind, limit=limit):
            payload = json.loads(payload_json)
            payload.setdefault("kind", row_kind)
            payload.setdefault("record_id", record_id)
            payloads.append(payload)
        return tuple(payloads)

    def close(self) -> None:
        """Close the SQLite index connection."""
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    @staticmethod
    def _spine_rotation_settings() -> JsonlRotationSettings:
        return _spine_rotation_settings()

    def _append(self, kind: str, record_id: str, record: Any) -> None:
        if self._conn is None:
            raise WorkbenchSpineCorrupt("spine connection is closed", path=self._sqlite_path)
        payload = _to_jsonable(record)
        envelope = {
            "schema_version": _SCHEMA_VERSION,
            "kind": kind,
            "record_id": record_id,
            "payload": payload,
        }
        line = json.dumps(envelope, separators=(",", ":"), sort_keys=True) + "\n"
        with self._write_lock, _SpineFileLock(self._lock_path):
            self._rotate_jsonl_if_needed(len(line.encode("utf-8")))
            previous_jsonl_size = self._jsonl_path.stat().st_size
            if previous_jsonl_size != self._last_jsonl_size:
                self._records = self._load_jsonl_records(include_archives=True)
                self._last_jsonl_size = previous_jsonl_size
            jsonl_appended = False
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                self._validate_dependencies(kind, payload)
                self._append_jsonl_line(line)
                jsonl_appended = True
                self._insert_record(kind, record_id, payload)
                # COMMIT before writing external Rust-authority files so that a
                # failed COMMIT cannot leave the Rust files ahead of SQLite.
                self._conn.execute("COMMIT")
                self._records.append(envelope)
                self._last_jsonl_size = self._jsonl_path.stat().st_size
            except Exception:
                with suppress(sqlite3.DatabaseError):
                    self._conn.execute("ROLLBACK")
                if jsonl_appended:
                    self._rollback_jsonl_append(previous_jsonl_size)
                    with suppress(Exception):
                        self._records = self._load_jsonl_records(include_archives=True)
                        self._last_jsonl_size = self._jsonl_path.stat().st_size
                        self._sync_rust_authority(self._records)
                raise
            # Sync Rust-authority files after the SQLite COMMIT succeeds so
            # the two stores are never ahead of each other.
            try:
                self._sync_rust_authority(self._records)
            except Exception:
                logger.warning(
                    "Workbench spine rust-authority sync failed after committed append kind=%s record_id=%s",
                    kind,
                    record_id,
                    exc_info=True,
                )
            try:
                self._emit_receipt(kind, record_id)
            except Exception:
                logger.warning(
                    "Workbench spine receipt emission failed after committed append kind=%s record_id=%s",
                    kind,
                    record_id,
                    exc_info=True,
                )

    def _rollback_jsonl_append(self, previous_jsonl_size: int) -> None:
        try:
            with self._jsonl_path.open("r+b") as handle:
                handle.truncate(previous_jsonl_size)
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            raise WorkbenchSpineCorrupt("JSONL rollback truncate failed", path=self._jsonl_path) from exc

    def _sync_rust_authority(self, records: list[dict[str, Any]]) -> None:
        """Mirror the Python append log into the Rust authority file shape."""
        rust_text = _records_to_jsonl(records)
        source_text = _records_to_jsonl(records)
        source_hash = sha256(source_text.encode("utf-8")).hexdigest()
        rust_hash = sha256(rust_text.encode("utf-8")).hexdigest()
        if self._rust_authority_matches(
            rust_text=rust_text,
            record_count=len(records),
            source_hash=source_hash,
            rust_hash=rust_hash,
        ):
            return
        manifest = {
            "schema_version": 1,
            "authority": "amw-kernel::spine::SpineAuthority",
            "source_jsonl": _JSONL_FILENAME,
            "rust_jsonl": _RUST_AUTHORITY_LOG_FILENAME,
            "record_count": len(records),
            "source_sha256": source_hash,
            "rust_log_sha256": rust_hash,
            "generated_at_utc": datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        }
        self._atomic_write_text(self._rust_jsonl_path, rust_text)
        self._atomic_write_text(
            self._rust_manifest_path,
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        )

    def _rust_authority_matches(
        self,
        *,
        rust_text: str,
        record_count: int,
        source_hash: str,
        rust_hash: str,
    ) -> bool:
        if not self._rust_jsonl_path.exists() or not self._rust_manifest_path.exists():
            return False
        try:
            current_rust_text = self._rust_jsonl_path.read_text(encoding="utf-8")
            manifest = json.loads(self._rust_manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Rust spine manifest check failed; rebuild required: %s", exc)
            return False
        return (
            current_rust_text == rust_text
            and manifest.get("schema_version") == 1
            and manifest.get("authority") == "amw-kernel::spine::SpineAuthority"
            and manifest.get("source_jsonl") == _JSONL_FILENAME
            and manifest.get("rust_jsonl") == _RUST_AUTHORITY_LOG_FILENAME
            and manifest.get("record_count") == record_count
            and manifest.get("source_sha256") == source_hash
            and manifest.get("rust_log_sha256") == rust_hash
        )

    def _verify_rust_authority(self, records: list[dict[str, Any]]) -> None:
        if not self._rust_jsonl_path.exists() or not self._rust_manifest_path.exists():
            missing = self._rust_jsonl_path if not self._rust_jsonl_path.exists() else self._rust_manifest_path
            raise WorkbenchSpineCorrupt("Rust spine authority state missing", path=missing)
        try:
            manifest = json.loads(self._rust_manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise WorkbenchSpineCorrupt("Rust spine authority manifest corrupt", path=self._rust_manifest_path) from exc
        try:
            rust_text = self._rust_jsonl_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise WorkbenchSpineCorrupt("Rust spine authority log unreadable", path=self._rust_jsonl_path) from exc
        source_text = _records_to_jsonl(records)
        expected_source_hash = sha256(source_text.encode("utf-8")).hexdigest()
        expected_rust_hash = sha256(rust_text.encode("utf-8")).hexdigest()
        if manifest.get("authority") != "amw-kernel::spine::SpineAuthority":
            raise WorkbenchSpineCorrupt(
                "Rust spine authority manifest names unknown authority", path=self._rust_manifest_path
            )
        if manifest.get("record_count") != len(records):
            raise WorkbenchSpineCorrupt(
                "Rust spine authority manifest record count mismatch", path=self._rust_manifest_path
            )
        if manifest.get("source_sha256") != expected_source_hash:
            raise WorkbenchSpineCorrupt(
                "Rust spine authority manifest source hash mismatch", path=self._rust_manifest_path
            )
        if manifest.get("rust_log_sha256") != expected_rust_hash:
            raise WorkbenchSpineCorrupt(
                "Rust spine authority manifest log hash mismatch", path=self._rust_manifest_path
            )
        if rust_text != source_text:
            raise WorkbenchSpineCorrupt("Rust spine authority log diverged from append log", path=self._rust_jsonl_path)

    @staticmethod
    def _atomic_write_text(path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_name(f"{path.name}.tmp")
        try:
            with tmp_path.open("w", encoding="utf-8", newline="\n") as fh:
                fh.write(text)
                fh.flush()
                os.fsync(fh.fileno())
            tmp_path.replace(path)
        except OSError as exc:
            raise WorkbenchSpineCorrupt("Rust spine authority write failed", path=path) from exc


def _records_to_jsonl(records: list[dict[str, Any]]) -> str:
    return "".join(json.dumps(record, separators=(",", ":"), sort_keys=True) + "\n" for record in records)


_INSTANCE: WorkbenchSpine | None = None
_INSTANCE_LOCK: threading.Lock = threading.Lock()


def get_workbench_spine() -> WorkbenchSpine:
    """Return the process-wide WorkbenchSpine singleton.

    Returns:
        Resolved workbench spine value.
    """
    global _INSTANCE
    if _INSTANCE is None:
        with _INSTANCE_LOCK:
            if _INSTANCE is None:
                _INSTANCE = WorkbenchSpine()
    return _INSTANCE


def reset_workbench_spine_for_test() -> None:
    """Clear the process-wide spine singleton for isolated tests."""
    global _INSTANCE
    with _INSTANCE_LOCK:
        if _INSTANCE is not None:
            _INSTANCE.close()
        _INSTANCE = None


__all__ = [
    "WorkbenchSpine",
    "WorkbenchSpineCorrupt",
    "get_workbench_spine",
    "reset_workbench_spine_for_test",
]
