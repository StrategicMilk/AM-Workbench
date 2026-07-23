"""Structured Plan Reviewer integration."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

from vetinari.agents.contracts import OutcomeSignal, ToolEvidence
from vetinari.planning.review_outcome import PlanDecision, PlanReviewOutcome, RefusalReason
from vetinari.types import EvidenceBasis

logger = logging.getLogger(__name__)


PLAN_REVIEWER_SYSTEM_PROMPT = """You are Vetinari's Plan Reviewer.

Return only JSON matching this schema:
{
  "decision": "APPROVE|REFUSE|NEEDS_REVISION",
  "refusal_reasons": ["NON_GOAL_MATCH|DESTRUCTIVE_WITHOUT_GUARD|EVIDENCE_INSUFFICIENT|IFR_UNEXPLORED|SCOPE_EXCEEDS_BUDGET|OTHER"],
  "citations": ["receipt id, claim id, or file path"],
  "ifr_alternative": "string or null",
  "evidence": {
    "passed": true,
    "score": 1.0,
    "basis": "tool_evidence|llm_judgment|human_attested|hybrid|unsupported",
    "tool_evidence": [{"tool_name": "string", "command": "string", "exit_code": 0, "passed": true}]
  }
}

Ideal Final Result framing:
Before refusing a risky plan, attempt to describe the Ideal Final Result:
what outcome would satisfy the user's intent without the specific risky
action? If you propose an IFR alternative, set decision=NEEDS_REVISION; if no alternative is possible, set decision=REFUSE.

Rule 9 deterministic-task boundary:
Do not ask the model to perform deterministic parsing or keyword matching.
Deterministic checks belong in code. Cite concrete receipts, claim ids, or
file paths for any approval.
"""


def _coerce_tool_evidence(raw: Any, *, passed: bool, citations: list[str]) -> tuple[ToolEvidence, ...]:
    if isinstance(raw, list):
        rows: list[ToolEvidence] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            rows.append(
                ToolEvidence(
                    tool_name=str(item.get("tool_name") or "plan-reviewer"),
                    command=str(item.get("command") or item.get("citation") or "reviewer evidence"),
                    exit_code=int(item.get("exit_code", 0 if bool(item.get("passed", passed)) else 1)),
                    stdout_snippet=str(item.get("stdout_snippet", "")),
                    stdout_hash=str(item.get("stdout_hash", "")),
                    passed=bool(item.get("passed", passed)),
                )
            )
        if rows:
            return tuple(rows)
    if citations:
        return (
            ToolEvidence(
                tool_name="plan-reviewer-citation",
                command=citations[0],
                exit_code=0 if passed else 1,
                stdout_snippet="reviewer cited deterministic evidence",
                passed=passed,
            ),
        )
    return ()


def _coerce_evidence(raw: Any, *, citations: list[str]) -> OutcomeSignal:
    if not isinstance(raw, dict):
        return OutcomeSignal()
    try:
        basis = EvidenceBasis(raw.get("basis", EvidenceBasis.UNSUPPORTED.value))
    except ValueError:
        basis = EvidenceBasis.UNSUPPORTED
    passed = bool(raw.get("passed", False))
    return OutcomeSignal(
        passed=passed,
        score=float(raw.get("score", 0.0)),
        basis=basis,
        tool_evidence=_coerce_tool_evidence(raw.get("tool_evidence"), passed=passed, citations=citations),
        issues=tuple(str(issue) for issue in raw.get("issues", ())),
        suggestions=tuple(str(suggestion) for suggestion in raw.get("suggestions", ())),
    )


def parse_outcome(raw: str, *, model_id: str = "") -> PlanReviewOutcome:
    """Parse a structured reviewer response.

    Invalid JSON, missing required fields, or enum mismatches fail closed to a
    default REFUSE/OTHER outcome.

    Args:
        raw: Raw model response text.
        model_id: Optional model identifier for diagnostics.

    Returns:
        Parsed PlanReviewOutcome, or a fail-closed default outcome.

    Raises:
        No exceptions are intentionally raised; malformed responses are logged
        and converted into a refusal outcome.
    """
    try:
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("reviewer response must be a JSON object")
        if "decision" not in payload:
            raise ValueError("reviewer response missing required decision")
        decision = PlanDecision(payload["decision"])
        reasons = [RefusalReason(reason) for reason in payload.get("refusal_reasons", [])]
        citations = [str(citation) for citation in payload.get("citations", [])]
        ifr_raw = payload.get("ifr_alternative")
        ifr_alternative = str(ifr_raw) if ifr_raw is not None else None
        return PlanReviewOutcome(
            decision=decision,
            refusal_reasons=reasons,
            citations=citations,
            ifr_alternative=ifr_alternative,
            evidence=_coerce_evidence(payload.get("evidence"), citations=citations),
        )
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        logger.warning("Plan Reviewer parse failed for model %s: %s", model_id or "<unknown>", exc)
        return PlanReviewOutcome.refuse_default()


class PlanReviewer:
    """Small adapter that keeps LLM invocation separate from deterministic parsing."""

    def __init__(self, llm_call: Callable[[str, str], str], *, model_id: str) -> None:
        self._llm_call = llm_call
        self.model_id = model_id

    def review(self, plan_text: str) -> PlanReviewOutcome:
        """Invoke the reviewer model and parse its structured response.

        Args:
            plan_text: Plan content to review.

        Returns:
            Parsed PlanReviewOutcome.
        """
        raw = self._llm_call(PLAN_REVIEWER_SYSTEM_PROMPT, plan_text)
        return parse_outcome(raw, model_id=self.model_id)


__all__ = ["PLAN_REVIEWER_SYSTEM_PROMPT", "PlanReviewer", "parse_outcome"]
