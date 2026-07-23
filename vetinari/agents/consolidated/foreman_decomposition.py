"""Recursive decomposition helpers for the consolidated Foreman gate."""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import threading
from enum import Enum
from typing import Any

from vetinari.agents._self_critique import OperatorAttentionRequired
from vetinari.agents.contracts import DecomposeDecision, OutcomeSignal, Task, ToolEvidence, get_agent_spec
from vetinari.receipts.record import WorkReceipt, WorkReceiptKind
from vetinari.receipts.store import WorkReceiptStore
from vetinari.types import AgentType, EvidenceBasis

logger = logging.getLogger(__name__)

_PLAN_PARENT_MAP: dict[str, str] = {}
# RLock so the atomic check+register helper (ADR-0121) can call
# ``_detect_plan_cycle`` while it already holds the parent-map lock without
# deadlocking, and any future reentrant traversal stays correct.
_PLAN_PARENT_LOCK = threading.RLock()


def _detect_plan_cycle(child_plan_id: str, current_plan_id: str) -> bool:
    """Return whether ``child_plan_id`` is an ancestor of ``current_plan_id``."""
    if not child_plan_id or not current_plan_id:
        return False
    if child_plan_id == current_plan_id:
        return True
    with _PLAN_PARENT_LOCK:
        cursor = current_plan_id
        seen: set[str] = set()
        while cursor and cursor not in seen:
            seen.add(cursor)
            parent = _PLAN_PARENT_MAP.get(cursor)
            if parent == child_plan_id:
                return True
            cursor = parent or ""
    return False


def _check_and_register_child_plan(child_plan_id: str, parent_plan_id: str) -> None:
    """Atomic cycle check + parent-map registration under a single lock (ADR-0121).

    The previous ``_register_child_plan`` implementation released the
    parent-map lock between the cycle check and the write, which let
    two concurrent decomposition threads each see a cycle-free graph
    and then race to insert edges that together form a cycle.  This
    helper takes the lock once, repeats the cycle check inside the
    critical section, and only writes when the inserted edge cannot
    create a cycle.

    Args:
        child_plan_id: Plan that will be made a child of ``parent_plan_id``.
        parent_plan_id: Current parent plan in the recursion stack.

    Raises:
        RecursionError: If inserting the edge would create a direct or
            transitive cycle.  The map is left unchanged.
    """
    if not child_plan_id or not parent_plan_id:
        return
    with _PLAN_PARENT_LOCK:
        if child_plan_id == parent_plan_id or _detect_plan_cycle(child_plan_id, parent_plan_id):
            raise RecursionError(f"plan cycle detected: {child_plan_id} is an ancestor of {parent_plan_id}")
        _PLAN_PARENT_MAP[child_plan_id] = parent_plan_id


def _register_child_plan(child_plan_id: str, parent_plan_id: str) -> None:
    """Backward-compatible alias for :func:`_check_and_register_child_plan`.

    Kept so out-of-tree callers that imported the older non-atomic name
    still get atomic semantics.  New call sites SHOULD use
    :func:`_check_and_register_child_plan` directly.
    """
    _check_and_register_child_plan(child_plan_id, parent_plan_id)


def clear_plan_parent_map() -> None:
    """Clear the in-process recursive plan map for isolated tests."""
    with _PLAN_PARENT_LOCK:
        _PLAN_PARENT_MAP.clear()


def snapshot_plan_parent_map(*, limit: int | None = None) -> tuple[dict[str, str], int]:
    """Return a bounded copy of the recursive plan-parent map.

    Args:
        limit: Maximum number of child-to-parent edges to include in the snapshot,
            or ``None`` to include the full in-process map.

    Returns:
        Pair of ``(snapshot, total)`` where ``snapshot`` is the bounded
        child-plan-to-parent-plan mapping and ``total`` is the unbounded map size.
    """
    with _PLAN_PARENT_LOCK:
        total = len(_PLAN_PARENT_MAP)
        if limit is None or total <= limit:
            return dict(_PLAN_PARENT_MAP), total
        snapshot: dict[str, str] = {}
        for index, (child, parent) in enumerate(_PLAN_PARENT_MAP.items()):
            if index >= limit:
                break
            snapshot[child] = parent
        return snapshot, total


def _agent_depth_cap(task: Task) -> tuple[AgentType, int]:
    raw_agent = task.assigned_agent.value if isinstance(task.assigned_agent, Enum) else task.assigned_agent
    agent_type = task.assigned_agent if isinstance(task.assigned_agent, AgentType) else AgentType(raw_agent)
    spec = get_agent_spec(agent_type)
    cap = spec.max_delegation_depth if spec is not None else 0
    return agent_type, cap


def _remaining_delegations(delegation_budget: Any, task: Task) -> int:
    if delegation_budget is None:
        return 1
    remaining = getattr(delegation_budget, "remaining", None)
    if callable(remaining):
        return int(remaining(task.id))
    if hasattr(delegation_budget, "remaining_dispatches"):
        return int(delegation_budget.remaining_dispatches)
    return 1


def _would_violate_plan_graph(plan_graph: Any, task: Task) -> bool:
    if plan_graph is None:
        return False
    checker = getattr(plan_graph, "would_decomposition_violate_invariants", None)
    if callable(checker):
        return bool(checker(task))
    from vetinari.planning.plan_graph import MAX_PLAN_WIDTH

    nodes = getattr(plan_graph, "nodes", set())
    return len(nodes) >= MAX_PLAN_WIDTH


def _extract_margin(logprobs: Any) -> float | None:
    if not logprobs:
        return None
    if isinstance(logprobs, dict):
        values = sorted((float(v) for v in logprobs.values()), reverse=True)
    else:
        values = sorted((float(v) for v in logprobs), reverse=True)
    if len(values) < 2:
        return None
    return round(values[0] - values[1], 3)


def _emit_decompose_decision_receipt(self: Any, task: Task, decision: DecomposeDecision) -> None:
    store = getattr(self, "_receipt_store", None) or getattr(self, "receipt_store", None)
    if store is None:
        store = WorkReceiptStore()
        with contextlib.suppress(Exception):
            self._receipt_store = store
    output_payload = {
        "action": decision.action,
        "reason": decision.reason,
        "confidence": decision.confidence,
        "model_id": decision.model_id,
    }
    output_hash = hashlib.sha256(json.dumps(output_payload, sort_keys=True, default=str).encode("utf-8")).hexdigest()
    basis = EvidenceBasis.LLM_JUDGMENT if decision.model_id else EvidenceBasis.TOOL_EVIDENCE
    tool_evidence = ()
    if basis is EvidenceBasis.TOOL_EVIDENCE:
        tool_evidence = (
            ToolEvidence(
                tool_name="foreman_decomposition",
                command=f"judge_decomposability task_id={task.id}",
                exit_code=0 if decision.action != "escalate" else 1,
                stdout_snippet=f"action={decision.action};confidence={decision.confidence};reason={decision.reason}",
                stdout_hash=output_hash,
                passed=decision.action != "escalate",
            ),
        )
    receipt = WorkReceipt(
        project_id=task.metadata.get("plan_id", "decompose") if isinstance(task.metadata, dict) else "decompose",
        agent_id="foreman:decompose-decision",
        agent_type=AgentType.FOREMAN,
        kind=WorkReceiptKind.PLAN_ROUND,
        outcome=OutcomeSignal(
            passed=decision.action != "escalate",
            basis=basis,
            score=decision.confidence,
            tool_evidence=tool_evidence,
            issues=(decision.reason,),
            provenance=decision.provenance,
        ),
        inputs_summary=f"decompose decision for {task.id}"[:200],
        outputs_summary=json.dumps(
            output_payload,
            sort_keys=True,
        )[:200],
        awaiting_user=decision.action == "escalate",
        awaiting_reason=decision.reason if decision.action == "escalate" else None,
    )
    store.append(receipt)


def _invoke_decompose_decision_model(
    self: Any, task: Task, spec_frame: Any, plan_graph: Any, delegation_budget: Any
) -> dict[str, Any]:
    hook = getattr(self, "_decompose_decision_model", None)
    if callable(hook):
        return dict(hook(task=task, spec_frame=spec_frame, plan_graph=plan_graph, delegation_budget=delegation_budget))
    infer = getattr(self, "_infer_json", None)
    if callable(infer):
        prompt = (
            "Choose one action: execute_here, decompose_further, escalate.\n"
            f"Task: {task.description}\n"
            f"SpecFrame: {spec_frame!r}\n"
            f"PlanGraph nodes: {len(getattr(plan_graph, 'nodes', ())) if plan_graph is not None else 0}\n"
            f"Delegation remaining: {_remaining_delegations(delegation_budget, task)}"
        )
        return dict(infer(prompt))
    raise OperatorAttentionRequired("no model decision provider for judge_decomposability")


def _foreman_judge_decomposability(
    self: Any,
    task: Task,
    plan_graph: Any = None,
    delegation_budget: Any = None,
    spec_frame: Any = None,
    *,
    recursive_depth: int,
) -> DecomposeDecision:
    """Judge recursive decomposition with deterministic gates before models."""
    agent_type, depth_cap = _agent_depth_cap(task)
    if recursive_depth >= depth_cap:
        decision = DecomposeDecision(
            action="escalate",
            reason=f"depth_cap_reached:{agent_type.value.lower()}:depth={recursive_depth}",
            confidence=1.0,
        )
        _emit_decompose_decision_receipt(self, task, decision)
        return decision
    if _would_violate_plan_graph(plan_graph, task):
        decision = DecomposeDecision(
            action="execute_here",
            reason="plan_graph_invariant_breach:would_exceed_width_or_depth",
            confidence=1.0,
        )
        _emit_decompose_decision_receipt(self, task, decision)
        return decision
    if _remaining_delegations(delegation_budget, task) <= 0:
        decision = DecomposeDecision(
            action="execute_here",
            reason="delegation_budget_exhausted",
            confidence=1.0,
        )
        _emit_decompose_decision_receipt(self, task, decision)
        return decision

    raw = _invoke_decompose_decision_model(self, task, spec_frame, plan_graph, delegation_budget)
    model_id = str(raw.get("model_id") or "")
    margin = _extract_margin(raw.get("logprobs"))
    if margin is None:
        decision = DecomposeDecision(
            action="escalate", reason="no_confidence_signal", confidence=0.0, model_id=model_id
        )
    elif margin < 0.15:
        decision = DecomposeDecision(
            action="escalate",
            reason=f"low_confidence_margin:{margin:.3f}<0.15",
            confidence=margin,
            model_id=model_id,
        )
    else:
        suggested_agent_raw = raw.get("suggested_agent")
        suggested_agent = AgentType(suggested_agent_raw) if suggested_agent_raw else None
        decision = DecomposeDecision(
            action=raw.get("action", "escalate"),
            reason=str(raw.get("reason") or "model_decision"),
            suggested_agent=suggested_agent,
            confidence=margin,
            model_id=model_id,
        )
    _emit_decompose_decision_receipt(self, task, decision)
    return decision
