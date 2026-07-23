"""Inspector OutcomeSignal aggregation.

Combines multiple OutcomeSignals from sub-checks (tool wrappers, LLM verifiers,
and other inspectors) into a single consolidated verdict so downstream
consumers see one signal with explicit basis and provenance, not a bag of
heterogeneous results.

This is step 4 of the pipeline: Intake → Planning → Execution → **Quality Gate** → Assembly.
"""

from __future__ import annotations

from datetime import datetime, timezone

from vetinari.agents.contracts import OutcomeSignal, Provenance
from vetinari.types import EvidenceBasis

_AGGREGATOR_SOURCE = "vetinari.skills.inspector_outcome.aggregate_outcome_signals"


def aggregate_outcome_signals(signals: list[OutcomeSignal]) -> OutcomeSignal:
    """Aggregate a list of OutcomeSignals into a single consolidated verdict.

    Aggregation rules (in priority order):
    - Empty list -> ``passed=False, basis=UNSUPPORTED`` (fail-closed, Rule 2).
    - Any UNSUPPORTED on a high-accuracy path -> aggregate is
      ``passed=False, basis=UNSUPPORTED``.
    - All tool-evidence -> ``basis=TOOL_EVIDENCE``.
    - All LLM-judgment -> ``basis=LLM_JUDGMENT``, issues include
      "no tool evidence for claim" advisory.
    - Mix of tool + LLM -> ``basis=HYBRID``.
    - Aggregate ``passed`` is True only if ALL constituent signals pass.
    - Aggregate ``score`` is the mean of constituent scores.

    Args:
        signals: List of OutcomeSignals to aggregate. May be empty.

    Returns:
        A single OutcomeSignal summarising the full set.
    """
    timestamp = datetime.now(timezone.utc).isoformat()

    if not signals:
        return OutcomeSignal(
            passed=False,
            score=0.0,
            basis=EvidenceBasis.UNSUPPORTED,
            issues=("No outcome signals provided — cannot aggregate",),
            provenance=Provenance(source=_AGGREGATOR_SOURCE, timestamp_utc=timestamp),
        )

    unsupported = [s for s in signals if s.basis is EvidenceBasis.UNSUPPORTED]
    if unsupported:
        all_issues: tuple[str, ...] = tuple(issue for s in unsupported for issue in s.issues)
        return OutcomeSignal(
            passed=False,
            score=0.0,
            basis=EvidenceBasis.UNSUPPORTED,
            issues=all_issues or ("One or more signals have UNSUPPORTED basis — high-accuracy claim cannot be closed",),
            provenance=Provenance(source=_AGGREGATOR_SOURCE, timestamp_utc=timestamp),
        )

    has_tool = any(s.basis is EvidenceBasis.TOOL_EVIDENCE for s in signals)
    has_llm = any(s.basis is EvidenceBasis.LLM_JUDGMENT for s in signals)

    if has_tool and has_llm:
        basis = EvidenceBasis.HYBRID
    elif has_tool:
        basis = EvidenceBasis.TOOL_EVIDENCE
    else:
        basis = EvidenceBasis.LLM_JUDGMENT

    all_passed = all(s.passed for s in signals)
    avg_score = round(sum(s.score for s in signals) / len(signals), 3)
    merged_issues: tuple[str, ...] = tuple(issue for s in signals for issue in s.issues)
    merged_suggestions: tuple[str, ...] = tuple(sug for s in signals for sug in s.suggestions)
    merged_tool_evidence = tuple(evidence for s in signals for evidence in s.tool_evidence)

    advisory: tuple[str, ...] = ()
    if basis is EvidenceBasis.LLM_JUDGMENT:
        advisory = ("no tool evidence for claim — verdict is LLM judgment only",)

    return OutcomeSignal(
        passed=all_passed,
        score=avg_score,
        basis=basis,
        tool_evidence=merged_tool_evidence,
        issues=merged_issues + advisory,
        suggestions=merged_suggestions,
        provenance=Provenance(source=_AGGREGATOR_SOURCE, timestamp_utc=timestamp),
    )


__all__ = ["aggregate_outcome_signals"]
