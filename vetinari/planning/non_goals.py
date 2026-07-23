"""Deterministic non-goal matching for execution plans."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from vetinari.agents.contracts import ExecutionPlan, OutcomeSignal
from vetinari.learning.atomic_writers import _write_text_atomic
from vetinari.planning.review_outcome import (
    OverrideAppeal,
    PlanDecision,
    PlanReviewOutcome,
    RefusalReason,
    utc_now_iso,
)
from vetinari.receipts.record import WorkReceipt, WorkReceiptKind
from vetinari.receipts.store import WorkReceiptStore
from vetinari.types import AgentType, EvidenceBasis
from vetinari.workbench.session_kernel import canonicalize_id

HUMAN_ATTESTED_PREFIXES = ("human:", "user:", "maintainer:")


def _canonicalize_project_id(value: str | None) -> str:
    return canonicalize_id(value, field_name="project_id")


def _assert_within_root(path: Path, *, root: Path) -> Path:
    root_path = root.resolve()
    candidate = path.resolve()
    if not candidate.is_relative_to(root_path):
        raise ValueError("project path escapes outputs/projects root")
    return candidate


@dataclass(frozen=True, slots=True)
class MatchRule:
    """Structured deterministic match criteria for a non-goal."""

    keyword_any: list[str] = field(default_factory=list)
    keyword_all: list[str] = field(default_factory=list)
    forbidden_paths: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class NonGoal:
    """A project-scoped statement of what the project should not pursue."""

    id: str
    project_id: str
    text: str
    added_by: str
    added_at_utc: str = field(default_factory=utc_now_iso)
    match_rules: list[MatchRule] = field(default_factory=list)
    hard_refuse: bool = False

    def __post_init__(self) -> None:
        if not self.text.strip():
            raise ValueError("non-goal text must be non-empty")
        if self.hard_refuse and not self.added_by.startswith(HUMAN_ATTESTED_PREFIXES):
            raise ValueError("hard_refuse non-goals require a human-attested added_by prefix")

    def __repr__(self) -> str:
        return f"NonGoal(id={self.id!r}, project_id={self.project_id!r}, hard_refuse={self.hard_refuse!r})"

    def to_dict(self) -> dict[str, Any]:
        """Serialize the non-goal for JSONL persistence."""
        return {
            "id": self.id,
            "project_id": self.project_id,
            "text": self.text,
            "added_by": self.added_by,
            "added_at_utc": self.added_at_utc,
            "match_rules": [
                {
                    "keyword_any": list(rule.keyword_any),
                    "keyword_all": list(rule.keyword_all),
                    "forbidden_paths": list(rule.forbidden_paths),
                }
                for rule in self.match_rules
            ],
            "hard_refuse": self.hard_refuse,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> NonGoal:
        """Rebuild a non-goal from JSONL data.

        Args:
            raw: Dictionary containing non-goal fields as stored in JSONL.

        Returns:
            A fully-constructed NonGoal instance.
        """
        rules = [MatchRule(**rule) for rule in raw.get("match_rules", [])]
        return cls(
            id=str(raw["id"]),
            project_id=str(raw["project_id"]),
            text=str(raw["text"]),
            added_by=str(raw["added_by"]),
            added_at_utc=str(raw.get("added_at_utc", utc_now_iso())),
            match_rules=rules,
            hard_refuse=bool(raw.get("hard_refuse")),
        )


@dataclass(frozen=True, slots=True)
class MatchEvidence:
    """Concrete deterministic evidence that a plan matched a non-goal."""

    non_goal_id: str
    rule_type: str
    matched_value: str


class NonGoalStore:
    """JSONL store for project non-goals and override appeals."""

    def __init__(self, repo_root: Path | str = ".") -> None:
        self.repo_root = Path(repo_root).resolve()

    def _project_dir(self, project_id: str) -> Path:
        safe_project_id = _canonicalize_project_id(project_id)
        projects_root = (self.repo_root / "outputs" / "projects").resolve()
        return _assert_within_root(projects_root / safe_project_id, root=projects_root)

    def _non_goals_path(self, project_id: str) -> Path:
        return self._project_dir(project_id) / "non_goals.jsonl"

    def _appeals_path(self, project_id: str) -> Path:
        return self._project_dir(project_id) / "override_appeals.jsonl"

    def _plan_feedback_path(self, project_id: str) -> Path:
        return self._project_dir(project_id) / "plan_feedback.jsonl"

    def list_non_goals(self, project_id: str) -> list[NonGoal]:
        """Return all non-goals registered for a project.

        Args:
            project_id: The project whose non-goals to load.

        Returns:
            Ordered list of NonGoal instances, empty if none registered.
        """
        path = self._non_goals_path(project_id)
        if not path.exists():
            return []
        return [NonGoal.from_dict(json.loads(line)) for line in path.read_text(encoding="utf-8").splitlines() if line]

    def add_non_goal(self, non_goal: NonGoal) -> NonGoal:
        """Persist a non-goal, deduplicating by case-folded text.

        Args:
            non_goal: The non-goal to add.

        Returns:
            The stored non-goal (existing record if a duplicate was found).
        """
        existing = self.list_non_goals(non_goal.project_id)
        for item in existing:
            if item.text.casefold() == non_goal.text.casefold():
                return item
        self._append_jsonl(self._non_goals_path(non_goal.project_id), non_goal.to_dict())
        return non_goal

    def record_override_appeal(self, project_id: str, appeal: OverrideAppeal) -> None:
        """Append an override appeal to the project's appeals log.

        Args:
            project_id: The project the appeal belongs to.
            appeal: The override appeal to persist.
        """
        self._append_jsonl(self._appeals_path(project_id), appeal.to_dict())

    def record_plan_feedback(self, project_id: str, payload: dict[str, Any]) -> None:
        """Append structured plan feedback to the project's feedback log.

        Args:
            project_id: The project the feedback belongs to.
            payload: Feedback fields to persist as a single JSONL record.
        """
        row = {
            "plan_id": str(payload["plan_id"]),
            "decision": str(payload["decision"]),
            "reason_code": str(payload["reason_code"]),
            "severity": str(payload.get("severity") or "medium"),
            "free_text": str(payload.get("free_text") or ""),
            "recorded_at_utc": str(payload.get("recorded_at_utc") or utc_now_iso()),
        }
        if payload.get("outcome_id") is not None:
            row["outcome_id"] = str(payload["outcome_id"])
        if payload.get("reviewed_at_utc") is not None:
            row["reviewed_at_utc"] = str(payload["reviewed_at_utc"])
        self._append_jsonl(self._plan_feedback_path(project_id), row)

    def list_plan_feedback(self, project_id: str) -> list[dict[str, Any]]:
        """Return every recorded plan-feedback row for a project.

        Closes the Wave-10 follow-up where ``record_plan_feedback`` had no
        reader. The promotion gate calls this so structured planner-reject
        signals can surface as proposal blockers.

        Args:
            project_id: The project whose feedback log to read.

        Returns:
            One dict per JSONL row, ordered as recorded. Empty list when the
            log file does not yet exist.
        """
        path = self._plan_feedback_path(project_id)
        if not path.exists():
            return []
        rows: list[dict[str, Any]] = []
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                rows.append(json.loads(line))
        return rows

    def list_override_appeals(self, project_id: str) -> list[dict[str, Any]]:
        """Return all override appeals for a project as raw dicts.

        Args:
            project_id: The project whose appeals to load.

        Returns:
            List of raw appeal dicts, empty if none exist.
        """
        path = self._appeals_path(project_id)
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]

    @staticmethod
    def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
        """Append one JSON record to a JSONL file through atomic replacement.

        Args:
            path: Destination JSONL file path; parent dirs are created if absent.
            payload: Record to serialize as a single JSON line.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n"
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
        _write_text_atomic(path, existing + line)


def _plan_summary(plan: ExecutionPlan) -> str:
    parts = [plan.goal, plan.notes]
    parts.extend(task.description for task in plan.tasks)
    return "\n".join(part for part in parts if part)


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
    original = getattr(plan, "original_plan", None)
    raw_scope = getattr(original, "owned_write_scope", None)
    if isinstance(raw_scope, str):
        paths.add(raw_scope.replace("\\", "/"))
    elif isinstance(raw_scope, Iterable):
        paths.update(str(item).replace("\\", "/") for item in raw_scope)
    return paths


def _token_present(summary: str, token: str) -> bool:
    return re.search(rf"(?<![A-Za-z0-9_]){re.escape(token)}(?![A-Za-z0-9_])", summary, re.IGNORECASE) is not None


def matches(plan: ExecutionPlan, non_goal: NonGoal) -> MatchEvidence | None:
    """Return deterministic match evidence for a plan/non-goal pair.

    Args:
        plan: The execution plan to test against the non-goal's rules.
        non_goal: The non-goal containing one or more MatchRule entries.

    Returns:
        MatchEvidence describing the first rule that matched, or None if no
        match was found.
    """
    summary = _plan_summary(plan)
    paths = _plan_paths(plan)
    for rule in non_goal.match_rules:
        for keyword in rule.keyword_any:
            if _token_present(summary, keyword):
                return MatchEvidence(non_goal.id, "keyword_any", keyword)
        if rule.keyword_all and all(_token_present(summary, keyword) for keyword in rule.keyword_all):
            return MatchEvidence(non_goal.id, "keyword_all", ",".join(rule.keyword_all))
        for forbidden in rule.forbidden_paths:
            normalized = forbidden.replace("\\", "/").rstrip("/")
            if any(path == normalized or path.startswith(f"{normalized}/") for path in paths):
                return MatchEvidence(non_goal.id, "forbidden_paths", forbidden)
    return None


def build_non_goal_receipt(project_id: str, outcome: PlanReviewOutcome) -> WorkReceipt:
    """Build the PLAN_ROUND receipt associated with a non-goal match.

    Args:
        project_id: The project the plan belongs to.
        outcome: The review outcome containing decision, citations, and evidence.

    Returns:
        A WorkReceipt with awaiting_user=True and the appropriate refusal reason.
    """
    reason_ids = ", ".join(outcome.citations)
    reason = f"plan matched non-goal: {reason_ids} -- revise or file an override appeal"
    if outcome.decision is PlanDecision.REFUSE:
        reason = f"plan matched hard-refuse non-goal: {reason_ids} -- revise plan scope"
    return WorkReceipt(
        project_id=project_id,
        agent_id="foreman:plan-review",
        agent_type=AgentType.FOREMAN,
        kind=WorkReceiptKind.PLAN_ROUND,
        outcome=outcome.evidence,
        inputs_summary="plan non-goal check",
        outputs_summary=outcome.decision.value,
        awaiting_user=True,
        awaiting_reason=reason,
    )


def _build_non_goal_outcome(
    *,
    matched_ids: list[str],
    hard_refuse: bool,
    override_appeal: OverrideAppeal | None,
    reviewer: Callable[[ExecutionPlan, OverrideAppeal], PlanReviewOutcome] | None,
    plan: ExecutionPlan,
) -> PlanReviewOutcome:
    if hard_refuse:
        return PlanReviewOutcome(
            decision=PlanDecision.REFUSE,
            refusal_reasons=[RefusalReason.NON_GOAL_MATCH],
            citations=matched_ids,
            evidence=OutcomeSignal(
                passed=False,
                basis=EvidenceBasis.TOOL_EVIDENCE,
                issues=("hard-refuse non-goal matched",),
            ),
            override_appeal=override_appeal,
        )

    if override_appeal is not None and override_appeal.has_attested_artifact:
        if reviewer is not None:
            return reviewer(plan, override_appeal)
        # Invariant: has_attested_artifact is True (checked above), so
        # attested_artifact is guaranteed non-None here.
        assert override_appeal.attested_artifact is not None
        return PlanReviewOutcome(
            decision=PlanDecision.APPROVE,
            citations=matched_ids,
            evidence=OutcomeSignal(
                passed=True,
                score=1.0,
                basis=EvidenceBasis.HUMAN_ATTESTED,
                attested_artifacts=(override_appeal.attested_artifact,),
            ),
            override_appeal=override_appeal,
        )

    return PlanReviewOutcome(
        decision=PlanDecision.NEEDS_REVISION,
        refusal_reasons=[RefusalReason.NON_GOAL_MATCH],
        citations=matched_ids,
        evidence=OutcomeSignal(
            passed=False,
            basis=EvidenceBasis.TOOL_EVIDENCE,
            issues=("non-goal matched",),
        ),
        override_appeal=override_appeal,
    )


def check_non_goals(
    plan: ExecutionPlan,
    project_id: str,
    *,
    store: NonGoalStore | None = None,
    non_goals: list[NonGoal] | None = None,
    override_appeal: OverrideAppeal | None = None,
    reviewer: Callable[[ExecutionPlan, OverrideAppeal], PlanReviewOutcome] | None = None,
    receipt_store: WorkReceiptStore | None = None,
) -> PlanReviewOutcome | None:
    """Check a plan against project non-goals before any reviewer LLM call.

    Args:
        plan: Candidate execution plan.
        project_id: Project identifier owning the plan.
        store: Optional non-goal store override.
        non_goals: Optional preloaded non-goals for tests.
        override_appeal: Optional appeal to record and consider.
        reviewer: Optional reviewer used when an appeal has attested evidence.
        receipt_store: Optional receipt store for emitted review receipts.

    Returns:
        PlanReviewOutcome when a non-goal matched, otherwise None.
    """
    active_store = store or NonGoalStore()
    candidates = non_goals if non_goals is not None else active_store.list_non_goals(project_id)
    matched = [(non_goal, evidence) for non_goal in candidates if (evidence := matches(plan, non_goal))]
    if not matched:
        return None

    matched_ids = [non_goal.id for non_goal, _ in matched]
    hard_refuse = any(non_goal.hard_refuse for non_goal, _ in matched)
    if override_appeal is not None:
        active_store.record_override_appeal(project_id, override_appeal)
    outcome = _build_non_goal_outcome(
        matched_ids=matched_ids,
        hard_refuse=hard_refuse,
        override_appeal=override_appeal,
        reviewer=reviewer,
        plan=plan,
    )

    if receipt_store is not None and outcome.decision is not PlanDecision.APPROVE:
        receipt_store.append(build_non_goal_receipt(project_id, outcome))
    return outcome


__all__ = [
    "MatchEvidence",
    "MatchRule",
    "NonGoal",
    "NonGoalStore",
    "build_non_goal_receipt",
    "check_non_goals",
    "matches",
]
