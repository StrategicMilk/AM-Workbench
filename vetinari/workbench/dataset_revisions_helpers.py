"""Private helper mixin for the dataset revision store."""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from vetinari.agents.contracts import OutcomeSignal, Provenance, ToolEvidence
from vetinari.learning.atomic_writers import _write_text_atomic
from vetinari.receipts.record import WorkReceipt, WorkReceiptKind
from vetinari.types import AgentType, EvidenceBasis, ShardKind
from vetinari.workbench import dataset_revision_sqlite as _sqlite_helpers
from vetinari.workbench.assets import AssetKind, WorkbenchAsset
from vetinari.workbench.dataset_revision_records import (
    DatasetBranch,
    DatasetRevision,
    DatasetRevisionError,
    DatasetRevisionSchemaMismatch,
    DatasetRevisionUnknown,
    DatasetTag,
    _branch_from_payload,
    _revision_from_payload,
    _tag_from_payload,
    _to_jsonable,
    _utc_now_iso,
)


@dataclass(frozen=True, slots=True)
class _DatasetRevisionStoreSnapshot:
    revisions: dict[str, DatasetRevision]
    revision_order: tuple[str, ...]
    branches: dict[str, DatasetBranch]
    tags: dict[str, DatasetTag]
    revisions_text: str
    branches_text: str
    tags_text: str

    def __repr__(self) -> str:
        """Return compact rollback state for diagnostics."""
        return (
            "_DatasetRevisionStoreSnapshot("
            f"revisions={len(self.revisions)!r}, branches={len(self.branches)!r}, "
            f"tags={len(self.tags)!r}, revision_order={len(self.revision_order)!r})"
        )


class _DatasetRevisionStoreHelperMixin:
    """Private persistence and side-effect helpers for DatasetRevisionStore."""

    if TYPE_CHECKING:
        _branches: Any
        _branches_path: Any
        _jsonl_path: Any
        _project_id: Any
        _receipt_store: Any
        _revision_order: Any
        _revisions: Any
        _spine: Any
        _tags: Any
        _tags_path: Any

    _schema_version: int

    def _ensure_jsonl_with_header(self, path: Path) -> None:
        if path.exists():
            return
        header = json.dumps({"kind": "header", "schema_version": self._schema_version}, sort_keys=True)
        self._append_line(path, header + "\n")

    def _configure_sqlite(self) -> None:
        try:
            conn = _sqlite_helpers.require_conn(self)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
        except sqlite3.DatabaseError as exc:
            raise DatasetRevisionSchemaMismatch("SQLite concurrency configuration failed") from exc

    def _load_all_jsonl(self) -> None:
        revision_rows = self._load_rows(self._jsonl_path, expected_kind="revision")
        branch_rows = self._load_rows(self._branches_path, expected_kind="branch")
        tag_rows = self._load_rows(self._tags_path, expected_kind="tag")
        for row in revision_rows:
            revision = _revision_from_payload(row["payload"])
            self._revisions[revision.revision_id] = revision
            self._revision_order.append(revision.revision_id)
        for row in branch_rows:
            branch = _branch_from_payload(row["payload"])
            if branch.head_revision_id not in self._revisions:
                raise DatasetRevisionUnknown(
                    f"branch {branch.name!r} points at absent revision {branch.head_revision_id!r}"
                )
            self._branches[branch.name] = branch
        for row in tag_rows:
            tag = _tag_from_payload(row["payload"])
            if tag.revision_id not in self._revisions:
                raise DatasetRevisionUnknown(f"tag {tag.name!r} points at absent revision {tag.revision_id!r}")
            self._tags[tag.name] = tag

    def _load_rows(self, path: Path, *, expected_kind: str) -> list[dict[str, Any]]:
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise DatasetRevisionError(f"{path.name} unreadable") from exc
        if raw and not raw.endswith(b"\n"):
            raise DatasetRevisionSchemaMismatch(f"{path.name} truncated; last line incomplete")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise DatasetRevisionSchemaMismatch(f"{path.name} decode failed") from exc
        rows: list[dict[str, Any]] = []
        saw_header = False
        for lineno, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise DatasetRevisionSchemaMismatch(f"{path.name} parse failed at line {lineno}") from exc
            if row.get("kind") == "header":
                saw_header = True
                if row.get("schema_version") != self._schema_version:
                    raise DatasetRevisionSchemaMismatch(
                        f"{path.name} schema version {row.get('schema_version')!r} "
                        f"does not match {self._schema_version!r}"
                    )
                continue
            if row.get("schema_version") != self._schema_version or row.get("kind") != expected_kind:
                raise DatasetRevisionSchemaMismatch(f"{path.name} invalid row at line {lineno}")
            if "payload" not in row:
                raise DatasetRevisionSchemaMismatch(f"{path.name} missing payload at line {lineno}")
            rows.append(row)
        if not saw_header:
            raise DatasetRevisionSchemaMismatch(f"{path.name} missing schema header")
        return rows

    def _snapshot_locked(self) -> _DatasetRevisionStoreSnapshot:
        return _DatasetRevisionStoreSnapshot(
            revisions=dict(self._revisions),
            revision_order=tuple(self._revision_order),
            branches=dict(self._branches),
            tags=dict(self._tags),
            revisions_text=self._jsonl_path.read_text(encoding="utf-8"),
            branches_text=self._branches_path.read_text(encoding="utf-8"),
            tags_text=self._tags_path.read_text(encoding="utf-8"),
        )

    def _restore_snapshot_locked(self, snapshot: _DatasetRevisionStoreSnapshot) -> None:
        self._revisions = dict(snapshot.revisions)
        self._revision_order = list(snapshot.revision_order)
        self._branches = dict(snapshot.branches)
        self._tags = dict(snapshot.tags)
        _write_text_atomic(self._jsonl_path, snapshot.revisions_text)
        _write_text_atomic(self._branches_path, snapshot.branches_text)
        _write_text_atomic(self._tags_path, snapshot.tags_text)
        _sqlite_helpers.rebuild_sqlite(self)

    def _append_revision_locked(self, revision: DatasetRevision) -> None:
        line = self._envelope("revision", revision)
        self._append_line(self._jsonl_path, line)
        self._revisions[revision.revision_id] = revision
        self._revision_order.append(revision.revision_id)
        _sqlite_helpers.insert_revision_sqlite(self, revision)

    def _append_branch_locked(self, branch: DatasetBranch) -> None:
        self._append_line(self._branches_path, self._envelope("branch", branch))
        self._branches[branch.name] = branch
        _sqlite_helpers.upsert_branch_sqlite(self, branch)

    def _upsert_branch_locked(self, branch: DatasetBranch) -> None:
        self._append_line(self._branches_path, self._envelope("branch", branch))
        self._branches[branch.name] = branch
        _sqlite_helpers.upsert_branch_sqlite(self, branch)

    def _append_tag_locked(self, tag: DatasetTag) -> None:
        self._append_line(self._tags_path, self._envelope("tag", tag))
        self._tags[tag.name] = tag
        _sqlite_helpers.insert_tag_sqlite(self, tag)

    @staticmethod
    def _append_line(path: Path, line: str) -> None:
        # Use O_APPEND open so the OS-level append is atomic and no read is
        # needed.  This avoids the read-modify-write window that would allow a
        # concurrent process to lose a write even when a threading.Lock guards
        # in-process serialisation.
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8", newline="\n") as fh:
                fh.write(line)
                fh.flush()
                os.fsync(fh.fileno())
        except OSError as exc:
            raise DatasetRevisionError(f"JSONL append failed for {path.name}") from exc

    def _append_spine_asset_locked(self, revision: DatasetRevision) -> None:
        self._spine.append_asset(
            WorkbenchAsset(
                asset_id=revision.revision_id,
                kind=AssetKind.DATASET,
                name=f"dataset/{revision.branch}/{revision.revision_id}",
                revision="1",
                created_at_utc=revision.created_at_utc,
                taints=revision.taints,
                provenance={"source": f"dataset-revision:{revision.revision_id}"},
            )
        )

    def _emit_receipt_locked(self, operation: str, record_id: str) -> None:
        now = _utc_now_iso()
        receipt = WorkReceipt(
            project_id=self._project_id,
            agent_id="workbench-dataset-revision-store",
            agent_type=AgentType.WORKBENCH,
            kind=WorkReceiptKind.SPINE_EVENT,
            outcome=OutcomeSignal(
                passed=True,
                score=1.0,
                basis=EvidenceBasis.TOOL_EVIDENCE,
                tool_evidence=(
                    ToolEvidence(
                        tool_name="dataset_revision_store",
                        command=f"dataset_revision_store.{operation}",
                        exit_code=0,
                        stdout_snippet=f"record_id={record_id}",
                        passed=True,
                    ),
                ),
                provenance=Provenance(
                    source="vetinari.workbench.dataset_revisions",
                    timestamp_utc=now,
                    tool_name="dataset_revision_store",
                ),
                kind=ShardKind.STANDARD,
            ),
            started_at_utc=now,
            finished_at_utc=now,
            inputs_summary=f"dataset revision {operation}",
            outputs_summary=f"record_id={record_id}",
        )
        self._receipt_store.append(receipt)

    def _envelope(self, kind: str, payload: Any) -> str:
        return (
            json.dumps(
                {
                    "schema_version": self._schema_version,
                    "kind": kind,
                    "payload": _to_jsonable(payload),
                },
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        )


__all__ = ["_DatasetRevisionStoreHelperMixin"]
