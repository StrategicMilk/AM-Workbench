"""Typed AM Workbench binding object model above the storage spine.

Every Wave-13+ AM Workbench depth pack imports card types from this module
instead of defining parallel persistence shapes. The storage layer remains
``vetinari.workbench.metadata_spine``; this module is the read-only typed API
and schema anchor for Python and non-Python consumers.

Side effects: importing this module opens no files, starts no threads,
registers no callbacks, and subscribes to no event buses. The only file read is
inside ``validate_no_parallel_persistence`` when a caller explicitly asks to
scan one module.

JSON Schema export: schemas/workbench_spine.schema.json.
Decision-Ref: ADR-0125
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from vetinari.workbench.assets import AssetCard, AssetCardKind, AssetKind, AssetTaint, WorkbenchAsset
from vetinari.workbench.evals import EvalKind, EvalResult, EvalScore
from vetinari.workbench.evidence_assets import (
    EvidenceAssetCard,
    EvidenceAssetFailureRecord,
    EvidenceAssetKind,
    ProofStatus,
)
from vetinari.workbench.leases import LeaseStatus, WorkbenchLease
from vetinari.workbench.method_library import (
    MeasuredDelta,
    MethodCard,
    MethodEvidenceRef,
    MethodKind,
    PromotionStatus,
)
from vetinari.workbench.proposals import (
    Promotion,
    ProposalGate,
    ProposalStatus,
    WorkbenchProposal,
    WorkbenchProposalKind,
)
from vetinari.workbench.runs import RunKind, RunMetric, RunStatus, WorkbenchRun
from vetinari.workbench.source_cards import (
    FreshnessPolicy,
    SourceCard,
    SourceKind,
    StalenessAction,
)
from vetinari.workbench.tool_cards import ClaimPromotionPolicy, ToolCard, ToolKind
from vetinari.workbench.traces import TraceSpan, WorkbenchTrace

Run = WorkbenchRun
Trace = WorkbenchTrace
Proposal = WorkbenchProposal
Lease = WorkbenchLease

_PROJECT_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_FORBIDDEN_STORE_PATTERNS = (
    re.compile(r"outputs[/\\]workbench[/\\](?!spine(?:[/\\]|[\"'`)]))[^\"'`)\s]+", re.IGNORECASE),
    re.compile(r"Path\([\"']outputs[/\\]workbench[/\\](?!spine(?:[/\\]|[\"']))[^\"']+[\"']\)", re.IGNORECASE),
    re.compile(r"sqlite3\.connect\([^)]*outputs[/\\]workbench[/\\](?!spine(?:[/\\]|[\"']))", re.IGNORECASE),
    re.compile(
        r"\.(?:open|write_text|write_bytes)\([^)]*outputs[/\\]workbench[/\\](?!spine(?:[/\\]|[\"']))", re.IGNORECASE
    ),
)
_LEGACY_PARALLEL_PERSISTENCE_ALLOWLIST = frozenset({
    "dataset_revisions.py",
    "local_runtime_onboarding.py",
    "rag_debugger.py",
})


class WorkbenchProjectIdRejected(ValueError):
    """Raised when a project id is unsafe for workbench storage paths."""

    def __init__(self, value: object) -> None:
        super().__init__(
            f"rejected workbench project_id {value!r}; use 1-64 ASCII letters, digits, '_' or '-' with no path markers"
        )


class WorkbenchParallelPersistence(RuntimeError):
    """Raised when workbench code attempts to bypass WorkbenchSpine storage."""

    def __init__(self, module_path: Path, pattern: str) -> None:
        super().__init__(
            f"{module_path} declares parallel workbench persistence matching {pattern!r}; "
            "route workbench writes through WorkbenchSpine instead"
        )


def _require_non_empty(value: str, field_name: str) -> None:
    if not value or not value.strip():
        raise ValueError(f"{field_name} must be non-empty")


def _require_string_tuple(values: tuple[str, ...], field_name: str) -> None:
    if not isinstance(values, tuple):
        raise ValueError(f"{field_name} must be a tuple")
    if not all(isinstance(value, str) and value.strip() for value in values):
        raise ValueError(f"{field_name} must contain non-empty strings")


@dataclass(frozen=True, slots=True)
class Workspace:
    """Top-level workbench container."""

    workspace_id: str
    name: str
    created_at_utc: str
    default_project_id: str

    def __post_init__(self) -> None:
        _require_non_empty(self.workspace_id, "workspace_id")
        _require_non_empty(self.name, "name")
        _require_non_empty(self.created_at_utc, "created_at_utc")
        validate_project_id(self.default_project_id)

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return (
            f"Workspace(workspace_id={self.workspace_id!r}, name={self.name!r}, created_at_utc={self.created_at_utc!r})"
        )


@dataclass(frozen=True, slots=True)
class ModeTemplate:
    """Product-facing workflow contract for a workbench mode."""

    template_id: str
    name: str
    version: str
    charter: str
    allowed_tools: tuple[str, ...]
    output_schema_ref: str

    def __post_init__(self) -> None:
        _require_non_empty(self.template_id, "template_id")
        _require_non_empty(self.name, "name")
        _require_non_empty(self.version, "version")
        _require_non_empty(self.charter, "charter")
        _require_string_tuple(self.allowed_tools, "allowed_tools")
        _require_non_empty(self.output_schema_ref, "output_schema_ref")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"ModeTemplate(template_id={self.template_id!r}, name={self.name!r}, version={self.version!r})"


@dataclass(frozen=True, slots=True)
class SpineCapabilityPack:
    """Trusted installable bundle of workbench capabilities."""

    pack_id: str
    name: str
    version: str
    signed_by: str
    capabilities: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_non_empty(self.pack_id, "pack_id")
        _require_non_empty(self.name, "name")
        _require_non_empty(self.version, "version")
        _require_non_empty(self.signed_by, "signed_by")
        _require_string_tuple(self.capabilities, "capabilities")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"SpineCapabilityPack(pack_id={self.pack_id!r}, name={self.name!r}, version={self.version!r})"


@dataclass(frozen=True, slots=True)
class SpineDatasetRevision:
    """Dataset revision card bound to a stored asset."""

    revision_id: str
    asset_id: str
    parent_revision_id: str | None
    captured_at_utc: str
    lineage_pointer: str

    def __post_init__(self) -> None:
        _require_non_empty(self.revision_id, "revision_id")
        _require_non_empty(self.asset_id, "asset_id")
        if self.parent_revision_id is not None:
            _require_non_empty(self.parent_revision_id, "parent_revision_id")
        _require_non_empty(self.captured_at_utc, "captured_at_utc")
        _require_non_empty(self.lineage_pointer, "lineage_pointer")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"SpineDatasetRevision(revision_id={self.revision_id!r}, asset_id={self.asset_id!r}, parent_revision_id={self.parent_revision_id!r})"


@dataclass(frozen=True, slots=True)
class EvalSuite:
    """Grouping of EvalResult records under one rubric."""

    suite_id: str
    name: str
    eval_ids: tuple[str, ...]
    rubric_ref: str

    def __post_init__(self) -> None:
        _require_non_empty(self.suite_id, "suite_id")
        _require_non_empty(self.name, "name")
        _require_string_tuple(self.eval_ids, "eval_ids")
        _require_non_empty(self.rubric_ref, "rubric_ref")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"EvalSuite(suite_id={self.suite_id!r}, name={self.name!r}, eval_ids={self.eval_ids!r})"


@dataclass(frozen=True, slots=True)
class Experiment:
    """Controlled comparison of one or more workbench runs."""

    experiment_id: str
    hypothesis: str
    run_ids: tuple[str, ...]
    outcome: str | None

    def __post_init__(self) -> None:
        _require_non_empty(self.experiment_id, "experiment_id")
        _require_non_empty(self.hypothesis, "hypothesis")
        _require_string_tuple(self.run_ids, "run_ids")
        if self.outcome is not None:
            _require_non_empty(self.outcome, "outcome")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"Experiment(experiment_id={self.experiment_id!r}, hypothesis={self.hypothesis!r}, run_ids={self.run_ids!r})"


@dataclass(frozen=True, slots=True)
class Automation:
    """Automation builder binding card."""

    automation_id: str
    name: str
    trigger: str
    steps: tuple[str, ...]
    approver_required: bool

    def __post_init__(self) -> None:
        _require_non_empty(self.automation_id, "automation_id")
        _require_non_empty(self.name, "name")
        _require_non_empty(self.trigger, "trigger")
        _require_string_tuple(self.steps, "steps")
        if not isinstance(self.approver_required, bool):
            raise ValueError("approver_required must be bool")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"Automation(automation_id={self.automation_id!r}, name={self.name!r}, trigger={self.trigger!r})"


@dataclass(frozen=True, slots=True)
class ReproCapsule:
    """Portable, sealed proof capsule for reproducing one run."""

    capsule_id: str
    run_id: str
    snapshot_uri: str
    sealed_at_utc: str
    hash_sha256: str

    def __post_init__(self) -> None:
        _require_non_empty(self.capsule_id, "capsule_id")
        _require_non_empty(self.run_id, "run_id")
        _require_non_empty(self.snapshot_uri, "snapshot_uri")
        _require_non_empty(self.sealed_at_utc, "sealed_at_utc")
        _require_non_empty(self.hash_sha256, "hash_sha256")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return (
            f"ReproCapsule(capsule_id={self.capsule_id!r}, run_id={self.run_id!r}, snapshot_uri={self.snapshot_uri!r})"
        )


BINDING_CARD_TYPES = (
    Workspace,
    ModeTemplate,
    SpineCapabilityPack,
    SourceCard,
    AssetCard,
    EvidenceAssetCard,
    MethodCard,
    ToolCard,
    Run,
    Trace,
    SpineDatasetRevision,
    EvalSuite,
    Experiment,
    Proposal,
    Automation,
    ReproCapsule,
)


def validate_project_id(value: str | None) -> str:
    """Return a canonical project id or raise on traversal-bearing input.

    Returns:
        Validation outcome for project id.

    Raises:
        Exception: Propagates validation or runtime failures from the underlying operation.
    """
    if not isinstance(value, str):
        raise WorkbenchProjectIdRejected(value)
    if not value or len(value) > 64:
        raise WorkbenchProjectIdRejected(value)
    if value in {".", ".."} or any(marker in value for marker in ("/", "\\", "..", "\x00", " ", ";")):
        raise WorkbenchProjectIdRejected(value)
    if _PROJECT_ID_RE.fullmatch(value) is None:
        raise WorkbenchProjectIdRejected(value)
    return value


def validate_no_parallel_persistence(module_path: Path | str) -> None:
    """Reject workbench modules that define their own output JSONL/SQLite store.

    Raises:
        Exception: Propagates validation or runtime failures from the underlying operation.
    """
    if not isinstance(module_path, (Path, str)):
        raise TypeError("module_path must be pathlib.Path or str")
    path = Path(module_path)
    if not path.is_file():
        raise FileNotFoundError(path)
    source = path.read_text(encoding="utf-8")
    if path.name in _LEGACY_PARALLEL_PERSISTENCE_ALLOWLIST:
        return None
    for pattern in _FORBIDDEN_STORE_PATTERNS:
        if pattern.search(source):
            raise WorkbenchParallelPersistence(path, pattern.pattern)
    return None


__all__ = [
    "BINDING_CARD_TYPES",
    "AssetCard",
    "AssetCardKind",
    "AssetKind",
    "AssetTaint",
    "Automation",
    "ClaimPromotionPolicy",
    "EvalKind",
    "EvalResult",
    "EvalScore",
    "EvalSuite",
    "EvidenceAssetCard",
    "EvidenceAssetFailureRecord",
    "EvidenceAssetKind",
    "Experiment",
    "FreshnessPolicy",
    "Lease",
    "LeaseStatus",
    "MeasuredDelta",
    "MethodCard",
    "MethodEvidenceRef",
    "MethodKind",
    "ModeTemplate",
    "Promotion",
    "PromotionStatus",
    "ProofStatus",
    "Proposal",
    "ProposalGate",
    "ProposalStatus",
    "ReproCapsule",
    "Run",
    "RunKind",
    "RunMetric",
    "RunStatus",
    "SourceCard",
    "SourceKind",
    "SpineCapabilityPack",
    "SpineDatasetRevision",
    "StalenessAction",
    "ToolCard",
    "ToolKind",
    "Trace",
    "TraceSpan",
    "WorkbenchAsset",
    "WorkbenchLease",
    "WorkbenchParallelPersistence",
    "WorkbenchProjectIdRejected",
    "WorkbenchProposal",
    "WorkbenchProposalKind",
    "WorkbenchRun",
    "WorkbenchTrace",
    "Workspace",
    "validate_no_parallel_persistence",
    "validate_project_id",
]
