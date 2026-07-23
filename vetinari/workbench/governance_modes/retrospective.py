"""Advisory-only retrospective governance scans."""

from __future__ import annotations

from collections.abc import Iterable
from copy import deepcopy
from datetime import datetime, timezone
from uuid import uuid4

from vetinari.workbench.policy.verdicts import VerdictValue

from .contracts import (
    GovernanceEnforcementEffect,
    GovernanceMode,
    RetrospectiveFinding,
    RetrospectiveScanInput,
    RetrospectiveScanReport,
)


def run_retrospective_policy_scan(
    snapshots: Iterable[RetrospectiveScanInput],
    *,
    scan_id: str | None = None,
    generated_at_utc: str | None = None,
) -> RetrospectiveScanReport:
    """Replay historical snapshots against candidate policy context without mutation.

    Returns:
        Outcome produced by run_retrospective_policy_scan().

    Raises:
        Exception: Propagates validation or runtime failures from the underlying operation.
    """
    rows = tuple(snapshots)
    before = deepcopy(rows)
    findings = tuple(
        finding
        for index, snapshot in enumerate(rows, start=1)
        if (finding := _finding_for(snapshot, index)) is not None
    )
    if before != rows:
        raise AssertionError("retrospective scan mutated caller snapshots")
    candidate_policy_version = rows[0].candidate_policy_version if rows else "no-candidate-policy"
    candidate_shield_version = rows[0].candidate_shield_version if rows else "no-candidate-shield"
    return RetrospectiveScanReport(
        scan_id=scan_id or f"retrospective-scan-{uuid4().hex}",
        candidate_policy_version=candidate_policy_version,
        candidate_shield_version=candidate_shield_version,
        scanned_trace_refs=tuple(row.trace_ref for row in rows),
        findings=findings,
        blast_radius_summary=_blast_radius_summary(findings, actions_scanned=len(rows)),
        generated_at_utc=generated_at_utc or _utc_now_iso(),
    )


def _finding_for(snapshot: RetrospectiveScanInput, index: int) -> RetrospectiveFinding | None:
    verdict_value = str(snapshot.historical_verdict_payload.get("value", "")).strip()
    candidate_mode = GovernanceMode(snapshot.candidate_mode)
    would_block = candidate_mode is GovernanceMode.STRICT and verdict_value in {
        VerdictValue.BLOCK.value,
        VerdictValue.ESCALATE.value,
    }
    would_warn = would_block or (
        candidate_mode in {GovernanceMode.WARN, GovernanceMode.STRICT}
        and verdict_value in {VerdictValue.WARN.value, VerdictValue.BLOCK.value, VerdictValue.ESCALATE.value}
    )
    if not would_warn:
        return None
    effect = (
        GovernanceEnforcementEffect.WOULD_HAVE_BLOCKED if would_block else GovernanceEnforcementEffect.WOULD_HAVE_WARNED
    )
    verdict_ref = str(
        snapshot.historical_verdict_payload.get("verdict_id")
        or snapshot.historical_verdict_payload.get("verdict_ref")
        or f"historical-verdict:{snapshot.action_ref}"
    )
    recommended = (snapshot.candidate_shield_version,) if would_block else ()
    return RetrospectiveFinding(
        finding_id=f"retrospective-finding-{index}",
        enforcement_effect=effect,
        historical_trace_ref=snapshot.trace_ref,
        historical_action_ref=snapshot.action_ref,
        historical_verdict_ref=verdict_ref,
        historical_receipt_refs=snapshot.receipt_refs,
        historical_policy_version=snapshot.historical_policy_version,
        historical_shield_version=snapshot.historical_shield_version,
        candidate_policy_version=snapshot.candidate_policy_version,
        candidate_shield_version=snapshot.candidate_shield_version,
        candidate_rule_refs=snapshot.candidate_rule_refs or (f"candidate-policy:{snapshot.candidate_policy_version}",),
        would_have_warned=would_warn,
        would_have_blocked=would_block,
        advisory_only=True,
        enforced=False,
        history_mutated=False,
        recommended_shield_packs=recommended,
        likely_false_positive_notes=snapshot.likely_false_positive_notes,
        evidence_refs=(snapshot.trace_ref, snapshot.action_ref, verdict_ref, *snapshot.receipt_refs),
        summary=_summary(snapshot, would_block),
    )


def _blast_radius_summary(findings: tuple[RetrospectiveFinding, ...], *, actions_scanned: int) -> dict[str, int]:
    return {
        "actions_scanned": actions_scanned,
        "would_have_warned": sum(1 for finding in findings if finding.would_have_warned),
        "would_have_blocked": sum(1 for finding in findings if finding.would_have_blocked),
        "history_mutations": 0,
    }


def _summary(snapshot: RetrospectiveScanInput, would_block: bool) -> str:
    preview = "would have blocked" if would_block else "would have warned"
    return (
        f"{snapshot.action_ref} {preview} under candidate policy "
        f"{snapshot.candidate_policy_version} and shield {snapshot.candidate_shield_version}"
    )


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
