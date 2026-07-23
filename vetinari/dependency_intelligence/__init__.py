"""Dependency upstream intelligence and refresh proposal surface."""

from __future__ import annotations

from vetinari.dependency_intelligence.engine import (
    BackendHealthSignal,
    BenchmarkSignal,
    CapabilityRelevanceSignal,
    DependencyIntelligenceError,
    DependencyIntelligenceReport,
    DependencyProposal,
    DependencySource,
    InstalledDependency,
    OverlayStatus,
    ProposalKind,
    RefreshStatus,
    UpstreamRelease,
    evaluate_dependency_intelligence,
    load_dependency_sources,
    report_to_dict,
)

__all__ = [
    "BackendHealthSignal",
    "BenchmarkSignal",
    "CapabilityRelevanceSignal",
    "DependencyIntelligenceError",
    "DependencyIntelligenceReport",
    "DependencyProposal",
    "DependencySource",
    "InstalledDependency",
    "OverlayStatus",
    "ProposalKind",
    "RefreshStatus",
    "UpstreamRelease",
    "evaluate_dependency_intelligence",
    "load_dependency_sources",
    "report_to_dict",
]
