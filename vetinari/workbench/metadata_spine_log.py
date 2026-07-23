"""JSONL and receipt helpers for the Workbench metadata spine."""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from vetinari.agents.contracts import OutcomeSignal, Provenance, ToolEvidence
from vetinari.receipts.record import WorkReceipt, WorkReceiptKind
from vetinari.types import AgentType, EvidenceBasis, ShardKind
from vetinari.workbench.metadata_spine_records import WorkbenchSpineCorrupt

logger = logging.getLogger(__name__)

_RECEIPT_ACTOR = AgentType.WORKBENCH
_SCHEMA_SOURCE = "vetinari.workbench.metadata_spine"


class _WorkbenchSpineLogMixin:
    """JSONL append-log, archive replay, rotation, and receipt behavior."""

    if TYPE_CHECKING:
        _jsonl_path: Any
        _project_id: Any
        _receipt_store: Any
        _spine_rotation_settings: Any
        _store_dir: Any

    def _append_jsonl_line(self, line: str) -> None:
        try:
            with self._jsonl_path.open("a", encoding="utf-8", newline="\n") as fh:
                fh.write(line)
                fh.flush()
                os.fsync(fh.fileno())
        except OSError as exc:
            raise WorkbenchSpineCorrupt("JSONL append failed", path=self._jsonl_path) from exc

    def _emit_receipt(self, record_kind: str, record_id: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        receipt = WorkReceipt(
            project_id=self._project_id,
            agent_id="workbench-metadata-spine",
            agent_type=_RECEIPT_ACTOR,
            kind=WorkReceiptKind.SPINE_EVENT,
            outcome=OutcomeSignal(
                passed=True,
                score=1.0,
                basis=EvidenceBasis.TOOL_EVIDENCE,
                tool_evidence=(
                    ToolEvidence(
                        tool_name="workbench_spine",
                        command=f"append {record_kind}",
                        exit_code=0,
                        stdout_snippet=f"record_id={record_id}",
                        passed=True,
                    ),
                ),
                provenance=Provenance(
                    source=_SCHEMA_SOURCE,
                    timestamp_utc=now,
                    tool_name="workbench_spine",
                ),
                kind=ShardKind.STANDARD,
            ),
            started_at_utc=now,
            finished_at_utc=now,
            inputs_summary=f"spine append: {record_kind}",
            outputs_summary=f"record_id={record_id}",
        )
        self._receipt_store.append(receipt)

    def _load_jsonl_records(
        self,
        *,
        include_archives: bool = False,
        skip_corrupt_lines: bool = False,
    ) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        paths = [*self._archive_paths(), self._jsonl_path] if include_archives else [self._jsonl_path]
        for source_path in paths:
            if not source_path.exists():
                continue
            records.extend(self._load_jsonl_records_from_path(source_path, skip_corrupt_lines=skip_corrupt_lines))
        return records

    @staticmethod
    def _load_jsonl_records_from_path(
        source_path: Path,
        *,
        skip_corrupt_lines: bool = False,
    ) -> list[dict[str, Any]]:
        try:
            raw = source_path.read_bytes()
        except OSError as exc:
            raise WorkbenchSpineCorrupt("JSONL unreadable", path=source_path) from exc
        if raw and not raw.endswith(b"\n"):
            if not skip_corrupt_lines:
                raise WorkbenchSpineCorrupt("JSONL truncated; last line incomplete", path=source_path)
            logger.warning("Skipping truncated JSONL tail in %s during repair replay", source_path)
            last_newline = raw.rfind(b"\n")
            raw = raw[: last_newline + 1] if last_newline >= 0 else b""
        records: list[dict[str, Any]] = []
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            if not skip_corrupt_lines:
                raise WorkbenchSpineCorrupt("JSONL decode failed", path=source_path) from exc
            logger.warning("Decoding JSONL with replacement characters during repair replay: %s", source_path)
            text = raw.decode("utf-8", errors="replace")
        for lineno, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                if skip_corrupt_lines:
                    logger.warning("Skipping corrupt JSONL line %s in %s", lineno, source_path)
                    continue
                raise WorkbenchSpineCorrupt(
                    f"JSONL parse failed at line {lineno}",
                    path=source_path,
                ) from exc
            if not {"kind", "record_id", "payload"}.issubset(row):
                if skip_corrupt_lines:
                    logger.warning("Skipping incomplete JSONL line %s in %s", lineno, source_path)
                    continue
                raise WorkbenchSpineCorrupt(
                    f"JSONL missing required keys at line {lineno}",
                    path=source_path,
                )
            records.append(row)
        return records

    def _rotate_jsonl_if_needed(self, incoming_bytes: int) -> None:
        if not self._jsonl_path.exists():
            return
        try:
            raw = self._jsonl_path.read_bytes()
        except OSError as exc:
            raise WorkbenchSpineCorrupt("JSONL unreadable", path=self._jsonl_path) from exc
        if raw and not raw.endswith(b"\n"):
            raise WorkbenchSpineCorrupt("JSONL truncated; last line incomplete", path=self._jsonl_path)
        rotation = self._spine_rotation_settings()
        current_lines = len(raw.splitlines())
        if self._jsonl_path.stat().st_size + incoming_bytes <= rotation.max_bytes and (
            current_lines + 1 <= rotation.max_lines
        ):
            return
        archive_path = self._next_archive_path()
        try:
            self._atomic_rotate_jsonl(archive_path)
        except OSError as exc:
            raise WorkbenchSpineCorrupt("JSONL rotation failed", path=self._jsonl_path) from exc

    def _atomic_rotate_jsonl(self, archive_path: Path) -> None:
        self._store_dir.mkdir(parents=True, exist_ok=True)
        replacement_path = self._store_dir / f"{self._jsonl_path.name}.next"
        with replacement_path.open("w", encoding="utf-8", newline="\n") as handle:
            handle.flush()
            os.fsync(handle.fileno())
        self._jsonl_path.replace(archive_path)
        try:
            replacement_path.replace(self._jsonl_path)
        except OSError:
            archive_path.replace(self._jsonl_path)
            raise

    def _archive_paths(self) -> tuple[Path, ...]:
        if not self._store_dir.exists():
            return ()
        return tuple(
            sorted(
                (
                    path
                    for path in self._store_dir.glob(f"{self._jsonl_path.stem}.*{self._jsonl_path.suffix}")
                    if path.name not in {self._jsonl_path.name, "spine.rust.jsonl"}
                ),
                key=lambda path: (path.stat().st_mtime_ns, path.name),
            )
        )

    def _next_archive_path(self) -> Path:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        archive_path = self._store_dir / f"{self._jsonl_path.stem}.{stamp}{self._jsonl_path.suffix}"
        if not archive_path.exists():
            return archive_path
        for suffix in range(1, 1000):
            candidate = self._store_dir / f"{self._jsonl_path.stem}.{stamp}-{suffix}{self._jsonl_path.suffix}"
            if not candidate.exists():
                return candidate
        raise WorkbenchSpineCorrupt("JSONL archive path allocation failed", path=self._jsonl_path)
