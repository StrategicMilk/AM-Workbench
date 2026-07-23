"""Offline-first upstream dependency intelligence.

This module reads configured dependency sources and local observations, then
returns typed proposals. It never edits backend pins, overlay queues, tuning
config, capability state, or installed packages.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

import yaml

from vetinari.constants import OUTPUTS_DIR, PROJECT_ROOT
from vetinari.dependency_intelligence.engine_evaluation import (
    evaluate_dependency_intelligence_impl,
    report_to_dict_impl,
)

DEFAULT_SOURCE_CONFIG = PROJECT_ROOT / "config" / "dependency_sources.yaml"
DEFAULT_FIXTURE_DIR = PROJECT_ROOT / "tests" / "fixtures" / "dependency_intelligence"
READ_ONLY_SURFACES = (
    "config/backend_pins.yaml",
    "config/capabilities.yaml",
    "config/backend_tuning.yaml",
    "third_party_overlays/**",
    str(OUTPUTS_DIR / "capabilities" / "state.jsonl"),
)
MUTATION_FORBIDDEN = (
    "auto_upgrade",
    "pip_install",
    "git_apply",
    "write_backend_pin",
    "write_overlay",
    "write_capability_state",
)


class DependencyIntelligenceError(RuntimeError):
    """Raised when dependency intelligence inputs are missing or malformed."""


class ProposalKind(str, Enum):
    """Typed proposal classes emitted by upstream intelligence."""

    UPSTREAM_RELEASE = "upstream_release"
    PATCH_OVERLAY_STALE = "patch_overlay_stale"
    PATCH_OVERLAY_SUPERSEDED = "patch_overlay_superseded"
    CAPABILITY_NOW_RELEVANT = "capability_now_relevant"
    BENCHMARK_RERUN = "benchmark_rerun"
    BACKEND_HEALTH_REGRESSION = "backend_health_regression"


class RefreshStatus(str, Enum):
    """Overall refresh verdict."""

    READY = "ready"
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True)
class DependencySource:
    """Configured upstream source for one dependency surface."""

    name: str
    ecosystem: str
    package: str
    version_observation: str
    release_feed: str
    source_type: str
    pinned_surface: str
    proposal_tags: tuple[str, ...] = ()

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"DependencySource(name={self.name!r}, ecosystem={self.ecosystem!r}, package={self.package!r})"


@dataclass(frozen=True, slots=True)
class InstalledDependency:
    """Observed local dependency version."""

    name: str
    version: str
    source: str


@dataclass(frozen=True, slots=True)
class UpstreamRelease:
    """Latest upstream release observation from an offline or live source."""

    dependency: str
    version: str
    released_at: str
    notes: str = ""

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"UpstreamRelease(dependency={self.dependency!r}, version={self.version!r}, released_at={self.released_at!r})"


@dataclass(frozen=True, slots=True)
class OverlayStatus:
    """Read-only overlay rebase status for one dependency."""

    dependency: str
    overlay_id: str
    upstream_version: str
    last_checked_upstream_version: str
    rebase_status: str
    retired: bool = False
    purpose: str = ""

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"OverlayStatus(dependency={self.dependency!r}, overlay_id={self.overlay_id!r}, upstream_version={self.upstream_version!r})"


@dataclass(frozen=True, slots=True)
class BenchmarkSignal:
    """Benchmark drift observation that may require a rerun."""

    dependency: str
    baseline_hash: str
    current_hash: str
    drift_percent: float
    threshold_percent: float
    representative_task_count: int

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"BenchmarkSignal(dependency={self.dependency!r}, baseline_hash={self.baseline_hash!r}, current_hash={self.current_hash!r})"


@dataclass(frozen=True, slots=True)
class BackendHealthSignal:
    """Runtime health signal for an installed backend."""

    dependency: str
    status: str
    last_ok_version: str = ""
    error: str = ""

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"BackendHealthSignal(dependency={self.dependency!r}, status={self.status!r}, last_ok_version={self.last_ok_version!r})"


@dataclass(frozen=True, slots=True)
class CapabilityRelevanceSignal:
    """A declined capability that has fresh relevance evidence."""

    capability: str
    dependency: str
    declined_at: str
    relevance_reason: str
    upstream_version: str

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"CapabilityRelevanceSignal(capability={self.capability!r}, dependency={self.dependency!r}, declined_at={self.declined_at!r})"


@dataclass(frozen=True, slots=True)
class DependencyProposal:
    """Typed, non-mutating proposal for an operator or later workflow."""

    proposal_id: str
    kind: ProposalKind
    dependency: str
    title: str
    rationale: str
    suggested_action: str
    evidence: tuple[str, ...]
    blocked_by: tuple[str, ...] = ()
    would_mutate: tuple[str, ...] = ()

    @property
    def blocked(self) -> bool:
        """Return true when the proposal is blocked by refresh prerequisites."""
        return bool(self.blocked_by)

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return (
            f"DependencyProposal(proposal_id={self.proposal_id!r}, kind={self.kind!r}, dependency={self.dependency!r})"
        )


@dataclass(frozen=True, slots=True)
class DependencyIntelligenceReport:
    """Dependency refresh report containing proposals and refresh blockers."""

    status: RefreshStatus
    proposals: tuple[DependencyProposal, ...]
    blocked_reasons: tuple[str, ...]
    read_only_surfaces: tuple[str, ...] = READ_ONLY_SURFACES
    mutation_forbidden: tuple[str, ...] = MUTATION_FORBIDDEN
    source_names: tuple[str, ...] = ()

    def proposals_by_kind(self, kind: ProposalKind) -> tuple[DependencyProposal, ...]:
        """Return proposals matching ``kind``."""
        return tuple(proposal for proposal in self.proposals if proposal.kind is kind)

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"DependencyIntelligenceReport(status={self.status!r}, proposals={self.proposals!r}, blocked_reasons={self.blocked_reasons!r})"


@dataclass(frozen=True, slots=True)
class _Bundle:
    sources: tuple[DependencySource, ...]
    installed: dict[str, InstalledDependency]
    releases: dict[str, UpstreamRelease]
    overlays: tuple[OverlayStatus, ...] = ()
    benchmarks: tuple[BenchmarkSignal, ...] = ()
    health: tuple[BackendHealthSignal, ...] = ()
    capabilities: tuple[CapabilityRelevanceSignal, ...] = ()

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"_Bundle(sources={self.sources!r}, installed={self.installed!r}, releases={self.releases!r})"


def load_dependency_sources(path: str | Path = DEFAULT_SOURCE_CONFIG) -> tuple[DependencySource, ...]:
    """Load dependency source configuration from YAML.

    Returns:
        Resolved dependency sources value.

    Raises:
        Exception: Propagates validation or runtime failures from the underlying operation.
    """
    data = _load_yaml_mapping(Path(path))
    rows = data.get("sources")
    if not isinstance(rows, list) or not rows:
        raise DependencyIntelligenceError(f"{path}: non-empty sources list is required")
    sources = tuple(_source_from_row(row, path=Path(path), index=index) for index, row in enumerate(rows))
    names = [source.name for source in sources]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise DependencyIntelligenceError(f"{path}: duplicate source names: {', '.join(duplicates)}")
    return sources


def evaluate_dependency_intelligence(
    *,
    source_config: str | Path = DEFAULT_SOURCE_CONFIG,
    fixture_dir: str | Path | None = None,
) -> DependencyIntelligenceReport:
    """Evaluate configured dependency surfaces and return typed proposals.

    ``fixture_dir`` enables deterministic offline execution. Network fetchers are
    intentionally absent from this pack; online refresh can feed the same schema
    later without giving this layer permission to upgrade anything.

    Returns:
        DependencyIntelligenceReport value produced by evaluate_dependency_intelligence().

    Raises:
        Exception: Propagates validation or runtime failures from the underlying operation.
    """
    return evaluate_dependency_intelligence_impl(
        source_config=source_config,
        fixture_dir=fixture_dir,
        error_type=DependencyIntelligenceError,
        load_bundle_fn=_load_bundle,
        version_gt_fn=_version_gt,
        release_proposal_fn=_release_proposal,
        stale_overlay_proposal_fn=_stale_overlay_proposal,
        overlay_superseded_proposal_fn=_overlay_superseded_proposal,
        benchmark_proposal_fn=_benchmark_proposal,
        health_proposal_fn=_health_proposal,
        capability_proposal_fn=_capability_proposal,
        dedupe_proposals_fn=_dedupe_proposals,
        report_type=DependencyIntelligenceReport,
        status_type=RefreshStatus,
    )


def report_to_dict(report: DependencyIntelligenceReport) -> dict[str, Any]:
    """Serialize a report to plain JSON-compatible data."""
    return report_to_dict_impl(report)


def _load_bundle(source_config: Path, fixture_dir: Path) -> _Bundle:
    sources = load_dependency_sources(source_config)
    observed = _load_yaml_mapping(fixture_dir / "observations.yaml")
    return _Bundle(
        sources=sources,
        installed={item.name: item for item in _installed_from_rows(observed.get("installed", []))},
        releases={item.dependency: item for item in _releases_from_rows(observed.get("upstream_releases", []))},
        overlays=tuple(_overlay_from_row(row) for row in _require_list(observed, "overlays")),
        benchmarks=tuple(_benchmark_from_row(row) for row in _require_list(observed, "benchmarks")),
        health=tuple(_health_from_row(row) for row in _require_list(observed, "backend_health")),
        capabilities=tuple(_capability_from_row(row) for row in _require_list(observed, "capability_relevance")),
    )


def _load_yaml_mapping(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise DependencyIntelligenceError(f"{path}: file does not exist")
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise DependencyIntelligenceError(f"{path}: YAML parse failed: {exc}") from exc
    if not isinstance(loaded, dict):
        raise DependencyIntelligenceError(f"{path}: expected mapping at document root")
    return loaded


def _require_list(data: dict[str, Any], key: str) -> list[Any]:
    rows = data.get(key, [])
    if not isinstance(rows, list):
        raise DependencyIntelligenceError(f"observations.yaml: {key} must be a list")
    return rows


def _source_from_row(row: Any, *, path: Path, index: int) -> DependencySource:
    if not isinstance(row, dict):
        raise DependencyIntelligenceError(f"{path}: sources[{index}] must be a mapping")
    required = ("name", "ecosystem", "package", "version_observation", "release_feed", "source_type", "pinned_surface")
    missing = [key for key in required if not str(row.get(key, "")).strip()]
    if missing:
        raise DependencyIntelligenceError(f"{path}: sources[{index}] missing {', '.join(missing)}")
    return DependencySource(
        name=str(row["name"]),
        ecosystem=str(row["ecosystem"]),
        package=str(row["package"]),
        version_observation=str(row["version_observation"]),
        release_feed=str(row["release_feed"]),
        source_type=str(row["source_type"]),
        pinned_surface=str(row["pinned_surface"]),
        proposal_tags=tuple(str(item) for item in row.get("proposal_tags", ())),
    )


def _installed_from_rows(rows: Any) -> tuple[InstalledDependency, ...]:
    return tuple(
        InstalledDependency(
            name=str(row["name"]), version=str(row["version"]), source=str(row.get("source", "fixture"))
        )
        for row in _coerce_row_list(rows, "installed")
    )


def _releases_from_rows(rows: Any) -> tuple[UpstreamRelease, ...]:
    return tuple(
        UpstreamRelease(
            dependency=str(row["dependency"]),
            version=str(row["version"]),
            released_at=str(row.get("released_at", "")),
            notes=str(row.get("notes", "")),
        )
        for row in _coerce_row_list(rows, "upstream_releases")
    )


def _overlay_from_row(row: Any) -> OverlayStatus:
    if not isinstance(row, dict):
        raise DependencyIntelligenceError("overlays entries must be mappings")
    retired = row.get("retired", False)
    if not isinstance(retired, bool):
        raise DependencyIntelligenceError("overlays retired must be a boolean")
    return OverlayStatus(
        dependency=str(row["dependency"]),
        overlay_id=str(row["overlay_id"]),
        upstream_version=str(row["upstream_version"]),
        last_checked_upstream_version=str(row["last_checked_upstream_version"]),
        rebase_status=str(row["rebase_status"]),
        retired=retired,
        purpose=str(row.get("purpose", "")),
    )


def _benchmark_from_row(row: Any) -> BenchmarkSignal:
    if not isinstance(row, dict):
        raise DependencyIntelligenceError("benchmarks entries must be mappings")
    return BenchmarkSignal(
        dependency=str(row["dependency"]),
        baseline_hash=str(row["baseline_hash"]),
        current_hash=str(row["current_hash"]),
        drift_percent=float(row["drift_percent"]),
        threshold_percent=float(row["threshold_percent"]),
        representative_task_count=int(row["representative_task_count"]),
    )


def _health_from_row(row: Any) -> BackendHealthSignal:
    if not isinstance(row, dict):
        raise DependencyIntelligenceError("backend_health entries must be mappings")
    return BackendHealthSignal(
        dependency=str(row["dependency"]),
        status=str(row["status"]),
        last_ok_version=str(row.get("last_ok_version", "")),
        error=str(row.get("error", "")),
    )


def _capability_from_row(row: Any) -> CapabilityRelevanceSignal:
    if not isinstance(row, dict):
        raise DependencyIntelligenceError("capability_relevance entries must be mappings")
    return CapabilityRelevanceSignal(
        capability=str(row["capability"]),
        dependency=str(row["dependency"]),
        declined_at=str(row["declined_at"]),
        relevance_reason=str(row["relevance_reason"]),
        upstream_version=str(row["upstream_version"]),
    )


def _coerce_row_list(rows: Any, key: str) -> list[dict[str, Any]]:
    if not isinstance(rows, list):
        raise DependencyIntelligenceError(f"observations.yaml: {key} must be a list")
    if not all(isinstance(row, dict) for row in rows):
        raise DependencyIntelligenceError(f"observations.yaml: {key} entries must be mappings")
    return rows


def _release_proposal(
    source: DependencySource,
    installed: InstalledDependency,
    release: UpstreamRelease,
) -> DependencyProposal:
    return DependencyProposal(
        proposal_id=_proposal_id(ProposalKind.UPSTREAM_RELEASE, source.name, release.version),
        kind=ProposalKind.UPSTREAM_RELEASE,
        dependency=source.name,
        title=f"Review {source.name} {release.version} before changing {source.pinned_surface}",
        rationale=(
            f"Configured source {source.release_feed} reports {release.version}; "
            f"local observation is {installed.version} from {installed.source}."
        ),
        suggested_action="Open a review proposal; do not auto-upgrade the pin or installed package.",
        evidence=(
            f"installed={installed.version}",
            f"upstream={release.version}",
            f"pinned_surface={source.pinned_surface}",
        ),
    )


def _stale_overlay_proposal(overlay: OverlayStatus, reason: str) -> DependencyProposal:
    return DependencyProposal(
        proposal_id=_proposal_id(ProposalKind.PATCH_OVERLAY_STALE, overlay.dependency, overlay.overlay_id),
        kind=ProposalKind.PATCH_OVERLAY_STALE,
        dependency=overlay.dependency,
        title=f"Refresh blocked by stale overlay {overlay.overlay_id}",
        rationale=reason,
        suggested_action="Rebase the overlay to clean or retire it before dependency refresh continues.",
        evidence=(
            f"overlay_upstream_version={overlay.upstream_version}",
            f"last_checked_upstream_version={overlay.last_checked_upstream_version}",
            f"rebase_status={overlay.rebase_status}",
        ),
        blocked_by=(reason,),
    )


def _overlay_superseded_proposal(overlay: OverlayStatus, release: UpstreamRelease) -> DependencyProposal:
    return DependencyProposal(
        proposal_id=_proposal_id(ProposalKind.PATCH_OVERLAY_SUPERSEDED, overlay.dependency, release.version),
        kind=ProposalKind.PATCH_OVERLAY_SUPERSEDED,
        dependency=overlay.dependency,
        title=f"Check whether {release.version} supersedes overlay {overlay.overlay_id}",
        rationale=(
            f"Overlay last checked {overlay.last_checked_upstream_version}; upstream now reports {release.version}. "
            "A local patch may have been accepted upstream or may need rebasing."
        ),
        suggested_action="Run the overlay known-bad/known-good proof and rebase review; do not apply patches automatically.",
        evidence=(f"overlay={overlay.overlay_id}", f"upstream={release.version}", f"purpose={overlay.purpose}"),
    )


def _benchmark_proposal(signal: BenchmarkSignal) -> DependencyProposal:
    return DependencyProposal(
        proposal_id=_proposal_id(ProposalKind.BENCHMARK_RERUN, signal.dependency, signal.current_hash),
        kind=ProposalKind.BENCHMARK_RERUN,
        dependency=signal.dependency,
        title=f"Rerun benchmark window for {signal.dependency}",
        rationale=(
            f"Benchmark drift {signal.drift_percent:.1f}% exceeded threshold {signal.threshold_percent:.1f}% "
            f"or evidence hash changed ({signal.baseline_hash} -> {signal.current_hash})."
        ),
        suggested_action="Queue a benchmark rerun before promotion, rollback, or pin movement decisions.",
        evidence=(
            f"baseline_hash={signal.baseline_hash}",
            f"current_hash={signal.current_hash}",
            f"representative_task_count={signal.representative_task_count}",
        ),
    )


def _health_proposal(signal: BackendHealthSignal) -> DependencyProposal:
    return DependencyProposal(
        proposal_id=_proposal_id(ProposalKind.BACKEND_HEALTH_REGRESSION, signal.dependency, signal.status),
        kind=ProposalKind.BACKEND_HEALTH_REGRESSION,
        dependency=signal.dependency,
        title=f"Investigate {signal.dependency} health status {signal.status}",
        rationale=f"Backend health is {signal.status}; last known OK version is {signal.last_ok_version or 'unknown'}.",
        suggested_action="Open an operator review and rerun health checks after upstream review.",
        evidence=(f"status={signal.status}", f"error={signal.error}"),
    )


def _capability_proposal(signal: CapabilityRelevanceSignal) -> DependencyProposal:
    return DependencyProposal(
        proposal_id=_proposal_id(ProposalKind.CAPABILITY_NOW_RELEVANT, signal.capability, signal.upstream_version),
        kind=ProposalKind.CAPABILITY_NOW_RELEVANT,
        dependency=signal.dependency,
        title=f"Revisit declined capability {signal.capability}",
        rationale=(
            f"{signal.capability} was declined at {signal.declined_at}, but {signal.dependency} "
            f"{signal.upstream_version} now makes it relevant: {signal.relevance_reason}."
        ),
        suggested_action="Notify the user and request explicit approval before any install attempt.",
        evidence=(f"capability={signal.capability}", f"upstream={signal.upstream_version}"),
    )


def _dedupe_proposals(proposals: list[DependencyProposal]) -> list[DependencyProposal]:
    seen: set[str] = set()
    deduped: list[DependencyProposal] = []
    for proposal in proposals:
        if proposal.proposal_id in seen:
            continue
        seen.add(proposal.proposal_id)
        deduped.append(proposal)
    return deduped


def _proposal_id(kind: ProposalKind, dependency: str, value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9]+", "-", f"{kind.value}-{dependency}-{value}").strip("-").upper()
    return f"DEPINT-{safe[:96]}"


def _version_gt(candidate: str, current: str) -> bool:
    return _version_key(candidate) > _version_key(current)


def _version_key(value: str) -> tuple[int, ...]:
    numbers = tuple(int(part) for part in re.findall(r"\d+", value))
    return numbers or (0,)


def _to_json(report: DependencyIntelligenceReport) -> str:
    return json.dumps(report_to_dict(report), indent=2, sort_keys=True)
