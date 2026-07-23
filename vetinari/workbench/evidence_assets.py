"""Evidence-backed product asset cards over the Workbench metadata spine.

This derived-view layer turns raw Workbench records into product-facing asset
cards. It is read-only against the spine and never appends. The first
load_kind_catalog call reads config/workbench/evidence_asset_kinds.yaml under
_KIND_CATALOG_LOCK; imports perform no I/O and start no background threads.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

import yaml

from vetinari.constants import PROJECT_ROOT
from vetinari.security.redaction import redact_route_payload
from vetinari.workbench.assets import AssetKind, AssetTaint, WorkbenchAsset
from vetinari.workbench.evals import EvalResult
from vetinari.workbench.metadata_spine import WorkbenchSpine, WorkbenchSpineCorrupt, get_workbench_spine
from vetinari.workbench.proposals import ProposalStatus, WorkbenchProposal
from vetinari.workbench.runs import RunStatus, WorkbenchRun

logger = logging.getLogger(__name__)


_KIND_CATALOG_CONFIG_PATH: Path = PROJECT_ROOT / "config" / "workbench" / "evidence_asset_kinds.yaml"
_KIND_CATALOG_LOCK: threading.Lock = threading.Lock()
_KIND_CATALOG_CACHE: dict[str, dict[str, str]] = {}


class EvidenceAssetProjectIdRejected(ValueError):
    """Raised when a project id is not canonical."""

    def __init__(self, value: object) -> None:
        super().__init__(f"invalid project_id {value!r}; use [A-Za-z0-9_-] up to 64 characters")
        self.value = value


class EvidenceAssetLibraryError(Exception):
    """Raised when evidence asset cards cannot be safely served."""


class EvidenceAssetKind(str, Enum):
    """Product-level asset kinds surfaced in evidence cards."""

    PROMPT = "prompt"
    MODEL_ROUTE = "model_route"
    DATASET = "dataset"
    EVAL_SUITE = "eval_suite"
    TRACE = "trace"
    ADAPTER = "adapter"
    SCRAPING_RECIPE = "scraping_recipe"
    RUNTIME_PROFILE = "runtime_profile"
    POLICY = "policy"
    BENCHMARK_RUN = "benchmark_run"
    RELEASE_EVIDENCE_PACKAGE = "release_evidence_package"
    PUBLIC_EXPORT_SNAPSHOT = "public_export_snapshot"


class ProofStatus(str, Enum):
    """Fail-closed proof maturity for an evidence asset card."""

    UNKNOWN = "unknown"
    UNVERIFIED = "unverified"
    PARTIALLY_VERIFIED = "partially_verified"
    VERIFIED = "verified"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class EvidenceAssetFailureRecord:
    """One failed run, blocked proposal, or blocking taint."""

    failure_id: str
    run_id: str
    kind: str
    summary: str
    recorded_at_utc: str

    def __post_init__(self) -> None:
        _require_non_empty(self.failure_id, "failure_id")
        _require_non_empty(self.kind, "kind")
        _require_non_empty(self.summary, "summary")
        _require_non_empty(self.recorded_at_utc, "recorded_at_utc")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"EvidenceAssetFailureRecord(failure_id={self.failure_id!r}, run_id={self.run_id!r}, kind={self.kind!r})"

    def to_dict(self) -> dict[str, Any]:
        """Return the evidence-asset API JSON contract for this failure."""
        return {
            "failure_id": self.failure_id,
            "run_id": self.run_id,
            "kind": self.kind,
            "summary": self.summary,
            "recorded_at_utc": self.recorded_at_utc,
        }


@dataclass(frozen=True, slots=True)
class EvidenceAssetCard:
    """Product-facing evidence card derived from the metadata spine."""

    asset_card_id: str
    kind: EvidenceAssetKind
    name: str
    revision: str
    created_at_utc: str
    provenance: tuple[tuple[str, str], ...]
    dependencies: tuple[str, ...]
    proof_status: ProofStatus
    failure_history: tuple[EvidenceAssetFailureRecord, ...]
    project_id: str
    taints: tuple[AssetTaint, ...] = ()
    eval_signals: tuple[tuple[str, str, bool], ...] = ()
    recent_runs: tuple[tuple[str, str, str], ...] = ()
    metadata: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_non_empty(self.asset_card_id, "asset_card_id")
        _require_non_empty(self.name, "name")
        _require_non_empty(self.revision, "revision")
        _require_non_empty(self.created_at_utc, "created_at_utc")
        _canonicalize_project_id(self.project_id)
        if not isinstance(self.kind, EvidenceAssetKind):
            raise ValueError("kind must be an EvidenceAssetKind")
        if not isinstance(self.proof_status, ProofStatus):
            raise ValueError("proof_status must be a ProofStatus")
        if not dict(self.provenance).get("source", "").strip():
            raise ValueError("provenance.source must be non-empty")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"EvidenceAssetCard(asset_card_id={self.asset_card_id!r}, kind={self.kind!r}, name={self.name!r})"

    def to_dict(self) -> dict[str, Any]:
        """Return the evidence-asset API JSON contract for this card."""
        return {
            "asset_card_id": self.asset_card_id,
            "kind": self.kind.value,
            "name": self.name,
            "revision": self.revision,
            "created_at_utc": self.created_at_utc,
            "project_id": self.project_id,
            "provenance": list(self.provenance),
            "dependencies": list(self.dependencies),
            "proof_status": self.proof_status.value,
            "failure_history": [record.to_dict() for record in self.failure_history],
            "taints": [
                {
                    "taint_id": taint.taint_id,
                    "severity": taint.severity,
                    "reason": taint.reason,
                    "attached_at_utc": taint.attached_at_utc,
                }
                for taint in self.taints
            ],
            "eval_signals": [
                {"eval_id": eval_id, "metric_name": metric_name, "passed": passed}
                for eval_id, metric_name, passed in self.eval_signals
            ],
            "recent_runs": [
                {"run_id": run_id, "status": status, "finished_at_utc": finished_at_utc}
                for run_id, status, finished_at_utc in self.recent_runs
            ],
        }


def _require_non_empty(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be non-empty")


def _canonicalize_project_id(value: str | None) -> str:
    """Return the shared spine project id or fail closed with this module's error."""
    from vetinari.workbench.spine import WorkbenchProjectIdRejected, validate_project_id

    try:
        return validate_project_id(value)
    except WorkbenchProjectIdRejected as exc:
        raise EvidenceAssetProjectIdRejected(value) from exc


def _load_kind_catalog_uncached() -> dict[str, dict[str, str]]:
    try:
        data = yaml.safe_load(_KIND_CATALOG_CONFIG_PATH.read_text(encoding="utf-8"))
    except OSError as exc:
        raise EvidenceAssetLibraryError("evidence asset kind catalog unreadable") from exc
    except yaml.YAMLError as exc:
        raise EvidenceAssetLibraryError("evidence asset kind catalog is invalid YAML") from exc
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise EvidenceAssetLibraryError("evidence asset kind catalog schema_version must be 1")
    rows = data.get("kinds")
    if not isinstance(rows, list):
        raise EvidenceAssetLibraryError("evidence asset kind catalog must contain a kinds list")
    expected = {kind.value for kind in EvidenceAssetKind}
    catalog: dict[str, dict[str, str]] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise EvidenceAssetLibraryError("evidence asset kind catalog rows must be mappings")
        kind_id = row.get("id")
        label = row.get("display_label")
        description = row.get("description")
        if not all(isinstance(value, str) and value.strip() for value in (kind_id, label, description)):
            raise EvidenceAssetLibraryError(f"evidence asset kind catalog row is incomplete: {row!r}")
        if kind_id not in expected:
            raise EvidenceAssetLibraryError(f"evidence asset kind catalog contains unknown kind {kind_id!r}")
        catalog[kind_id] = {"id": kind_id, "display_label": label, "description": description}
    if set(catalog) != expected:
        raise EvidenceAssetLibraryError(
            f"evidence asset kind catalog mismatch: live={sorted(catalog)} expected={sorted(expected)}"
        )
    return catalog


def load_kind_catalog() -> dict[str, dict[str, str]]:
    """Return the cached evidence asset kind catalog as a defensive copy.

    Returns:
        Resolved kind catalog value.
    """
    if _KIND_CATALOG_CACHE:
        return {key: dict(value) for key, value in _KIND_CATALOG_CACHE.items()}
    with _KIND_CATALOG_LOCK:
        if not _KIND_CATALOG_CACHE:
            _KIND_CATALOG_CACHE.update(_load_kind_catalog_uncached())
        return {key: dict(value) for key, value in _KIND_CATALOG_CACHE.items()}


def _derive_proof_status(
    eval_signals: tuple[tuple[str, str, bool], ...],
    failure_count: int,
    taints: tuple[AssetTaint, ...],
    *,
    run_count: int = 0,
) -> ProofStatus:
    """Derive fail-closed proof maturity from eval, run, and taint evidence."""
    if any(taint.severity == "blocker" for taint in taints):
        return ProofStatus.FAILED
    if any(not passed for _eval_id, _metric_name, passed in eval_signals):
        return ProofStatus.FAILED
    if not eval_signals:
        return ProofStatus.UNKNOWN
    if run_count <= 0:
        return ProofStatus.UNVERIFIED
    if failure_count > 0 or taints:
        return ProofStatus.PARTIALLY_VERIFIED
    return ProofStatus.VERIFIED


def _asset_kind_to_evidence_kind(asset: WorkbenchAsset) -> EvidenceAssetKind:
    override = asset.provenance.get("evidence_asset_kind")
    if override:
        try:
            return EvidenceAssetKind(override)
        except ValueError as exc:
            raise EvidenceAssetLibraryError(
                f"asset {asset.asset_id!r} has invalid evidence_asset_kind {override!r}"
            ) from exc
    mapping = {
        AssetKind.PROMPT: EvidenceAssetKind.PROMPT,
        AssetKind.MODEL: EvidenceAssetKind.MODEL_ROUTE,
        AssetKind.DATASET: EvidenceAssetKind.DATASET,
        AssetKind.ADAPTER: EvidenceAssetKind.ADAPTER,
        AssetKind.EVAL_SUITE: EvidenceAssetKind.EVAL_SUITE,
        AssetKind.TOOL: EvidenceAssetKind.SCRAPING_RECIPE,
    }
    return mapping[asset.kind]


def _join_runs_traces_evals_proposals(
    spine: WorkbenchSpine,
    asset: WorkbenchAsset,
    project_id: str,
) -> tuple[list[WorkbenchRun], list[EvalResult], list[WorkbenchProposal]]:
    runs = [
        run
        for run in spine.list_runs()
        if run.project_id == project_id and (asset.asset_id, asset.revision) in run.asset_revisions
    ]
    run_ids = {run.run_id for run in runs}
    evals = [
        result
        for result in spine.list_evals(asset_id=asset.asset_id)
        if result.asset_revision == asset.revision and result.run_id in run_ids
    ]
    proposals = [
        proposal
        for proposal in spine.list_proposals()
        if (asset.asset_id, asset.revision) in proposal.affected_revisions
    ]
    return runs, evals, proposals


class EvidenceAssetLibrary:
    """Read-only builder for EvidenceAssetCard objects."""

    def __init__(self, spine: WorkbenchSpine | None = None) -> None:
        self._spine = spine
        self._read_lock = threading.Lock()

    def list_cards(
        self,
        *,
        project_id: str = "default",
        kind: EvidenceAssetKind | str | None = None,
        proof_status: ProofStatus | str | None = None,
        taints_present: bool | None = None,
        limit: int | None = None,
    ) -> list[EvidenceAssetCard]:
        """Return evidence asset cards for a canonical project id.

        Returns:
            Collection of cards values.

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        canonical = _canonicalize_project_id(project_id)
        load_kind_catalog()
        spine = self._get_or_init_spine()
        parsed_kind = EvidenceAssetKind(kind) if isinstance(kind, str) else kind
        parsed_status = ProofStatus(proof_status) if isinstance(proof_status, str) else proof_status
        with self._read_lock:
            project_asset_ids = self._project_asset_ids(spine, canonical)
            cards: list[EvidenceAssetCard] = []
            for asset in spine.list_assets(taints_present=taints_present):
                asset_project = asset.provenance.get("project_id")
                if not (asset_project == canonical or (asset_project is None and asset.asset_id in project_asset_ids)):
                    continue
                card = self.build_card_for_asset(spine, asset, canonical)
                if parsed_kind is not None and card.kind is not parsed_kind:
                    continue
                if parsed_status is not None and card.proof_status is not parsed_status:
                    continue
                cards.append(card)
        cards.sort(key=lambda card: (card.created_at_utc, card.asset_card_id))
        if limit is not None:
            if limit < 1 or limit > 500:
                raise EvidenceAssetLibraryError("limit must be between 1 and 500")
            cards = cards[:limit]
        return cards

    def get_card(self, *, project_id: str = "default", asset_card_id: str) -> EvidenceAssetCard | None:
        """Return a single evidence asset card by stable card id.

        Returns:
            Resolved card value.

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        canonical = _canonicalize_project_id(project_id)
        if not asset_card_id or not asset_card_id.strip():
            raise EvidenceAssetLibraryError("asset_card_id must be non-empty")
        for card in self.list_cards(project_id=canonical):
            if card.asset_card_id == asset_card_id:
                return card
        return None

    def build_card_for_asset(
        self,
        spine: WorkbenchSpine,
        asset: WorkbenchAsset,
        project_id: str,
    ) -> EvidenceAssetCard:
        """Build one immutable card from a spine asset and related records.

        Args:
            spine: Spine value consumed by build_card_for_asset().
            asset: Asset value consumed by build_card_for_asset().
            project_id: Project identifier that scopes the operation.

        Returns:
            Newly constructed card for asset value.
        """
        runs, evals, proposals = _join_runs_traces_evals_proposals(spine, asset, project_id)
        eval_signals = tuple(
            (result.eval_id, score.metric_name, score.passed) for result in evals for score in result.scores
        )
        failed_runs = [run for run in runs if run.status is RunStatus.FAILED]
        blocked_proposals = [proposal for proposal in proposals if proposal.status is ProposalStatus.BLOCKED]
        failure_history = self._build_failure_history(asset, failed_runs, blocked_proposals)
        proof_status = _derive_proof_status(
            eval_signals,
            len(failed_runs) + len(blocked_proposals),
            asset.taints,
            run_count=len(runs),
        )
        return EvidenceAssetCard(
            asset_card_id=asset.asset_id,
            kind=_asset_kind_to_evidence_kind(asset),
            name=asset.name,
            revision=asset.revision,
            created_at_utc=asset.created_at_utc,
            provenance=_redacted_provenance_pairs(asset.provenance),
            dependencies=self._build_dependencies(asset, runs, proposals),
            proof_status=proof_status,
            failure_history=failure_history,
            project_id=project_id,
            taints=asset.taints,
            eval_signals=eval_signals,
            recent_runs=tuple(
                (run.run_id, run.status.value, run.finished_at_utc)
                for run in sorted(runs, key=lambda row: row.started_at_utc, reverse=True)[:10]
            ),
            metadata={"source_asset_kind": asset.kind.value},
        )

    def _get_or_init_spine(self) -> WorkbenchSpine:
        if self._spine is not None:
            return self._spine
        try:
            self._spine = get_workbench_spine()
        except WorkbenchSpineCorrupt as exc:
            raise EvidenceAssetLibraryError("workbench spine unavailable; repair the metadata spine") from exc
        return self._spine

    @staticmethod
    def _project_asset_ids(spine: WorkbenchSpine, project_id: str) -> set[str]:
        return {
            asset_id
            for run in spine.list_runs()
            if run.project_id == project_id
            for asset_id, _revision in run.asset_revisions
        }

    @staticmethod
    def _build_dependencies(
        asset: WorkbenchAsset,
        runs: list[WorkbenchRun],
        proposals: list[WorkbenchProposal],
    ) -> tuple[str, ...]:
        dependencies: set[str] = set()
        dependencies.update(part.strip() for part in asset.provenance.get("depends_on", "").split(",") if part.strip())
        for run in runs:
            dependencies.update(asset_id for asset_id, _revision in run.asset_revisions if asset_id != asset.asset_id)
        for proposal in proposals:
            dependencies.update(asset_id for asset_id in proposal.affected_assets if asset_id != asset.asset_id)
        return tuple(sorted(dependencies))

    @staticmethod
    def _build_failure_history(
        asset: WorkbenchAsset,
        failed_runs: list[WorkbenchRun],
        blocked_proposals: list[WorkbenchProposal],
    ) -> tuple[EvidenceAssetFailureRecord, ...]:
        records = [
            EvidenceAssetFailureRecord(
                failure_id=f"run:{run.run_id}",
                run_id=run.run_id,
                kind="failed_run",
                summary=f"Run {run.run_id} failed for asset {asset.asset_id}",
                recorded_at_utc=run.finished_at_utc or run.started_at_utc,
            )
            for run in failed_runs
        ]
        records.extend(
            EvidenceAssetFailureRecord(
                failure_id=f"proposal:{proposal.proposal_id}",
                run_id="",
                kind="blocked_proposal",
                summary="; ".join(proposal.gate.blockers)
                or proposal.notes
                or f"Proposal {proposal.proposal_id} blocked",
                recorded_at_utc=proposal.closed_at_utc or proposal.opened_at_utc,
            )
            for proposal in blocked_proposals
        )
        records.extend(
            EvidenceAssetFailureRecord(
                failure_id=f"taint:{taint.taint_id}",
                run_id="",
                kind=f"{taint.severity}_taint",
                summary=taint.reason,
                recorded_at_utc=taint.attached_at_utc,
            )
            for taint in asset.taints
            if taint.severity == "blocker"
        )
        return tuple(sorted(records, key=lambda record: (record.recorded_at_utc, record.failure_id)))


def _redacted_provenance_pairs(provenance: dict[str, str]) -> tuple[tuple[str, str], ...]:
    redacted = redact_route_payload(provenance)
    if not isinstance(redacted, dict):
        raise EvidenceAssetLibraryError("redaction returned unexpected provenance shape")
    return tuple(sorted((str(key), str(value)) for key, value in redacted.items() if str(value).strip()))


__all__ = [
    "EvidenceAssetCard",
    "EvidenceAssetFailureRecord",
    "EvidenceAssetKind",
    "EvidenceAssetLibrary",
    "EvidenceAssetLibraryError",
    "EvidenceAssetProjectIdRejected",
    "ProofStatus",
    "load_kind_catalog",
]
