"""Proof-backed evidence notebooks derived from Workbench spine records.

Evidence notebooks are read-only investigation views. They do not persist a
notebook document; product-claim cells are assembled from existing evidence
asset cards, run traces, eval rows, source provenance, and sealed repro
capsules. Imports perform no I/O.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from enum import Enum
from typing import Any

from vetinari.security.redaction import redact_route_payload, redact_text
from vetinari.workbench.evals import EvalResult
from vetinari.workbench.evidence_assets import (
    EvidenceAssetCard,
    EvidenceAssetKind,
    EvidenceAssetLibrary,
    EvidenceAssetLibraryError,
    ProofStatus,
)
from vetinari.workbench.metadata_spine import WorkbenchSpine, WorkbenchSpineCorrupt, get_workbench_spine
from vetinari.workbench.repro_capsules import (
    CapsuleProofStatus,
    ReproCapsuleError,
    ReproCapsuleExport,
    ReproCapsuleService,
)
from vetinari.workbench.runs import WorkbenchRun
from vetinari.workbench.spine import WorkbenchProjectIdRejected, validate_project_id
from vetinari.workbench.traces import WorkbenchTrace

logger = logging.getLogger(__name__)


class EvidenceNotebookProjectIdRejected(ValueError):
    """Raised when a project id is not canonical."""

    def __init__(self, value: object) -> None:
        super().__init__(f"invalid project_id {value!r}; use [A-Za-z0-9_-] up to 64 characters")
        self.value = value


class EvidenceNotebookError(Exception):
    """Raised when a notebook cannot be safely returned."""

    def __init__(self, message: str, *, code: str = "proof_unavailable") -> None:
        super().__init__(message)
        self.code = code


class EvidenceNotebookCellKind(str, Enum):
    """Cell categories supported by evidence notebooks."""

    PRODUCT_CLAIM = "product_claim"
    CONTEXT_NOTE = "context_note"


class NotebookProofKind(str, Enum):
    """Evidence row kinds that can back a product-claim cell."""

    EVIDENCE_ASSET = "evidence_asset"
    RUN_TRACE = "run_trace"
    EVAL_RESULT = "eval_result"
    SOURCE_CARD = "source_card"
    PROOF_COMMAND = "proof_command"
    REPRO_CAPSULE = "repro_capsule"


class NotebookProofStatus(str, Enum):
    """Fail-closed proof status for notebook proof refs."""

    CURRENT = "current"
    UNVERIFIED = "unverified"
    MISSING = "missing"
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True)
class NotebookProofRef:
    """One proof row attached to a notebook cell."""

    kind: NotebookProofKind
    proof_id: str
    status: NotebookProofStatus
    label: str
    source: str
    reproduction_command: str = ""
    metadata: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        _require_non_empty(self.proof_id, "proof_id")
        _require_non_empty(self.label, "label")
        _require_non_empty(self.source, "source")
        if not isinstance(self.kind, NotebookProofKind):
            raise EvidenceNotebookError("proof kind must be NotebookProofKind")
        if not isinstance(self.status, NotebookProofStatus):
            raise EvidenceNotebookError("proof status must be NotebookProofStatus")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"NotebookProofRef(kind={self.kind!r}, proof_id={self.proof_id!r}, status={self.status!r})"

    def to_dict(self) -> dict[str, Any]:
        """Return the evidence-notebook API JSON contract for this proof ref."""
        return {
            "kind": self.kind.value,
            "proof_id": self.proof_id,
            "status": self.status.value,
            "label": self.label,
            "source": self.source,
            "reproduction_command": self.reproduction_command,
            "metadata": list(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class EvidenceNotebookCell:
    """One investigation cell with explicit proof requirements."""

    cell_id: str
    kind: EvidenceNotebookCellKind
    purpose: str
    title: str
    text: str
    is_product_claim: bool
    proof_refs: tuple[NotebookProofRef, ...] = ()

    def __post_init__(self) -> None:
        _require_non_empty(self.cell_id, "cell_id")
        _require_non_empty(self.purpose, "purpose")
        _require_non_empty(self.title, "title")
        _require_non_empty(self.text, "text")
        if not isinstance(self.kind, EvidenceNotebookCellKind):
            raise EvidenceNotebookError("cell kind must be EvidenceNotebookCellKind")
        if self.is_product_claim and self.kind is not EvidenceNotebookCellKind.PRODUCT_CLAIM:
            raise EvidenceNotebookError("product claims must use PRODUCT_CLAIM cells")
        if self.kind is EvidenceNotebookCellKind.PRODUCT_CLAIM:
            if not self.proof_refs:
                raise EvidenceNotebookError(f"product claim cell {self.cell_id!r} has no proof refs")
            non_current = [ref.proof_id for ref in self.proof_refs if ref.status is not NotebookProofStatus.CURRENT]
            if non_current:
                raise EvidenceNotebookError(
                    f"product claim cell {self.cell_id!r} has non-current proof refs: {', '.join(non_current)}"
                )
        elif self.is_product_claim:
            raise EvidenceNotebookError("static-note cells cannot carry product claims")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"EvidenceNotebookCell(cell_id={self.cell_id!r}, kind={self.kind!r}, purpose={self.purpose!r})"

    def to_dict(self) -> dict[str, Any]:
        """Return the evidence-notebook API JSON contract for this cell.

        Returns:
            Value produced for the caller.
        """
        commands = [ref.reproduction_command for ref in self.proof_refs if ref.reproduction_command]
        proof_status = (
            "current"
            if self.proof_refs and all(ref.status is NotebookProofStatus.CURRENT for ref in self.proof_refs)
            else "blocked"
        )
        return {
            "cell_id": self.cell_id,
            "kind": self.kind.value,
            "purpose": self.purpose,
            "title": self.title,
            "text": self.text,
            "is_product_claim": self.is_product_claim,
            "proof_refs": [ref.to_dict() for ref in self.proof_refs],
            "proof_status": proof_status,
            "rerunnable_commands": commands,
        }


@dataclass(frozen=True, slots=True)
class EvidenceNotebook:
    """A derived proof-backed investigation notebook."""

    notebook_id: str
    project_id: str
    title: str
    summary: str
    cells: tuple[EvidenceNotebookCell, ...]
    created_at_utc: str
    updated_at_utc: str

    def __post_init__(self) -> None:
        _require_non_empty(self.notebook_id, "notebook_id")
        _canonicalize_project_id(self.project_id)
        _require_non_empty(self.title, "title")
        _require_non_empty(self.summary, "summary")
        if not self.cells:
            raise EvidenceNotebookError("notebook must contain at least one cell")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return (
            f"EvidenceNotebook(notebook_id={self.notebook_id!r}, project_id={self.project_id!r}, title={self.title!r})"
        )

    def to_dict(self) -> dict[str, Any]:
        """Return the evidence-notebook API JSON contract for this notebook."""
        return {
            "notebook_id": self.notebook_id,
            "project_id": self.project_id,
            "title": self.title,
            "summary": self.summary,
            "created_at_utc": self.created_at_utc,
            "updated_at_utc": self.updated_at_utc,
            "cells": [cell.to_dict() for cell in self.cells],
        }


class EvidenceNotebookService:
    """Read-only notebook service over existing Workbench proof surfaces."""

    def __init__(
        self,
        *,
        spine: WorkbenchSpine | None = None,
        evidence_library: EvidenceAssetLibrary | None = None,
        repro_service: ReproCapsuleService | None = None,
    ) -> None:
        self._spine = spine
        self._evidence_library = evidence_library
        self._repro_service = repro_service
        self._read_lock = threading.RLock()

    def list_notebooks(self, *, project_id: str = "default") -> list[EvidenceNotebook]:
        """Return derived notebooks for one project.

        Returns:
            Collection of notebooks values.
        """
        canonical = _canonicalize_project_id(project_id)
        return [self.build_notebook(project_id=canonical, notebook_id="proof-ledger")]

    def get_notebook(self, *, project_id: str = "default", notebook_id: str) -> EvidenceNotebook | None:
        """Return one derived notebook by id.

        Returns:
            Resolved notebook value.
        """
        canonical = _canonicalize_project_id(project_id)
        if notebook_id != "proof-ledger":
            return None
        return self.build_notebook(project_id=canonical, notebook_id=notebook_id)

    def build_notebook(self, *, project_id: str = "default", notebook_id: str = "proof-ledger") -> EvidenceNotebook:
        """Assemble one fail-closed evidence notebook.

        Returns:
            Newly constructed notebook value.

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        canonical = _canonicalize_project_id(project_id)
        if notebook_id != "proof-ledger":
            raise EvidenceNotebookError(f"notebook {notebook_id!r} not found", code="not_found")
        with self._read_lock:
            try:
                spine = self._get_or_init_spine()
                cards = self._get_or_init_library(spine).list_cards(project_id=canonical)
                capsules = self._get_or_init_repro_service(spine).list_capsules(project_id=canonical)
            except EvidenceNotebookProjectIdRejected:
                raise
            except (EvidenceAssetLibraryError, ReproCapsuleError, WorkbenchSpineCorrupt) as exc:
                logger.info("Evidence notebook upstream unavailable: %s", exc)
                raise EvidenceNotebookError(
                    "workbench notebook proof upstream unavailable", code="upstream_unavailable"
                ) from exc
            cells = self._build_cells(spine, canonical, cards, capsules)
        timestamps = [
            cell.proof_refs[0].metadata[0][1] for cell in cells if cell.proof_refs and cell.proof_refs[0].metadata
        ]
        created = min(timestamps) if timestamps else "1970-01-01T00:00:00Z"
        updated = max(timestamps) if timestamps else created
        return EvidenceNotebook(
            notebook_id=notebook_id,
            project_id=canonical,
            title="Workbench Evidence Notebook",
            summary="Derived investigation notebook with product claims backed by live Workbench proof.",
            cells=tuple(cells),
            created_at_utc=created,
            updated_at_utc=updated,
        )

    def _build_cells(
        self,
        spine: WorkbenchSpine,
        project_id: str,
        cards: list[EvidenceAssetCard],
        capsules: list[ReproCapsuleExport],
    ) -> list[EvidenceNotebookCell]:
        if not cards:
            return [
                EvidenceNotebookCell(
                    cell_id="context-empty",
                    kind=EvidenceNotebookCellKind.CONTEXT_NOTE,
                    purpose="context",
                    title="No claim cells available",
                    text="No verified evidence asset cards are available for this project.",
                    is_product_claim=False,
                    proof_refs=(),
                )
            ]
        capsule_by_run = {capsule.manifest.run_id: capsule for capsule in capsules}
        return [
            self._build_product_claim_cell(spine, project_id, card, capsule_by_run)
            for card in sorted(cards, key=lambda row: (row.kind.value, row.asset_card_id))
        ]

    @staticmethod
    def _build_product_claim_cell(
        spine: WorkbenchSpine,
        project_id: str,
        card: EvidenceAssetCard,
        capsule_by_run: dict[str, ReproCapsuleExport],
    ) -> EvidenceNotebookCell:
        if card.proof_status is not ProofStatus.VERIFIED:
            raise EvidenceNotebookError(
                f"evidence asset {card.asset_card_id!r} is not verified",
                code="proof_unavailable",
            )
        runs = _runs_for_card(spine, project_id, card)
        if not runs:
            raise EvidenceNotebookError(f"evidence asset {card.asset_card_id!r} has no run proof")
        refs: list[NotebookProofRef] = [_asset_proof_ref(card)]
        for run in runs:
            refs.extend(_trace_refs(spine, run))
            refs.extend(_eval_refs(spine, run, card))
            capsule = capsule_by_run.get(run.run_id)
            if capsule is None or capsule.proof_status is not CapsuleProofStatus.SEALED:
                raise EvidenceNotebookError(f"run {run.run_id!r} has no sealed rerunnable capsule")
            refs.extend(_capsule_refs(capsule))
        refs.append(_source_ref(card))
        _require_proof_kinds(card.asset_card_id, refs)
        purpose = _purpose_for_kind(card.kind)
        text = _clean_text(
            f"{card.name} is represented as {purpose} evidence with current run, trace, eval, and rerun proof."
        )
        return EvidenceNotebookCell(
            cell_id=f"claim-{card.asset_card_id}",
            kind=EvidenceNotebookCellKind.PRODUCT_CLAIM,
            purpose=purpose,
            title=_clean_text(card.name),
            text=text,
            is_product_claim=True,
            proof_refs=tuple(refs),
        )

    def _get_or_init_spine(self) -> WorkbenchSpine:
        if self._spine is not None:
            return self._spine
        try:
            self._spine = get_workbench_spine()
        except WorkbenchSpineCorrupt:
            raise
        return self._spine

    def _get_or_init_library(self, spine: WorkbenchSpine) -> EvidenceAssetLibrary:
        if self._evidence_library is None:
            self._evidence_library = EvidenceAssetLibrary(spine=spine)
        return self._evidence_library

    def _get_or_init_repro_service(self, spine: WorkbenchSpine) -> ReproCapsuleService:
        if self._repro_service is None:
            self._repro_service = ReproCapsuleService(spine=spine)
        return self._repro_service


def _require_non_empty(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise EvidenceNotebookError(f"{field_name} must be non-empty")


def _canonicalize_project_id(value: str | None) -> str:
    try:
        return validate_project_id(value)
    except WorkbenchProjectIdRejected as exc:
        raise EvidenceNotebookProjectIdRejected(value) from exc


def _purpose_for_kind(kind: EvidenceAssetKind) -> str:
    return {
        EvidenceAssetKind.RELEASE_EVIDENCE_PACKAGE: "release readiness",
        EvidenceAssetKind.MODEL_ROUTE: "model selection",
        EvidenceAssetKind.POLICY: "audit closure",
        EvidenceAssetKind.PUBLIC_EXPORT_SNAPSHOT: "public export preparation",
        EvidenceAssetKind.RUNTIME_PROFILE: "local runtime tuning",
        EvidenceAssetKind.BENCHMARK_RUN: "benchmark comparison",
    }.get(kind, kind.value.replace("_", " "))


def _clean_text(value: str) -> str:
    redacted = redact_text(value)
    if not isinstance(redacted, str):
        raise EvidenceNotebookError("redaction returned unexpected text shape")
    return redacted


def _redacted_metadata(values: dict[str, str]) -> tuple[tuple[str, str], ...]:
    redacted = redact_route_payload(values)
    if not isinstance(redacted, dict):
        raise EvidenceNotebookError("redaction returned unexpected metadata shape")
    return tuple(sorted((str(key), str(value)) for key, value in redacted.items() if str(value).strip()))


def _runs_for_card(spine: WorkbenchSpine, project_id: str, card: EvidenceAssetCard) -> list[WorkbenchRun]:
    runs = [
        run
        for run in spine.list_runs()
        if run.project_id == project_id
        and any(
            asset_id == card.asset_card_id and revision == card.revision for asset_id, revision in run.asset_revisions
        )
    ]
    return sorted(runs, key=lambda row: row.started_at_utc)


def _asset_proof_ref(card: EvidenceAssetCard) -> NotebookProofRef:
    return NotebookProofRef(
        kind=NotebookProofKind.EVIDENCE_ASSET,
        proof_id=card.asset_card_id,
        status=NotebookProofStatus.CURRENT,
        label=_clean_text(card.name),
        source=dict(card.provenance).get("source", "evidence asset card"),
        metadata=_redacted_metadata({"created_at_utc": card.created_at_utc, "revision": card.revision}),
    )


def _trace_refs(spine: WorkbenchSpine, run: WorkbenchRun) -> list[NotebookProofRef]:
    traces = spine.list_traces_for_run(run.run_id)
    if not traces:
        raise EvidenceNotebookError(f"run {run.run_id!r} has no trace proof")
    return [_trace_ref(trace) for trace in traces]


def _trace_ref(trace: WorkbenchTrace) -> NotebookProofRef:
    return NotebookProofRef(
        kind=NotebookProofKind.RUN_TRACE,
        proof_id=trace.trace_id,
        status=NotebookProofStatus.CURRENT,
        label=f"trace {trace.trace_id}",
        source="workbench spine trace",
        metadata=_redacted_metadata({"captured_at_utc": trace.captured_at_utc, "run_id": trace.run_id}),
    )


def _eval_refs(spine: WorkbenchSpine, run: WorkbenchRun, card: EvidenceAssetCard) -> list[NotebookProofRef]:
    evals = [
        eval_result
        for eval_result in spine.list_evals(run_id=run.run_id)
        if eval_result.asset_id == card.asset_card_id
        and eval_result.asset_revision == card.revision
        and all(score.passed for score in eval_result.scores)
    ]
    if not evals:
        raise EvidenceNotebookError(f"run {run.run_id!r} has no passing eval proof")
    return [_eval_ref(eval_result) for eval_result in evals]


def _eval_ref(eval_result: EvalResult) -> NotebookProofRef:
    return NotebookProofRef(
        kind=NotebookProofKind.EVAL_RESULT,
        proof_id=eval_result.eval_id,
        status=NotebookProofStatus.CURRENT,
        label=f"eval {eval_result.eval_id}",
        source=eval_result.kind.value,
        metadata=_redacted_metadata({"captured_at_utc": eval_result.captured_at_utc, "run_id": eval_result.run_id}),
    )


def _source_ref(card: EvidenceAssetCard) -> NotebookProofRef:
    provenance = dict(card.provenance)
    return NotebookProofRef(
        kind=NotebookProofKind.SOURCE_CARD,
        proof_id=f"source:{card.asset_card_id}",
        status=NotebookProofStatus.CURRENT,
        label="source provenance",
        source=provenance.get("source", "evidence asset provenance"),
        metadata=_redacted_metadata(provenance),
    )


def _capsule_refs(capsule: ReproCapsuleExport) -> list[NotebookProofRef]:
    manifest = capsule.manifest
    command = _clean_text(manifest.reproduction_command)
    return [
        NotebookProofRef(
            kind=NotebookProofKind.REPRO_CAPSULE,
            proof_id=manifest.capsule_id,
            status=NotebookProofStatus.CURRENT,
            label=f"sealed capsule {manifest.capsule_id}",
            source="repro capsule service",
            metadata=_redacted_metadata({
                "manifest_hash_sha256": capsule.manifest_hash_sha256,
                "run_id": manifest.run_id,
            }),
        ),
        NotebookProofRef(
            kind=NotebookProofKind.PROOF_COMMAND,
            proof_id=f"command:{manifest.run_id}",
            status=NotebookProofStatus.CURRENT,
            label="rerunnable proof command",
            source="sealed repro capsule",
            reproduction_command=command,
            metadata=_redacted_metadata({"run_id": manifest.run_id}),
        ),
    ]


def _require_proof_kinds(asset_id: str, refs: list[NotebookProofRef]) -> None:
    actual = {ref.kind for ref in refs}
    required = {
        NotebookProofKind.EVIDENCE_ASSET,
        NotebookProofKind.RUN_TRACE,
        NotebookProofKind.EVAL_RESULT,
        NotebookProofKind.SOURCE_CARD,
        NotebookProofKind.PROOF_COMMAND,
        NotebookProofKind.REPRO_CAPSULE,
    }
    missing = required - actual
    if missing:
        raise EvidenceNotebookError(
            f"evidence asset {asset_id!r} missing notebook proof kinds: {sorted(kind.value for kind in missing)}"
        )


__all__ = [
    "EvidenceNotebook",
    "EvidenceNotebookCell",
    "EvidenceNotebookCellKind",
    "EvidenceNotebookError",
    "EvidenceNotebookProjectIdRejected",
    "EvidenceNotebookService",
    "NotebookProofKind",
    "NotebookProofRef",
    "NotebookProofStatus",
]
