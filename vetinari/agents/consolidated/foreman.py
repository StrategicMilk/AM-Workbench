"""Foreman dispatch gate for structured plan-review outcomes."""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

import yaml

from vetinari.agents._self_critique import CritiqueResult, OperatorAttentionRequired, run_self_critique
from vetinari.agents.consolidated.foreman_decomposition import (
    _PLAN_PARENT_LOCK,
    _PLAN_PARENT_MAP,
    _check_and_register_child_plan,
    _detect_plan_cycle,
    _foreman_judge_decomposability,
    _register_child_plan,
    clear_plan_parent_map,
)
from vetinari.agents.contracts import ExecutionPlan, OutcomeSignal, Task
from vetinari.intake import IntakeParser, RequestFrame
from vetinari.planning.context_bundle import ContextBundleItem, ContextBundleResolver
from vetinari.planning.review_outcome import PlanDecision, PlanReviewOutcome
from vetinari.receipts.record import WorkReceipt, WorkReceiptKind
from vetinari.receipts.store import WorkReceiptStore
from vetinari.types import AgentType, EvidenceBasis, TaskKind

# Touch the imported symbol so static analyzers do not strip the
# re-export — it has no in-file caller but is part of the foreman
# module's public test surface (ADR-0121).
_check_and_register_child_plan = _check_and_register_child_plan

logger = logging.getLogger(__name__)


_TELEMETRY_LOCK = threading.Lock()
_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_DEFAULT_CRITIQUE_TELEMETRY_PATH = _PROJECT_ROOT / "outputs" / "kaizen" / "critique_pass_rates.jsonl"
DEFAULT_ROUTING_MANIFEST_PATH = Path(__file__).resolve().parents[2] / "config" / "runtime" / "agent-routing-guards.yaml"

_JUDGMENT_INSTALLED = False


@dataclass(frozen=True, slots=True)
class DispatchGateResult:
    """Decision produced before Worker dispatch."""

    dispatched: bool
    reason: str = ""
    receipt: WorkReceipt | None = None
    worker_results: list[Any] = field(default_factory=list)

    def __repr__(self) -> str:
        return f"DispatchGateResult(dispatched={self.dispatched!r}, reason={self.reason!r})"


def annotate_task_kind(task: Task) -> TaskKind:
    """Classify a task into the scaffold-then-fill pass.

        The classifier is deterministic-first: reserved verbs at the beginning of
        the task description win, model-emitted metadata is read only when the
        deterministic gate is ambiguous, and IMPLEMENTATION is the conservative
        default.

    Returns:
        TaskKind value produced by annotate_task_kind().
    """
    scaffold_verbs = ("scaffold", "skeleton", "stub", "bootstrap")
    verification_verbs = ("test", "verify", "check", "assert", "validate")
    # Only lowercase the prefix we actually compare against (longest verb is
    # "bootstrap" at 9 chars; round up to 16 for word-boundary slack) so a
    # multi-KB task description does not trigger a full-string allocation
    # here.  Slicing a ``str`` subclass returns a plain ``str``, so the
    # ``.lower()`` call below cannot re-enter the subclass's blocked
    # ``.lower()`` implementation in the foreman dispatch hot-path test.
    stripped = (task.description or "").lstrip()
    description_prefix = stripped[:16].lower()
    if description_prefix.startswith(scaffold_verbs):
        return TaskKind.SCAFFOLD
    if description_prefix.startswith(verification_verbs):
        return TaskKind.VERIFICATION

    metadata = task.metadata if isinstance(task.metadata, dict) else {}
    raw_kind = metadata.get("kind")
    if raw_kind:
        try:
            raw_value = raw_kind.value if isinstance(raw_kind, Enum) else raw_kind
            return raw_kind if isinstance(raw_kind, TaskKind) else TaskKind(raw_value)
        except ValueError:
            logger.warning("Invalid task kind metadata %r for task %s; defaulting to implementation", raw_kind, task.id)

    decision = metadata.get("decompose_decision")
    if isinstance(decision, dict):
        suggested_agent = decision.get("suggested_agent")
    else:
        suggested_agent = getattr(decision, "suggested_agent", None)
    suggested_agent_value = getattr(suggested_agent, "value", suggested_agent)
    if suggested_agent_value == AgentType.INSPECTOR.value:
        return TaskKind.VERIFICATION

    return TaskKind.IMPLEMENTATION


def install_foreman_judgment() -> type[Any]:
    """Install judge_decomposability on the runtime planner ForemanAgent.

    Returns:
        type[Any] value produced by install_foreman_judgment().
    """
    global _JUDGMENT_INSTALLED
    from vetinari.agents.planner_agent import ForemanAgent as PlannerForemanAgent

    if not hasattr(PlannerForemanAgent, "judge_decomposability"):
        PlannerForemanAgent.judge_decomposability = _foreman_judge_decomposability
    _JUDGMENT_INSTALLED = True
    return PlannerForemanAgent


def __getattr__(name: str) -> Any:
    if name == "ForemanAgent":
        return install_foreman_judgment()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _plan_paths(plan: ExecutionPlan) -> set[str]:
    paths: set[str] = set()
    for task in plan.tasks:
        paths.update(value.replace("\\", "/") for value in task.outputs)
        for key in ("owned_write_scope", "paths", "files"):
            raw = task.metadata.get(key)
            if isinstance(raw, str):
                paths.add(raw.replace("\\", "/"))
            elif isinstance(raw, Iterable):
                paths.update(str(item).replace("\\", "/") for item in raw)
    return paths


def _load_routing_manifest(path: Path | str) -> dict[str, Any]:
    manifest_path = Path(path)
    if not manifest_path.exists():
        raise RuntimeError(f"routing guard manifest is unavailable: {manifest_path}")
    try:
        with manifest_path.open("r", encoding="utf-8") as handle:
            payload = yaml.safe_load(handle)
    except (OSError, yaml.YAMLError) as exc:
        raise RuntimeError(f"routing guard manifest is unreadable: {manifest_path}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"routing guard manifest must be a mapping: {manifest_path}")
    for manifest_field in ("protected_files", "destructive_ownership"):
        values = payload.get(manifest_field)
        if (
            not isinstance(values, list)
            or (manifest_field == "protected_files" and not values)
            or any(not isinstance(item, str) or not item.strip() for item in values)
        ):
            raise RuntimeError(f"routing guard manifest field {manifest_field!r} must be a valid string list")
    return payload


def _guard_prefix(value: str) -> str:
    """Normalize an exact/subtree guard, including a terminal ``/**``."""
    guard = value.strip().replace("\\", "/").rstrip("/")
    if guard.endswith("/**"):
        guard = guard[:-3].rstrip("/")
    if not guard or any(character in guard for character in "*?[]"):
        raise RuntimeError(f"routing guard manifest path uses an unsupported pattern: {value!r}")
    if guard.startswith("/") or any(component in {"", ".", ".."} for component in guard.split("/")):
        raise RuntimeError(f"routing guard manifest path is not repository-relative: {value!r}")
    return guard


def plan_requires_tool_evidence(
    plan: ExecutionPlan,
    *,
    routing_manifest_path: Path | str = DEFAULT_ROUTING_MANIFEST_PATH,
    protected_files: Iterable[str] | None = None,
    destructive_ownership: Iterable[str] | None = None,
) -> bool:
    """Return whether a plan touches high-accuracy or protected scope.

    Args:
        plan: The execution plan to inspect.
        routing_manifest_path: Path to the agent-routing YAML manifest that
            defines protected_files and destructive_ownership lists.
        protected_files: Override the manifest's protected_files list.
        destructive_ownership: Override the manifest's destructive_ownership list.

    Returns:
        True when any task carries requires_tool_evidence metadata or when any
        plan path matches a guarded prefix from the manifest.
    """
    if any(task.metadata.get("requires_tool_evidence") for task in plan.tasks):
        return True
    manifest = _load_routing_manifest(routing_manifest_path)
    protected = list(protected_files if protected_files is not None else manifest.get("protected_files", []))
    destructive = list(
        destructive_ownership if destructive_ownership is not None else manifest.get("destructive_ownership", [])
    )
    guarded = [_guard_prefix(item) for item in protected + destructive]
    plan_paths = _plan_paths(plan)
    return any(path == guard or path.startswith(f"{guard}/") for path in plan_paths for guard in guarded)


def mark_requires_tool_evidence(
    plan: ExecutionPlan,
    *,
    routing_manifest_path: Path | str = DEFAULT_ROUTING_MANIFEST_PATH,
    request_frame: RequestFrame | None = None,
) -> bool:
    """Flag each task in a high-accuracy plan as requiring tool evidence.

    Args:
        plan: The execution plan whose tasks will be flagged if the plan
            touches protected scope.
        routing_manifest_path: Path to the agent-routing YAML manifest.
        request_frame: Optional structured intake contract already produced by
            Foreman's intake phase. When omitted, Foreman parses ``plan.goal``.

    Returns:
        True when the plan was flagged (touches protected scope), False otherwise.
    """
    request_frame = request_frame or build_request_frame_for_planning(plan.goal)
    if request_frame.destructive_intent:
        for task in plan.tasks:
            task.metadata["destructive_intent"] = True
            task.metadata["protected_mutation_intent_required"] = True
    flagged = plan_requires_tool_evidence(plan, routing_manifest_path=routing_manifest_path)
    if flagged:
        for task in plan.tasks:
            task.metadata["requires_tool_evidence"] = True
    return flagged


def build_request_frame_for_planning(raw_prompt: str, persona_name: str | None = None) -> RequestFrame:
    """Parse Foreman's raw goal string before plan generation starts.

    Args:
        raw_prompt: Raw user goal accepted by the Foreman planning entry point.
        persona_name: Optional persona selected before planning.

    Returns:
        RequestFrame produced by the deterministic intake parser.
    """
    request_frame = IntakeParser().parse(raw_prompt, persona_name)
    logger.debug("Foreman intake resolved RequestFrame before planning: %r", request_frame)
    if request_frame.destructive_intent:
        logger.debug(
            "Foreman intake marked destructive intent for protected mutation guard metadata: %r",
            request_frame,
        )
    return request_frame


def _refusal_reason(outcome: PlanReviewOutcome, *, requires_tool_evidence: bool) -> str:
    if outcome.decision is not PlanDecision.APPROVE:
        parts = [reason.value for reason in outcome.refusal_reasons] or [outcome.decision.value]
        reason = "plan reviewer refused -- " + ", ".join(parts)
        if outcome.ifr_alternative:
            reason = f"{reason}; IFR alternative: {outcome.ifr_alternative}"
        return reason
    if outcome.evidence.basis is EvidenceBasis.UNSUPPORTED:
        return "plan reviewer approval lacks required evidence basis"
    if requires_tool_evidence and outcome.evidence.basis is EvidenceBasis.LLM_JUDGMENT:
        return "plan reviewer approval has only LLM judgment on high-accuracy plan"
    return ""


def build_dispatch_refusal_receipt(
    plan: ExecutionPlan,
    outcome: PlanReviewOutcome,
    *,
    requires_tool_evidence: bool,
) -> WorkReceipt:
    """Build a Foreman PLAN_ROUND receipt for a blocked dispatch.

    Args:
        plan: The execution plan that was blocked.
        outcome: The review outcome containing the refusal decision and evidence.
        requires_tool_evidence: Whether this plan requires tool-backed evidence.

    Returns:
        A WorkReceipt with awaiting_user=True capturing the refusal reason.
    """
    reason = _refusal_reason(outcome, requires_tool_evidence=requires_tool_evidence)
    return WorkReceipt(
        project_id=plan.plan_id,
        agent_id="foreman:dispatch-gate",
        agent_type=AgentType.FOREMAN,
        kind=WorkReceiptKind.PLAN_ROUND,
        outcome=OutcomeSignal(
            passed=False,
            basis=outcome.evidence.basis,
            issues=(reason,),
        ),
        inputs_summary="plan dispatch gate",
        outputs_summary=outcome.decision.value,
        awaiting_user=True,
        awaiting_reason=reason,
    )


def evaluate_dispatch_gate(
    plan: ExecutionPlan,
    outcome: PlanReviewOutcome,
    *,
    requires_tool_evidence: bool | None = None,
) -> DispatchGateResult:
    """Evaluate whether Worker dispatch is allowed given a review outcome.

    Args:
        plan: The execution plan being evaluated.
        outcome: The plan-review outcome to validate.
        requires_tool_evidence: Override for the tool-evidence requirement
            check; computed from the plan when None.

    Returns:
        DispatchGateResult with dispatched=True when dispatch is permitted, or
        dispatched=False with a blocking reason and WorkReceipt when blocked.
    """
    needs_tool = plan_requires_tool_evidence(plan) if requires_tool_evidence is None else requires_tool_evidence
    reason = _refusal_reason(outcome, requires_tool_evidence=needs_tool)
    if reason:
        receipt = build_dispatch_refusal_receipt(plan, outcome, requires_tool_evidence=needs_tool)
        return DispatchGateResult(dispatched=False, reason=reason, receipt=receipt)
    return DispatchGateResult(dispatched=True)


def dispatch_plan_after_review(
    plan: ExecutionPlan,
    outcome: PlanReviewOutcome,
    worker_dispatch: Callable[[Any], Any],
    *,
    receipt_store: WorkReceiptStore | None = None,
    requires_tool_evidence: bool | None = None,
) -> DispatchGateResult:
    """Dispatch Worker tasks only after structured plan-review approval.

    Args:
        plan: The execution plan whose tasks will be dispatched on approval.
        outcome: The plan-review outcome gating dispatch.
        worker_dispatch: Callable invoked once per task when dispatch is allowed.
        receipt_store: Optional store where refusal receipts are persisted.
        requires_tool_evidence: Override for the tool-evidence requirement
            check; computed from the plan when None.

    Returns:
        DispatchGateResult with dispatched=True and populated worker_results on
        success, or dispatched=False with a blocking reason when refused.
    """
    gate = evaluate_dispatch_gate(plan, outcome, requires_tool_evidence=requires_tool_evidence)
    if not gate.dispatched:
        if receipt_store is not None and gate.receipt is not None:
            receipt_store.append(gate.receipt)
        return gate
    for task in plan.tasks:
        task.metadata["kind"] = annotate_task_kind(task).value
    return DispatchGateResult(
        dispatched=True,
        worker_results=[_dispatch_task_after_shard_critique(task, worker_dispatch) for task in plan.tasks],
    )


def _dispatch_task_after_shard_critique(task: Task, worker_dispatch: Callable[[Any], Any]) -> Any:
    shard_path = _task_shard_path(task)
    if shard_path is None:
        return worker_dispatch(task)
    return dispatch_shard_after_self_critique(shard_path, lambda _path: worker_dispatch(task))


def _task_shard_path(task: Task) -> Path | None:
    candidate_values: list[Any] = []
    candidate_values.extend(task.outputs)
    candidate_values.extend(
        task.metadata.get(key) for key in ("shard_path", "shard", "path", "paths", "files", "owned_write_scope")
    )
    for value in _flatten_candidate_values(candidate_values):
        path = Path(str(value))
        if path.suffix.lower() == ".md" and path.name.startswith("SHARD-") and path.exists():
            return path
    return None


def _flatten_candidate_values(values: Iterable[Any]) -> list[Any]:
    flattened: list[Any] = []
    for value in values:
        if value is None:
            continue
        if isinstance(value, str):
            flattened.append(value)
        elif isinstance(value, Iterable):
            flattened.extend(value)
        else:
            flattened.append(value)
    return flattened


def _record_critique_telemetry(
    shard_path: str | Path,
    result: CritiqueResult,
    *,
    telemetry_path: str | Path = _DEFAULT_CRITIQUE_TELEMETRY_PATH,
) -> None:
    """Append one Foreman self-critique pass-rate record under a thread lock."""
    out_path = Path(telemetry_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "shard_path": str(shard_path),
        "kind": result.kind,
        "passed": result.passed,
        "attempt": result.attempt,
        "failed_checks": list(result.failed_checks),
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
    }
    line = json.dumps(record, ensure_ascii=False) + "\n"
    with _TELEMETRY_LOCK, out_path.open("a", encoding="utf-8") as handle:
        handle.write(line)


def _resolve_shard_context_bundle(
    shard_path: Path,
    resolver: ContextBundleResolver | None = None,
) -> int:
    """Pre-resolve ``context_bundle:`` items declared in shard frontmatter (Q-M4).

    Reads the shard, extracts each ``context_bundle`` entry, and asks the
    resolver to fetch + cache the ``sem context`` excerpt. Failures are
    logged at DEBUG and never block dispatch — context bundles are an
    optimisation, not a contract.

    Returns the count of items that were submitted to the resolver (mainly
    for telemetry / tests).
    """
    from vetinari.agents._self_critique import _split_frontmatter

    try:
        text = shard_path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("Cannot read shard %s for context-bundle resolution: %s", shard_path, exc)
        return 0
    frontmatter, _body = _split_frontmatter(text)
    raw_items = frontmatter.get("context_bundle")
    if not isinstance(raw_items, list) or not raw_items:
        return 0

    items: list[ContextBundleItem] = []
    for entry in raw_items:
        if not isinstance(entry, dict):
            continue
        entity = str(entry.get("entity") or "").strip()
        file_path = str(entry.get("file") or "").strip()
        try:
            budget = int(entry.get("budget_tokens") or 4000)
        except (TypeError, ValueError) as exc:
            logger.debug("Skipping context_bundle entry with invalid budget in %s: %s", shard_path, exc)
            continue
        if not entity or not file_path:
            continue
        try:
            items.append(ContextBundleItem(entity=entity, file=file_path, budget_tokens=budget))
        except ValueError as exc:
            logger.debug("Skipping invalid context_bundle entry in %s: %s", shard_path, exc)
            continue

    if not items:
        return 0

    active_resolver = resolver if resolver is not None else ContextBundleResolver()
    try:
        active_resolver.resolve_all(items)
    except Exception:
        # Pre-resolution is fire-and-forget. Workers can re-derive on miss.
        logger.warning("Context-bundle pre-resolution failed for %s", shard_path, exc_info=True)
    return len(items)


def dispatch_shard_after_self_critique(
    shard_path: str | Path,
    shard_dispatch: Callable[[Path], Any],
    *,
    telemetry_path: str | Path = _DEFAULT_CRITIQUE_TELEMETRY_PATH,
    context_bundle_resolver: ContextBundleResolver | None = None,
) -> Any:
    """Run Foreman self-critique before dispatching a generated shard.

    On a passing critique, also pre-resolves any ``context_bundle:`` items
    declared in the shard frontmatter (Q-M4 wiring) so the worker hits cache
    instead of re-deriving ``sem context`` excerpts.

    Args:
        shard_path: Path to the generated shard markdown file.
        shard_dispatch: Callable that dispatches a validated shard path.
        telemetry_path: JSONL path for critique pass-rate records.
        context_bundle_resolver: Optional resolver override for tests.

    Returns:
        Whatever ``shard_dispatch`` returns for a passing shard.

    Raises:
        OperatorAttentionRequired: If both critique attempts fail.
    """
    path = Path(shard_path)
    last_result: CritiqueResult | None = None
    for attempt in (1, 2):
        result = run_self_critique(path, attempt=attempt)
        _record_critique_telemetry(path, result, telemetry_path=telemetry_path)
        if result.passed:
            _resolve_shard_context_bundle(path, resolver=context_bundle_resolver)
            return shard_dispatch(path)
        last_result = result
    failed = last_result.failed_checks if last_result is not None else []
    raise OperatorAttentionRequired(f"Shard {path} failed self-critique on two attempts. Failed checks: {failed}")


__all__ = [
    "_PLAN_PARENT_LOCK",
    "_PLAN_PARENT_MAP",
    "DispatchGateResult",
    "_check_and_register_child_plan",
    "_detect_plan_cycle",
    "_foreman_judge_decomposability",
    "_register_child_plan",
    "annotate_task_kind",
    "build_dispatch_refusal_receipt",
    "build_request_frame_for_planning",
    "clear_plan_parent_map",
    "dispatch_plan_after_review",
    "dispatch_shard_after_self_critique",
    "evaluate_dispatch_gate",
    "mark_requires_tool_evidence",
    "plan_requires_tool_evidence",
]
