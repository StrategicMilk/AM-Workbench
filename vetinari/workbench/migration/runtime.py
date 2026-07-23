"""Fail-closed safe-import runtime for Workbench onboarding migration."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import shutil
import threading
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from vetinari.learning.atomic_writers import write_json_atomic
from vetinari.workbench.migration.contracts import (
    MigrationApplyRequest,
    MigrationApplyResult,
    MigrationApplyStatus,
    MigrationBlockReason,
    MigrationConflict,
    MigrationFinding,
    MigrationPlan,
    MigrationRisk,
    MigrationSourceSpec,
    migration_json_safe,
)
from vetinari.workbench.migration.sources import (
    WorkbenchMigrationError,
    default_migration_config_path,
    default_migration_state_dir,
    is_secret_like_migration_source,
    load_migration_source_specs,
    migration_ledger_filename,
    migration_utc_now_iso,
    redact_migration_preview,
    safe_migration_destination_fragment,
    safe_migration_rel_path,
    stable_migration_id,
)

logger = logging.getLogger(__name__)


class WorkbenchMigrationService:
    """Detect, plan, and apply safe Workbench imports through one write boundary."""

    def __init__(
        self,
        *,
        source_root: Path | str = ".",
        state_dir: Path | str | None = None,
        config_path: Path | str | None = None,
    ) -> None:
        self.source_root = Path(source_root)
        self.state_dir = Path(state_dir) if state_dir is not None else default_migration_state_dir()
        self.config_path = Path(config_path) if config_path is not None else default_migration_config_path()
        self._lock = threading.RLock()

    def detect(self) -> tuple[MigrationFinding, ...]:
        """Return safe-to-show findings without mutating state.

        Returns:
            tuple[MigrationFinding, ...] value produced by detect().

        """
        root = self.source_root.resolve()
        findings: list[MigrationFinding] = []
        specs = list(load_migration_source_specs(self.config_path))
        for spec in specs:
            findings.extend(
                self._finding_for_path(spec, candidate, root)
                for candidate in self._iter_existing_candidates(spec, root)
            )
        return tuple(sorted(findings, key=lambda item: (item.kind.value, item.path, item.item_id)))

    def plan(self, *, dry_run: bool = True) -> MigrationPlan:
        """Build a dry-run proposal. This method never writes to disk.

        Returns:
            MigrationPlan value produced by plan().
        """
        findings = self.detect()
        conflict_map: dict[str, list[MigrationFinding]] = {}
        blocked: list[MigrationBlockReason] = []
        for item in findings:
            if item.blocked_reason is not None:
                blocked.append(item.blocked_reason)
            if item.conflict_key is not None:
                conflict_map.setdefault(item.conflict_key, []).append(item)
        conflicts = tuple(
            MigrationConflict(
                conflict_key=key,
                destination_path=items[0].destination_path,
                candidate_item_ids=tuple(item.item_id for item in items),
                reason="destination already exists and must be explicitly selected",
            )
            for key, items in sorted(conflict_map.items())
        )
        proposal_id = stable_migration_id(
            "migration-plan",
            tuple((item.item_id, item.risk.value, item.default_selected, item.conflict_key) for item in findings),
        )
        return MigrationPlan(
            proposal_id=proposal_id,
            dry_run=dry_run,
            findings=findings,
            conflicts=conflicts,
            blocked_reasons=tuple(dict.fromkeys(blocked)),
            writes_planned=not dry_run and any(item.default_selected for item in findings),
            generated_at_utc=migration_utc_now_iso(),
        )

    def apply(self, request: MigrationApplyRequest, *, backup_confirmed: bool = False) -> MigrationApplyResult:
        """Apply selected imports with backup, report, and idempotency guard.

        Args:
            request: Migration apply request describing the proposal and selected imports.
            backup_confirmed: Explicit operator confirmation that a backup is required
                before destructive migration writes are allowed.

        Returns:
            MigrationApplyResult value produced by apply().

        Raises:
            WorkbenchMigrationError: When staging, commit, or report persistence
                fails after a backup-confirmed apply request starts.
        """
        with self._lock:
            if not backup_confirmed:
                return self._backup_confirmation_required(request)
            prior_proposal = self._find_prior_proposal(request.proposal_id)
            if prior_proposal is not None and not request.selected_item_ids:
                return self._idempotent_result(request.proposal_id, prior_proposal)
            plan = self.plan(dry_run=True)
            selected_ids = self._selected_item_ids(plan, request)
            idempotency_key = self._apply_idempotency_key(request, selected_ids)
            prior = self._find_prior_run(idempotency_key)
            if prior is not None:
                return self._idempotent_result(request.proposal_id, prior, idempotency_key=idempotency_key)

            blockers = self._validate_apply(plan, request, selected_ids)
            if blockers:
                return self._blocked_apply_result(request, blockers, idempotency_key)

            selected = [item for item in plan.findings if item.item_id in selected_ids]
            return self._apply_selected_items(plan, request, selected, idempotency_key)

    @staticmethod
    def _backup_confirmation_required(request: MigrationApplyRequest) -> MigrationApplyResult:
        return MigrationApplyResult(
            status=MigrationApplyStatus.BLOCKED,
            proposal_id=request.proposal_id,
            applied_item_ids=(),
            blocked_reasons=(MigrationBlockReason.BACKUP_CONFIRMATION_REQUIRED,),
            backup_path=None,
            report_path=None,
            idempotency_key=stable_migration_id(request.proposal_id, "backup-not-confirmed"),
        )

    @staticmethod
    def _idempotent_result(
        proposal_id: str,
        prior: Mapping[str, Any],
        *,
        idempotency_key: str | None = None,
    ) -> MigrationApplyResult:
        return MigrationApplyResult(
            status=MigrationApplyStatus.IDEMPOTENT,
            proposal_id=proposal_id,
            applied_item_ids=tuple(prior.get("applied_item_ids", ())),
            blocked_reasons=(MigrationBlockReason.ALREADY_APPLIED,),
            backup_path=prior.get("backup_path"),
            report_path=prior.get("report_path"),
            idempotency_key=idempotency_key or str(prior.get("idempotency_key", "")),
        )

    @staticmethod
    def _selected_item_ids(plan: MigrationPlan, request: MigrationApplyRequest) -> set[str]:
        return set(request.selected_item_ids) or {item.item_id for item in plan.findings if item.default_selected}

    @staticmethod
    def _apply_idempotency_key(request: MigrationApplyRequest, selected_ids: set[str]) -> str:
        return stable_migration_id(
            request.proposal_id,
            tuple(sorted(selected_ids)),
            tuple(sorted((request.conflict_selections or {}).items())),
            tuple(sorted(request.include_secret_item_ids)),
        )

    @staticmethod
    def _blocked_apply_result(
        request: MigrationApplyRequest,
        blockers: list[MigrationBlockReason],
        idempotency_key: str,
    ) -> MigrationApplyResult:
        return MigrationApplyResult(
            status=MigrationApplyStatus.BLOCKED,
            proposal_id=request.proposal_id,
            applied_item_ids=(),
            blocked_reasons=tuple(dict.fromkeys(blockers)),
            backup_path=None,
            report_path=None,
            idempotency_key=idempotency_key,
        )

    def _apply_selected_items(
        self,
        plan: MigrationPlan,
        request: MigrationApplyRequest,
        selected: list[MigrationFinding],
        idempotency_key: str,
    ) -> MigrationApplyResult:
        stage_dir = self._stage_dir(idempotency_key)
        try:
            self._cleanup_stage(stage_dir)
            backup_path = self._write_backup(selected, idempotency_key)
            staged_items = self._stage_items(selected, stage_dir)
            self._append_staged_ledger(request, selected, staged_items, backup_path, idempotency_key)
            applied_ids = self._promote_staged_items(staged_items)
            report_path = self._write_report(plan, selected, applied_ids, idempotency_key, backup_path)
            self._append_committed_ledger(request, applied_ids, backup_path, report_path, idempotency_key)
        except OSError as exc:
            self._cleanup_stage(stage_dir)
            raise WorkbenchMigrationError(f"migration apply failed before trusted commit: {exc}") from exc
        except WorkbenchMigrationError:
            self._cleanup_stage(stage_dir)
            raise
        else:
            self._cleanup_stage(stage_dir)
        return MigrationApplyResult(
            status=MigrationApplyStatus.APPLIED,
            proposal_id=request.proposal_id,
            applied_item_ids=applied_ids,
            blocked_reasons=(),
            backup_path=str(backup_path),
            report_path=str(report_path),
            idempotency_key=idempotency_key,
        )

    def _append_staged_ledger(
        self,
        request: MigrationApplyRequest,
        selected: list[MigrationFinding],
        staged_items: tuple[dict[str, str], ...],
        backup_path: Path,
        idempotency_key: str,
    ) -> None:
        self._append_ledger({
            "commit_phase": "staged",
            "idempotency_key": idempotency_key,
            "proposal_id": request.proposal_id,
            "selected_item_ids": tuple(item.item_id for item in selected),
            "staged_items": staged_items,
            "backup_path": str(backup_path),
            "report_path": None,
            "recorded_at_utc": migration_utc_now_iso(),
        })

    def _append_committed_ledger(
        self,
        request: MigrationApplyRequest,
        applied_ids: tuple[str, ...],
        backup_path: Path,
        report_path: Path,
        idempotency_key: str,
    ) -> None:
        self._append_ledger({
            "commit_phase": "committed",
            "idempotency_key": idempotency_key,
            "proposal_id": request.proposal_id,
            "applied_item_ids": applied_ids,
            "backup_path": str(backup_path),
            "report_path": str(report_path),
            "recorded_at_utc": migration_utc_now_iso(),
        })

    @staticmethod
    def _iter_existing_candidates(spec: MigrationSourceSpec, root: Path) -> Iterable[Path]:
        for raw in spec.paths:
            candidate = (root / raw).resolve()
            if not candidate.exists():
                continue
            if candidate.is_dir():
                files = [path for path in candidate.rglob("*") if path.is_file()]
                if not files:
                    yield candidate
                else:
                    yield from files
            elif candidate.is_file():
                yield candidate

    def _finding_for_path(self, spec: MigrationSourceSpec, path: Path, root: Path) -> MigrationFinding:
        rel_path = safe_migration_rel_path(path, root)
        text = self._read_preview(path)
        secret_like = is_secret_like_migration_source(path, text)
        destination = self._destination_for(spec, rel_path)
        conflict = destination.exists()
        if secret_like:
            risk = MigrationRisk.SENSITIVE_CREDENTIAL
            blocked_reason = MigrationBlockReason.CREDENTIAL_SELECTION_REQUIRED
            default_selected = False
            preview = "<redacted secret-like content>"
        elif spec.risky_tool:
            risk = MigrationRisk.RISKY_TOOL
            blocked_reason = MigrationBlockReason.RISKY_TOOL_EXPLICIT_SELECTION_REQUIRED
            default_selected = False
            preview = redact_migration_preview(text)
        elif conflict:
            risk = MigrationRisk.CONFLICT
            blocked_reason = MigrationBlockReason.CONFLICT_SELECTION_REQUIRED
            default_selected = False
            preview = redact_migration_preview(text)
        else:
            risk = MigrationRisk.LOW
            blocked_reason = None
            default_selected = True
            preview = redact_migration_preview(text)
        item_id = stable_migration_id(spec.source_id, spec.kind.value, rel_path)
        conflict_key = f"{spec.kind.value}:{safe_migration_destination_fragment(rel_path)}" if conflict else None
        return MigrationFinding(
            item_id=item_id,
            source_id=spec.source_id,
            label=spec.label,
            kind=spec.kind,
            path=rel_path,
            destination_path=str(destination),
            risk=risk,
            default_selected=default_selected,
            blocked_reason=blocked_reason,
            conflict_key=conflict_key,
            redacted_preview=preview,
        )

    @staticmethod
    def _read_preview(path: Path) -> str:
        if path.is_dir():
            return f"directory: {path.name}"
        try:
            return path.read_text(encoding="utf-8", errors="replace")[:4000]
        except OSError as exc:
            raise WorkbenchMigrationError(f"source file is unreadable: {path}: {exc}") from exc

    def _destination_for(self, spec: MigrationSourceSpec, rel_path: str) -> Path:
        return self.state_dir / "imports" / spec.kind.value / safe_migration_destination_fragment(rel_path)

    @staticmethod
    def _validate_apply(
        plan: MigrationPlan,
        request: MigrationApplyRequest,
        selected_ids: set[str],
    ) -> list[MigrationBlockReason]:
        blockers: list[MigrationBlockReason] = []
        if request.proposal_id != plan.proposal_id:
            blockers.append(MigrationBlockReason.STALE_PLAN)
        by_id = {item.item_id: item for item in plan.findings}
        for item_id in selected_ids:
            if item_id not in by_id:
                blockers.append(MigrationBlockReason.UNKNOWN_ITEM)
                continue
            item = by_id[item_id]
            if item.risk is MigrationRisk.SENSITIVE_CREDENTIAL and item_id not in set(request.include_secret_item_ids):
                blockers.append(MigrationBlockReason.CREDENTIAL_SELECTION_REQUIRED)
            if item.risk is MigrationRisk.RISKY_TOOL and item_id not in set(request.selected_item_ids):
                blockers.append(MigrationBlockReason.RISKY_TOOL_EXPLICIT_SELECTION_REQUIRED)
            if item.conflict_key is not None and (request.conflict_selections or {}).get(item.conflict_key) != item_id:
                blockers.append(MigrationBlockReason.CONFLICT_SELECTION_REQUIRED)
        return blockers

    def _write_backup(self, selected: list[MigrationFinding], idempotency_key: str) -> Path:
        backup_dir = self.state_dir / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup_path = backup_dir / f"{idempotency_key}.json"
        payload = {
            "kind": "workbench_migration_backup",
            "created_at_utc": migration_utc_now_iso(),
            "selected_item_ids": [item.item_id for item in selected],
            "existing_destinations": [
                {
                    "item_id": item.item_id,
                    "destination_path": item.destination_path,
                    "existed": Path(item.destination_path).exists(),
                    "redacted_preview": redact_migration_preview(
                        Path(item.destination_path).read_text(encoding="utf-8", errors="replace")
                    )
                    if Path(item.destination_path).is_file()
                    else "",
                }
                for item in selected
            ],
        }
        write_json_atomic(backup_path, payload)
        return backup_path

    def _stage_dir(self, idempotency_key: str) -> Path:
        return self.state_dir / "staging" / safe_migration_destination_fragment(idempotency_key)

    def _stage_items(self, selected: list[MigrationFinding], stage_dir: Path) -> tuple[dict[str, str], ...]:
        return tuple(self._stage_item(item, stage_dir) for item in selected)

    def _stage_item(self, item: MigrationFinding, stage_dir: Path) -> dict[str, str]:
        source = self.source_root / item.path
        staged_path = stage_dir / item.kind.value / safe_migration_destination_fragment(item.path)
        staged_path.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            write_json_atomic(staged_path, {"directory": item.path, "imported_at_utc": migration_utc_now_iso()})
        else:
            shutil.copyfile(source, staged_path)
        return {"item_id": item.item_id, "staged_path": str(staged_path), "destination_path": item.destination_path}

    @staticmethod
    def _promote_staged_items(staged_items: tuple[dict[str, str], ...]) -> tuple[str, ...]:
        applied_ids: list[str] = []
        completed: list[tuple[Path, Path | None]] = []
        try:
            for row in staged_items:
                staged_path = Path(row["staged_path"])
                destination = Path(row["destination_path"])
                destination.parent.mkdir(parents=True, exist_ok=True)
                rollback_path: Path | None = None
                if destination.exists():
                    rollback_path = staged_path.with_name(f".rollback-{row['item_id']}-{destination.name}")
                    os.replace(destination, rollback_path)
                try:
                    os.replace(staged_path, destination)
                except OSError:
                    if rollback_path is not None and rollback_path.exists():
                        os.replace(rollback_path, destination)
                    raise
                completed.append((destination, rollback_path))
                applied_ids.append(row["item_id"])
        except OSError:
            for destination, rollback_path in reversed(completed):
                with contextlib.suppress(OSError):
                    if destination.is_dir():
                        shutil.rmtree(destination)
                    elif destination.exists():
                        destination.unlink()
                if rollback_path is not None and rollback_path.exists():
                    os.replace(rollback_path, destination)
            raise
        return tuple(applied_ids)

    @staticmethod
    def _cleanup_stage(stage_dir: Path) -> None:
        if not stage_dir.exists():
            return
        shutil.rmtree(stage_dir)

    def _write_report(
        self,
        plan: MigrationPlan,
        selected: list[MigrationFinding],
        applied_ids: tuple[str, ...],
        idempotency_key: str,
        backup_path: Path,
    ) -> Path:
        report_dir = self.state_dir / "reports"
        report_dir.mkdir(parents=True, exist_ok=True)
        report_path = report_dir / f"{idempotency_key}.json"
        payload = {
            "kind": "workbench_migration_report",
            "safe_to_show": True,
            "proposal_id": plan.proposal_id,
            "applied_item_ids": applied_ids,
            "backup_path": str(backup_path),
            "selected": [migration_json_safe(item) for item in selected],
            "conflicts": migration_json_safe(plan.conflicts),
            "created_at_utc": migration_utc_now_iso(),
        }
        write_json_atomic(report_path, payload)
        # spine_consumers invokes get_spine() and absorbs observability failures.
        from vetinari.workbench.spine_consumers import record_run_completed

        record_run_completed(
            run_id=f"migration-{plan.proposal_id}",
            kind="agent_run",
            project_id="default",
        )
        return report_path

    def _ledger_path(self) -> Path:
        return self.state_dir / migration_ledger_filename()

    def _find_prior_run(self, idempotency_key: str) -> dict[str, Any] | None:
        return self._find_prior_row("idempotency_key", idempotency_key)

    def _find_prior_proposal(self, proposal_id: str) -> dict[str, Any] | None:
        return self._find_prior_row("proposal_id", proposal_id)

    def _find_prior_row(self, key: str, value: str) -> dict[str, Any] | None:
        path = self._ledger_path()
        if not path.exists():
            return None
        try:
            for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise WorkbenchMigrationError(f"migration ledger corrupt at line {line_no}: {exc}") from exc
                if row.get("commit_phase", "committed") != "committed":
                    continue
                if row.get(key) == value:
                    return row
        except OSError as exc:
            raise WorkbenchMigrationError(f"migration ledger is unreadable: {exc}") from exc
        return None

    def _append_ledger(self, payload: Mapping[str, Any]) -> None:
        # Use O_APPEND open so the OS-level append is atomic and no read is
        # needed.  This avoids the read-modify-write window that could allow a
        # concurrent process to lose a write even though self._lock guards
        # in-process serialisation.
        path = self._ledger_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(dict(payload), sort_keys=True) + "\n"
        with path.open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(line)
            fh.flush()
            os.fsync(fh.fileno())


def get_workbench_migration_service() -> WorkbenchMigrationService:
    return WorkbenchMigrationService()


__all__ = [
    "WorkbenchMigrationError",
    "WorkbenchMigrationService",
    "get_workbench_migration_service",
    "load_migration_source_specs",
]
