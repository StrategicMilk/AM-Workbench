"""Evaluation helpers for dependency intelligence engine wrappers."""

from __future__ import annotations

from pathlib import Path
from typing import Any


def evaluate_dependency_intelligence_impl(
    *,
    source_config: str | Path,
    fixture_dir: str | Path | None,
    error_type: type[Exception],
    load_bundle_fn: Any,
    version_gt_fn: Any,
    release_proposal_fn: Any,
    stale_overlay_proposal_fn: Any,
    overlay_superseded_proposal_fn: Any,
    benchmark_proposal_fn: Any,
    health_proposal_fn: Any,
    capability_proposal_fn: Any,
    dedupe_proposals_fn: Any,
    report_type: Any,
    status_type: Any,
) -> Any:
    """Evaluate observed dependency data using injected engine primitives.

    Args:
        source_config: Config file path listing dependency sources.
        fixture_dir: Directory containing offline observation fixtures.
        error_type: Exception type raised for unavailable live evaluation.
        load_bundle_fn: Callable that loads typed observation bundles.
        version_gt_fn: Callable that compares dependency versions.
        release_proposal_fn: Callable that builds release proposals.
        stale_overlay_proposal_fn: Callable that builds stale-overlay proposals.
        overlay_superseded_proposal_fn: Callable that builds superseded-overlay proposals.
        benchmark_proposal_fn: Callable that builds benchmark proposals.
        health_proposal_fn: Callable that builds backend-health proposals.
        capability_proposal_fn: Callable that builds capability proposals.
        dedupe_proposals_fn: Callable that removes duplicate proposals.
        report_type: Report class used for the final return value.
        status_type: Refresh status enum used for ready/blocked state.

    Returns:
        A dependency intelligence report instance.

    Raises:
        Exception: Uses ``error_type`` when live observations are unavailable.
    """
    if fixture_dir is None:
        raise error_type("live dependency observations are unavailable; pass fixture_dir for offline mode")
    bundle = load_bundle_fn(Path(source_config), Path(fixture_dir))
    proposals: list[Any] = []
    blockers: list[str] = []

    _collect_source_release_proposals(bundle, blockers, proposals, version_gt_fn, release_proposal_fn)
    _collect_overlay_proposals(
        bundle,
        blockers,
        proposals,
        version_gt_fn,
        stale_overlay_proposal_fn,
        overlay_superseded_proposal_fn,
    )

    proposals.extend(
        benchmark_proposal_fn(signal)
        for signal in bundle.benchmarks
        if (
            signal.current_hash != signal.baseline_hash
            or signal.drift_percent >= signal.threshold_percent
            or signal.representative_task_count <= 0
        )
    )

    proposals.extend(
        health_proposal_fn(signal) for signal in bundle.health if signal.status not in {"healthy", "unknown"}
    )

    _collect_capability_proposals(bundle, proposals, capability_proposal_fn)

    return _build_report(bundle, blockers, proposals, dedupe_proposals_fn, report_type, status_type)


def _build_report(
    bundle: Any,
    blockers: list[str],
    proposals: list[Any],
    dedupe_proposals_fn: Any,
    report_type: Any,
    status_type: Any,
) -> Any:
    status = status_type.BLOCKED if blockers else status_type.READY
    return report_type(
        status=status,
        proposals=tuple(dedupe_proposals_fn(proposals)),
        blocked_reasons=tuple(blockers),
        source_names=tuple(source.name for source in bundle.sources),
    )


def _collect_source_release_proposals(
    bundle: Any,
    blockers: list[str],
    proposals: list[Any],
    version_gt_fn: Any,
    release_proposal_fn: Any,
) -> None:
    for source in bundle.sources:
        installed = bundle.installed.get(source.name)
        release = bundle.releases.get(source.name)
        if installed is None:
            blockers.append(f"{source.name}: installed version observation missing")
            continue
        if release is None:
            blockers.append(f"{source.name}: upstream release observation missing")
            continue
        if version_gt_fn(release.version, installed.version):
            proposals.append(release_proposal_fn(source, installed, release))


def _collect_overlay_proposals(
    bundle: Any,
    blockers: list[str],
    proposals: list[Any],
    version_gt_fn: Any,
    stale_overlay_proposal_fn: Any,
    overlay_superseded_proposal_fn: Any,
) -> None:
    for overlay in bundle.overlays:
        if overlay.retired:
            continue
        if overlay.rebase_status != "clean":
            reason = (
                f"{overlay.dependency}: overlay {overlay.overlay_id} requires clean rebase or retirement "
                f"before refresh; status={overlay.rebase_status}"
            )
            blockers.append(reason)
            proposals.append(stale_overlay_proposal_fn(overlay, reason))
            continue
        release = bundle.releases.get(overlay.dependency)
        if release and version_gt_fn(release.version, overlay.last_checked_upstream_version):
            proposals.append(overlay_superseded_proposal_fn(overlay, release))


def _collect_capability_proposals(bundle: Any, proposals: list[Any], capability_proposal_fn: Any) -> None:
    for signal in bundle.capabilities:
        release = bundle.releases.get(signal.dependency)
        if release and release.version == signal.upstream_version:
            proposals.append(capability_proposal_fn(signal))


def report_to_dict_impl(report: Any) -> dict[str, Any]:
    """Serialize a dependency intelligence report to plain data."""
    return {
        "status": report.status.value,
        "blocked_reasons": list(report.blocked_reasons),
        "read_only_surfaces": list(report.read_only_surfaces),
        "mutation_forbidden": list(report.mutation_forbidden),
        "source_names": list(report.source_names),
        "proposals": [
            {
                "proposal_id": proposal.proposal_id,
                "kind": proposal.kind.value,
                "dependency": proposal.dependency,
                "title": proposal.title,
                "rationale": proposal.rationale,
                "suggested_action": proposal.suggested_action,
                "evidence": list(proposal.evidence),
                "blocked_by": list(proposal.blocked_by),
                "would_mutate": list(proposal.would_mutate),
            }
            for proposal in report.proposals
        ],
    }
