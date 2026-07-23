"""Typed dataset revision records and their local append-only store.

Step 1: typed dataset revision records consumed by ``DatasetRevisionStore``.
The local store mirrors DVC/lakeFS/Lance concepts without adding those
libraries as runtime dependencies.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
import uuid
from contextlib import suppress
from pathlib import Path
from typing import Any

from vetinari.constants import OUTPUTS_DIR
from vetinari.learning.atomic_writers import _write_text_atomic
from vetinari.receipts.store import WorkReceiptStore
from vetinari.workbench import dataset_revision_sqlite as _sqlite_helpers
from vetinari.workbench.assets import AssetTaint
from vetinari.workbench.data_assets import DataAsset
from vetinari.workbench.dataset_remotes import DatasetRemoteConfig, DatasetRemoteReceipt, remote_backend_for
from vetinari.workbench.dataset_revision_records import (
    _BRANCH_NAME_RE,
    _REVISION_ID_RE,
    DatasetBranch,
    DatasetDiff,
    DatasetRemoteKind,
    DatasetRevision,
    DatasetRevisionAuthFailed,
    DatasetRevisionError,
    DatasetRevisionRemoteUnavailable,
    DatasetRevisionSchemaMismatch,
    DatasetRevisionUnknown,
    DatasetTag,
    RevisionGateResult,
    RevisionStatus,
    _utc_now_iso,
    _validate_identifier,
)
from vetinari.workbench.dataset_revisions_helpers import _DatasetRevisionStoreHelperMixin
from vetinari.workbench.metadata_spine import WorkbenchSpine, get_workbench_spine

logger = logging.getLogger(__name__)


for _public_record in (DatasetBranch, DatasetDiff, DatasetRevision, DatasetTag, RevisionGateResult):
    _public_record.__module__ = __name__


_PROJECT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_DEFAULT_REVISIONS_DIR = OUTPUTS_DIR / "workbench" / "dataset_revisions"
_SCHEMA_VERSION = 1


# --- store implementation below ---


class DatasetRevisionStore(_DatasetRevisionStoreHelperMixin):
    """Append-only local dataset revision store with rebuildable SQLite index."""

    _schema_version = _SCHEMA_VERSION

    def __init__(
        self,
        base_dir: Path | str = _DEFAULT_REVISIONS_DIR,
        *,
        project_id: str = "default",
        remote_kind: DatasetRemoteKind = DatasetRemoteKind.LOCAL,
        spine: WorkbenchSpine | None = None,
        receipt_store: WorkReceiptStore | None = None,
    ) -> None:
        if not project_id or not _PROJECT_ID_RE.fullmatch(project_id):
            raise ValueError(
                f"DatasetRevisionStore.project_id {project_id!r} fails project-id regex (path traversal rejected)"
            )
        active_remote_kind = DatasetRemoteKind(remote_kind)
        if active_remote_kind is not DatasetRemoteKind.LOCAL:
            raise DatasetRevisionRemoteUnavailable(
                f"dataset revision remote kind {active_remote_kind.value!r} is not implemented for local store init"
            )
        root = Path(base_dir).expanduser().resolve()
        project_dir = (root / project_id).resolve()
        if not project_dir.is_relative_to(root):
            raise ValueError(f"resolved project path {project_dir} escapes revision root {root}")

        self._project_id = project_id
        self._project_dir = project_dir
        self._jsonl_path = project_dir / "revisions.jsonl"
        self._sqlite_path = project_dir / "revisions.sqlite"
        self._branches_path = project_dir / "branches.jsonl"
        self._tags_path = project_dir / "tags.jsonl"
        self._spine = spine if spine is not None else get_workbench_spine()
        self._receipt_store = receipt_store if receipt_store is not None else WorkReceiptStore()
        self._write_lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None
        self._revisions: dict[str, DatasetRevision] = {}
        self._revision_order: list[str] = []
        self._branches: dict[str, DatasetBranch] = {}
        self._tags: dict[str, DatasetTag] = {}

        try:
            project_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise DatasetRevisionError(
                f"DatasetRevisionStore project dir not creatable: {project_id!r} ({exc})"
            ) from exc
        if not project_dir.is_dir():
            raise DatasetRevisionError(f"DatasetRevisionStore project dir not creatable: {project_id!r}")

        if self._sqlite_path.exists() and not self._jsonl_path.exists():
            raise DatasetRevisionSchemaMismatch(
                "JSONL source-of-truth missing while SQLite index present; refusing to silently degrade"
            )
        self._ensure_jsonl_with_header(self._jsonl_path)
        self._ensure_jsonl_with_header(self._branches_path)
        self._ensure_jsonl_with_header(self._tags_path)
        self._load_all_jsonl()
        if self._sqlite_path.exists():
            _sqlite_helpers.check_sqlite_integrity(self)
        else:
            logger.warning("Rebuilding dataset revision SQLite index from %s", self._jsonl_path)
        self._conn = sqlite3.connect(self._sqlite_path, check_same_thread=False, isolation_level=None)
        self._configure_sqlite()
        _sqlite_helpers.create_schema(self)
        _sqlite_helpers.rebuild_sqlite(self)

    def commit(
        self,
        *,
        parent_revision_id: str | None,
        branch: str,
        assets: tuple[DataAsset, ...],
        message: str,
        source_receipt_ids: tuple[str, ...],
        reviewer_ids: tuple[str, ...] = (),
        taints: tuple[AssetTaint, ...] = (),
    ) -> DatasetRevision:
        """Append a new dataset revision and record it as a Workbench dataset asset.

        Returns:
            DatasetRevision value produced by commit().

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        _validate_identifier(branch, _BRANCH_NAME_RE, "branch")
        if parent_revision_id is not None:
            _validate_identifier(parent_revision_id, _REVISION_ID_RE, "parent_revision_id")

        with self._write_lock:
            # Keep WorkbenchSpine.append_asset and WorkReceiptStore SPINE_EVENT side effects inside this section.
            if parent_revision_id is not None and parent_revision_id not in self._revisions:
                raise DatasetRevisionUnknown(f"parent revision {parent_revision_id!r} not found")
            if parent_revision_id is not None and branch not in self._branches:
                raise DatasetRevisionUnknown(f"branch {branch!r} not found; call create_branch first")
            if parent_revision_id is not None and self._branches[branch].head_revision_id != parent_revision_id:
                raise DatasetRevisionError(
                    f"stale parent revision {parent_revision_id!r}; branch {branch!r} head is "
                    f"{self._branches[branch].head_revision_id!r}"
                )
            if parent_revision_id is None and branch in self._branches:
                raise DatasetRevisionError(f"branch {branch!r} already has a head")

            now = _utc_now_iso()
            status = RevisionStatus.REVIEWED if reviewer_ids else RevisionStatus.OPEN
            revision = DatasetRevision(
                revision_id=f"rev-{uuid.uuid4().hex[:16]}",
                parent_revision_id=parent_revision_id,
                branch=branch,
                status=status,
                assets=assets,
                created_at_utc=now,
                source_receipt_ids=source_receipt_ids,
                reviewer_ids=reviewer_ids,
                message=message,
                taints=taints,
            )
            existing_branch = self._branches.get(branch)
            branch_row = DatasetBranch(
                name=branch,
                head_revision_id=revision.revision_id,
                created_at_utc=existing_branch.created_at_utc if existing_branch is not None else now,
                created_by=existing_branch.created_by if existing_branch is not None else "dataset-revision-store",
            )
            snapshot = self._snapshot_locked()
            try:
                self._append_revision_locked(revision)
                self._upsert_branch_locked(branch_row)
                self._append_spine_asset_locked(revision)
                self._emit_receipt_locked("commit", revision.revision_id)
            except Exception:
                self._restore_snapshot_locked(snapshot)
                raise
            return revision

    def create_branch(self, *, name: str, from_revision_id: str, created_by: str) -> DatasetBranch:
        """Create a new branch pointer from an existing revision.

        Returns:
            Newly constructed branch value.

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        with self._write_lock:
            if from_revision_id not in self._revisions:
                raise DatasetRevisionUnknown(f"revision {from_revision_id!r} not found")
            if name in self._branches:
                raise DatasetRevisionError(f"branch {name!r} already exists")
            branch = DatasetBranch(
                name=name,
                head_revision_id=from_revision_id,
                created_at_utc=_utc_now_iso(),
                created_by=created_by,
            )
            snapshot = self._snapshot_locked()
            try:
                self._append_branch_locked(branch)
                self._emit_receipt_locked("create_branch", name)
            except Exception:
                self._restore_snapshot_locked(snapshot)
                raise
            return branch

    def tag(self, *, name: str, revision_id: str, created_by: str, message: str = "") -> DatasetTag:
        """Create a stable tag pointer to an existing revision.

        Returns:
            DatasetTag value produced by tag().

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        with self._write_lock:
            if revision_id not in self._revisions:
                raise DatasetRevisionUnknown(f"revision {revision_id!r} not found")
            if name in self._tags:
                raise DatasetRevisionError(f"tag {name!r} already exists")
            tag = DatasetTag(
                name=name,
                revision_id=revision_id,
                created_at_utc=_utc_now_iso(),
                created_by=created_by,
                message=message,
            )
            snapshot = self._snapshot_locked()
            try:
                self._append_tag_locked(tag)
                self._emit_receipt_locked("tag", name)
            except Exception:
                self._restore_snapshot_locked(snapshot)
                raise
            return tag

    def rollback(self, *, branch: str, target_revision_id: str, actor_id: str) -> DatasetRevision:
        """Append a rollback revision that copies the target revision's assets.

        Returns:
            DatasetRevision value produced by rollback().

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        with self._write_lock:
            if branch not in self._branches:
                raise DatasetRevisionUnknown(f"branch {branch!r} not found")
            if target_revision_id not in self._revisions:
                raise DatasetRevisionUnknown(f"revision {target_revision_id!r} not found")
            target = self._revisions[target_revision_id]
            if target.branch != branch:
                raise DatasetRevisionUnknown(f"revision {target_revision_id!r} is not reachable from branch {branch!r}")
            current_head = self._branches[branch].head_revision_id
            revision = DatasetRevision(
                revision_id=f"rev-{uuid.uuid4().hex[:16]}",
                parent_revision_id=current_head,
                branch=branch,
                status=RevisionStatus.ROLLED_BACK,
                assets=target.assets,
                created_at_utc=_utc_now_iso(),
                source_receipt_ids=target.source_receipt_ids,
                reviewer_ids=(actor_id,),
                message=f"rollback to {target_revision_id} by {actor_id}",
                taints=target.taints,
            )
            existing_branch = self._branches[branch]
            branch_row = DatasetBranch(
                name=branch,
                head_revision_id=revision.revision_id,
                created_at_utc=existing_branch.created_at_utc,
                created_by=existing_branch.created_by,
            )
            snapshot = self._snapshot_locked()
            try:
                self._append_revision_locked(revision)
                self._upsert_branch_locked(branch_row)
                self._append_spine_asset_locked(revision)
                self._emit_receipt_locked("rollback", revision.revision_id)
            except Exception:
                self._restore_snapshot_locked(snapshot)
                raise
            return revision

    def list_branches(self) -> tuple[DatasetBranch, ...]:
        """Return branch pointers ordered by branch name."""
        return tuple(self._branches[name] for name in sorted(self._branches))

    def list_revisions(self, *, branch: str | None = None, limit: int | None = None) -> tuple[DatasetRevision, ...]:
        """Return revisions in append order, optionally filtered by branch.

        Returns:
            Collection of revisions values.
        """
        revisions = [self._revisions[revision_id] for revision_id in self._revision_order]
        if branch is not None:
            revisions = [revision for revision in revisions if revision.branch == branch]
        if limit is not None:
            revisions = revisions[:limit]
        return tuple(revisions)

    def discard_revision_for_failed_import(self, revision_id: str, *, expected_branch_prefix: str) -> bool:
        """Remove a just-created import revision after a later atomic import step fails.

        Returns:
            True when the revision existed and was removed; False when it was already absent.

        Raises:
            DatasetRevisionError: If the revision does not belong to the expected import branch.
        """
        with self._write_lock:
            revision = self._revisions.get(revision_id)
            if revision is None:
                return False
            if not revision.branch.startswith(expected_branch_prefix):
                raise DatasetRevisionError(
                    f"refusing to discard revision {revision_id!r} from non-import branch {revision.branch!r}"
                )
            snapshot = self._snapshot_locked()
            try:
                self._revisions.pop(revision_id, None)
                self._revision_order = [item for item in self._revision_order if item != revision_id]
                self._branches = {
                    name: branch for name, branch in self._branches.items() if branch.head_revision_id != revision_id
                }
                self._tags = {name: tag for name, tag in self._tags.items() if tag.revision_id != revision_id}
                self._rewrite_store_locked()
            except Exception:
                self._restore_snapshot_locked(snapshot)
                raise
            return True

    def _rewrite_store_locked(self) -> None:
        revision_lines = [json.dumps({"kind": "header", "schema_version": self._schema_version}, sort_keys=True)]
        revision_lines.extend(
            self._envelope("revision", self._revisions[revision_id]).rstrip("\n")
            for revision_id in self._revision_order
        )
        branch_lines = [json.dumps({"kind": "header", "schema_version": self._schema_version}, sort_keys=True)]
        branch_lines.extend(self._envelope("branch", branch).rstrip("\n") for branch in self._branches.values())
        tag_lines = [json.dumps({"kind": "header", "schema_version": self._schema_version}, sort_keys=True)]
        tag_lines.extend(self._envelope("tag", tag).rstrip("\n") for tag in self._tags.values())
        _write_text_atomic(self._jsonl_path, "\n".join(revision_lines) + "\n")
        _write_text_atomic(self._branches_path, "\n".join(branch_lines) + "\n")
        _write_text_atomic(self._tags_path, "\n".join(tag_lines) + "\n")
        _sqlite_helpers.rebuild_sqlite(self)

    def diff(self, parent_revision_id: str, child_revision_id: str) -> DatasetDiff:
        """Return added, removed, and changed assets between two revisions.

        Args:
            parent_revision_id: Parent revision id value consumed by diff().
            child_revision_id: Child revision id value consumed by diff().

        Returns:
            DatasetDiff value produced by diff().

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        if parent_revision_id not in self._revisions:
            raise DatasetRevisionUnknown(f"revision {parent_revision_id!r} not found")
        if child_revision_id not in self._revisions:
            raise DatasetRevisionUnknown(f"revision {child_revision_id!r} not found")
        parent_by_path = {asset.asset_path: asset for asset in self._revisions[parent_revision_id].assets}
        child_by_path = {asset.asset_path: asset for asset in self._revisions[child_revision_id].assets}
        added = tuple(child_by_path[path] for path in sorted(child_by_path.keys() - parent_by_path.keys()))
        removed = tuple(parent_by_path[path] for path in sorted(parent_by_path.keys() - child_by_path.keys()))
        changed = tuple(
            (parent_by_path[path], child_by_path[path])
            for path in sorted(parent_by_path.keys() & child_by_path.keys())
            if parent_by_path[path].content_sha256 != child_by_path[path].content_sha256
        )
        return DatasetDiff(parent_revision_id, child_revision_id, added, removed, changed)

    def gate_for_promotion(self, revision_id: str) -> RevisionGateResult:
        """Return a fail-closed promotion gate result for a revision.

        Returns:
            RevisionGateResult value produced by gate_for_promotion().
        """
        revision = self._revisions.get(revision_id)
        if revision is None:
            return RevisionGateResult(passed=False, reasons=("unknown_revision",))
        reasons: list[str] = []
        if revision.status not in {RevisionStatus.REVIEWED, RevisionStatus.PROMOTED}:
            reasons.append("unreviewed")
        if not revision.reviewer_ids:
            reasons.append("missing_reviewer")
        if not revision.source_receipt_ids:
            reasons.append("missing_source_receipt")
        if revision.taints:
            reasons.append("tainted")
        if reasons:
            return RevisionGateResult(passed=False, reasons=tuple(reasons))
        return RevisionGateResult(
            passed=True,
            reasons=(
                "reviewed",
                f"reviewers={len(revision.reviewer_ids)}",
                f"source_receipts={len(revision.source_receipt_ids)}",
            ),
        )

    def assert_training_run_carries_dataset_revision(
        self,
        run: Any,
        provenance: Any,
    ) -> None:
        """Raise when a training run lacks a non-empty dataset revision id.

        Args:
            run: Run value consumed by assert_training_run_carries_dataset_revision().
            provenance: Provenance value consumed by assert_training_run_carries_dataset_revision().

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        from vetinari.training.data_provenance import TrainingDataProvenance
        from vetinari.workbench.runs import RunKind, WorkbenchRun

        if not isinstance(run, WorkbenchRun) and type(run).__name__ != "WorkbenchRun":
            raise DatasetRevisionError(f"run must be a WorkbenchRun, got {type(run).__name__!r}")
        if not isinstance(provenance, TrainingDataProvenance) and type(provenance).__name__ != "TrainingDataProvenance":
            raise DatasetRevisionError(f"provenance must be TrainingDataProvenance, got {type(provenance).__name__!r}")
        if getattr(run.kind, "value", run.kind) == RunKind.TRAINING_RUN.value and (
            provenance.dataset_revision_id is None or not provenance.dataset_revision_id.strip()
        ):
            raise DatasetRevisionError(
                f"training run {run.run_id!r} requires a non-empty dataset_revision_id; "
                f"provenance has dataset_revision_id={provenance.dataset_revision_id!r}"
            )

    def push_remote(self, revision_id: str, config: DatasetRemoteConfig) -> DatasetRemoteReceipt:
        """Push a local revision through a configured dataset remote backend.

        Args:
            revision_id: Local revision id to push.
            config: Remote backend configuration.

        Returns:
            Receipt emitted by the configured remote backend.

        Raises:
            DatasetRevisionUnknown: If the local revision id is unknown.
            DatasetRevisionError: If the backend rejects the remote operation.
        """
        with self._write_lock:
            revision = self._revisions.get(revision_id)
            if revision is None:
                raise DatasetRevisionUnknown(f"revision {revision_id!r} not found")
            return remote_backend_for(config.kind, config).push(revision)

    def pull_remote(self, revision_id: str, config: DatasetRemoteConfig) -> tuple[dict[str, Any], DatasetRemoteReceipt]:
        """Pull a remote revision payload through a configured backend."""
        return remote_backend_for(config.kind, config).pull(revision_id)

    def sync_remote(self, revision_id: str, config: DatasetRemoteConfig) -> DatasetRemoteReceipt:
        """Push and verify-read one local revision through a remote backend.

        Args:
            revision_id: Local revision id to synchronize.
            config: Remote backend configuration.

        Returns:
            Receipt emitted by the configured remote backend.

        Raises:
            DatasetRevisionUnknown: If the local revision id is unknown.
            DatasetRevisionError: If the backend rejects the remote operation.
        """
        with self._write_lock:
            revision = self._revisions.get(revision_id)
            if revision is None:
                raise DatasetRevisionUnknown(f"revision {revision_id!r} not found")
            return remote_backend_for(config.kind, config).sync(revision)

    def close(self) -> None:
        """Close the SQLite index connection."""
        if self._conn is not None:
            self._conn.close()
            self._conn = None


_INSTANCE: DatasetRevisionStore | None = None
_INSTANCE_LOCK = threading.Lock()


def get_dataset_revision_store() -> DatasetRevisionStore:
    """Return the process-wide DatasetRevisionStore singleton.

    Returns:
        Resolved dataset revision store value.
    """
    global _INSTANCE
    if _INSTANCE is None:
        with _INSTANCE_LOCK:
            if _INSTANCE is None:
                _INSTANCE = DatasetRevisionStore()
    return _INSTANCE


def reset_dataset_revision_store_for_test() -> None:
    """Clear the process-wide DatasetRevisionStore singleton for isolated tests."""
    global _INSTANCE
    with _INSTANCE_LOCK:
        if _INSTANCE is not None:
            with suppress(Exception):
                _INSTANCE.close()
        _INSTANCE = None


__all__ = [
    "DatasetBranch",
    "DatasetDiff",
    "DatasetRemoteConfig",
    "DatasetRemoteKind",
    "DatasetRemoteReceipt",
    "DatasetRevision",
    "DatasetRevisionAuthFailed",
    "DatasetRevisionError",
    "DatasetRevisionRemoteUnavailable",
    "DatasetRevisionSchemaMismatch",
    "DatasetRevisionStore",
    "DatasetRevisionUnknown",
    "DatasetTag",
    "RevisionGateResult",
    "RevisionStatus",
    "get_dataset_revision_store",
    "reset_dataset_revision_store_for_test",
]
