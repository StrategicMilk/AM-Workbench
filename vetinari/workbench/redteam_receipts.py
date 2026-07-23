"""Private receipt helpers for the Workbench red-team adapter."""

from __future__ import annotations

from typing import Any

from vetinari.agents.contracts import OutcomeSignal, Provenance
from vetinari.receipts.record import WorkReceipt, WorkReceiptKind
from vetinari.types import EvidenceBasis, ShardKind

REDTEAM_PUBLIC_EXPORTS = [
    "RedTeamAdapter",
    "RedTeamAdapterError",
    "RedTeamCase",
    "RedTeamRunResult",
    "RedTeamSuite",
    "load_redteam_suite_from_path",
    "run_redteam_suite",
]


def _emit_case_receipt_impl(
    *,
    project_id: str,
    suite_description: str,
    case: Any,
    eval_id: str,
    case_passed: bool,
    score_failed: int,
    score_total: int,
    receipt_store: Any,
    actor: Any,
    schema_version: str,
    now: str,
    clip: Any,
) -> WorkReceipt:
    receipt = WorkReceipt(
        project_id=project_id,
        agent_id=f"workbench-redteam:{eval_id}",
        agent_type=actor,
        kind=WorkReceiptKind.SPINE_EVENT,
        outcome=OutcomeSignal(
            passed=case_passed,
            score=1.0 if case_passed else 0.0,
            basis=EvidenceBasis.TOOL_EVIDENCE,
            provenance=Provenance(
                source="vetinari.workbench.redteam_adapter",
                timestamp_utc=now,
                tool_name="workbench_redteam_adapter",
                tool_version=schema_version,
            ),
            kind=ShardKind.STANDARD,
        ),
        started_at_utc=now,
        finished_at_utc=now,
        inputs_summary=clip(f"redteam case={case.case_id} kind={case.kind} suite={suite_description}"),
        outputs_summary=clip(f"eval_id={eval_id} passed={case_passed} failed={score_failed}/{score_total}"),
    )
    receipt_store.append(receipt)
    return receipt
