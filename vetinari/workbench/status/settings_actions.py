"""Explicit writable Workbench status action boundary."""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from vetinari.agents.contracts import OutcomeSignal, ToolEvidence
from vetinari.receipts.record import WorkReceipt, WorkReceiptKind
from vetinari.receipts.store import WorkReceiptStore
from vetinari.types import AgentType, EvidenceBasis

logger = logging.getLogger(__name__)


class WorkbenchStatusActionState(StrEnum):
    """Writable action result states."""

    APPLIED = "applied"
    BLOCKED = "blocked"
    PROPOSAL_ONLY = "proposal_only"


_MAX_DECISION_AGE_SECONDS = 15 * 60


@dataclass(frozen=True, slots=True)
class WorkbenchStatusActionResult:
    """JSON-safe result for one writable status action attempt."""

    action_id: str
    state: WorkbenchStatusActionState
    project_id: str
    reasons: tuple[str, ...]
    approval_decision_ref: str | None = None
    receipt_id: str | None = None
    proposal_only: bool = False

    @property
    def applied(self) -> bool:
        """Return true only when the callback ran after all gates passed."""
        return self.state is WorkbenchStatusActionState.APPLIED

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe action result."""
        return {
            "action_id": self.action_id,
            "state": self.state.value,
            "project_id": self.project_id,
            "reasons": list(self.reasons),
            "approval_decision_ref": self.approval_decision_ref,
            "receipt_id": self.receipt_id,
            "proposal_only": self.proposal_only,
            "applied": self.applied,
        }

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"WorkbenchStatusActionResult(action_id={self.action_id!r}, state={self.state!r}, project_id={self.project_id!r})"


def run_workbench_status_action(
    *,
    action_id: str,
    project_id: str = "default",
    actor_id: str = "workbench-status-console",
    approval_decision: Any | None = None,
    receipt_store: WorkReceiptStore | None = None,
    callback: Callable[[], Any] | None = None,
) -> WorkbenchStatusActionResult:
    """Run one writable setting action after Approval Chain and receipt gates.

    Returns:
        Outcome produced by run_workbench_status_action().
    """
    clean_action_id = str(action_id).strip()
    clean_project_id = str(project_id or "default").strip() or "default"
    if not clean_action_id:
        return _blocked("", clean_project_id, "action_id_required")
    decision_payload = _decision_payload(approval_decision)
    if decision_payload is None:
        return _blocked(clean_action_id, clean_project_id, "approval_chain_decision_required", proposal_only=True)
    decision_ref = str(decision_payload.get("decision_id") or "").strip()
    if not decision_ref:
        return _blocked(clean_action_id, clean_project_id, "approval_chain_decision_ref_required", proposal_only=True)
    binding_error = _decision_binding_error(decision_payload, clean_project_id, clean_action_id)
    if binding_error:
        return _blocked(clean_action_id, clean_project_id, binding_error, decision_ref, proposal_only=True)
    if not _decision_allows(decision_payload):
        return _blocked(
            clean_action_id, clean_project_id, "approval_chain_did_not_allow", decision_ref, proposal_only=True
        )
    if callback is None:
        return _blocked(
            clean_action_id, clean_project_id, "target_settings_callback_unavailable", decision_ref, proposal_only=True
        )
    try:
        callback()
    except Exception as exc:
        logger.warning("Handled recoverable failure before fallback.", exc_info=True)
        return _blocked(
            clean_action_id,
            clean_project_id,
            f"target_settings_callback_failed:{type(exc).__name__}",
            decision_ref,
        )
    store = receipt_store or WorkReceiptStore()
    receipt = _receipt(clean_project_id, actor_id, clean_action_id, decision_ref)
    try:
        store.append(receipt)
    except Exception as exc:
        logger.warning("Handled recoverable failure before fallback.", exc_info=True)
        return _blocked(clean_action_id, clean_project_id, f"receipt_append_failed:{type(exc).__name__}", decision_ref)
    return WorkbenchStatusActionResult(
        action_id=clean_action_id,
        state=WorkbenchStatusActionState.APPLIED,
        project_id=clean_project_id,
        reasons=("approval-chain-and-receipt-gates-passed",),
        approval_decision_ref=decision_ref,
        receipt_id=receipt.receipt_id,
        proposal_only=False,
    )


def _decision_payload(decision: Any | None) -> dict[str, Any] | None:
    if decision is None:
        return None
    if hasattr(decision, "to_dict"):
        payload = decision.to_dict()
    elif isinstance(decision, Mapping):
        payload = dict(decision)
    else:
        return None
    return {str(key): value for key, value in payload.items()}


def _decision_allows(payload: Mapping[str, Any]) -> bool:
    return bool(payload.get("allowed")) and str(payload.get("outcome", "")) == "allow"


def _decision_binding_error(payload: Mapping[str, Any], project_id: str, action_id: str) -> str:
    issued = _lookup_issued_decision(str(payload.get("decision_id") or ""))
    if issued is None:
        return "approval_chain_decision_untrusted"
    issued_payload = issued.to_dict()
    if str(payload.get("project_id") or "") != project_id or issued_payload["project_id"] != project_id:
        return "approval_chain_project_mismatch"
    if str(payload.get("action_id") or "") != action_id or issued_payload["action_id"] != action_id:
        return "approval_chain_action_mismatch"
    if str(payload.get("action_fingerprint") or "") != issued_payload["action_fingerprint"]:
        return "approval_chain_fingerprint_mismatch"
    if str(payload.get("schema_version") or "") != "1.0":
        return "approval_chain_schema_mismatch"
    if bool(payload.get("allowed")) != bool(issued_payload.get("allowed")):
        return "approval_chain_outcome_mismatch"
    if str(payload.get("outcome") or "") != str(issued_payload.get("outcome") or ""):
        return "approval_chain_outcome_mismatch"
    receipt_payload = payload.get("receipt_payload")
    issued_receipt = issued_payload.get("receipt_payload")
    if not isinstance(receipt_payload, Mapping) or not isinstance(issued_receipt, Mapping):
        return "approval_chain_receipt_payload_required"
    if str(receipt_payload.get("receipt_kind") or "") != "workbench_approval_chain_decision":
        return "approval_chain_receipt_payload_required"
    if str(receipt_payload.get("matched_step") or "") != str(issued_receipt.get("matched_step") or ""):
        return "approval_chain_receipt_mismatch"
    if _decision_is_stale(str(payload.get("decided_at_utc") or "")):
        return "approval_chain_decision_stale"
    return ""


def _lookup_issued_decision(decision_id: str) -> Any | None:
    try:
        from vetinari.workbench.approval_chain import get_workbench_approval_chain

        return get_workbench_approval_chain().lookup_decision(decision_id)
    except Exception:
        logger.warning("Handled recoverable failure before fallback.", exc_info=True)
        return None


def _decision_is_stale(value: str) -> bool:
    decided_at: datetime | None
    try:
        decided_at = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        logger.warning("Handled recoverable failure before fallback.", exc_info=True)
        decided_at = None
    if decided_at is None:
        return True
    age = datetime.now(UTC) - decided_at.astimezone(UTC)
    return age.total_seconds() > _MAX_DECISION_AGE_SECONDS


def _receipt(project_id: str, actor_id: str, action_id: str, decision_ref: str) -> WorkReceipt:
    now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    return WorkReceipt(
        project_id=project_id,
        agent_id=actor_id,
        agent_type=AgentType.WORKBENCH,
        kind=WorkReceiptKind.POLICY_DECISION,
        started_at_utc=now,
        finished_at_utc=now,
        inputs_summary=f"status action {action_id}",
        outputs_summary=f"approval decision {decision_ref}",
        outcome=OutcomeSignal(
            passed=True,
            score=1.0,
            basis=EvidenceBasis.TOOL_EVIDENCE,
            tool_evidence=ToolEvidence(
                tool_name="workbench_status_action",
                command=f"apply {action_id}",
                exit_code=0,
                stdout_snippet=f"approval_decision_ref={decision_ref}",
                passed=True,
            ),
            issues=(),
            suggestions=(),
        ),
        linked_claim_ids=(decision_ref,),
    )


def _blocked(
    action_id: str,
    project_id: str,
    reason: str,
    decision_ref: str | None = None,
    *,
    proposal_only: bool = False,
) -> WorkbenchStatusActionResult:
    return WorkbenchStatusActionResult(
        action_id=action_id,
        state=WorkbenchStatusActionState.PROPOSAL_ONLY if proposal_only else WorkbenchStatusActionState.BLOCKED,
        project_id=project_id,
        reasons=(reason,),
        approval_decision_ref=decision_ref,
        proposal_only=proposal_only,
    )


__all__ = [
    "WorkbenchStatusActionResult",
    "WorkbenchStatusActionState",
    "run_workbench_status_action",
]
