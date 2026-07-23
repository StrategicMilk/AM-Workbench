"""Inspector Extract — retroactive decision surfacing from implementation diffs.

After the Inspector reviews completed work, this module runs an "Extract"
pass that infers implicit decisions from the implementation — architectural
choices that were made but never recorded as explicit ADRs. Surfaces these
as candidate decision journal entries for human review.

Scans diffs for: new dependencies, API design choices, data model changes,
error handling strategies, and other patterns that represent significant
decisions.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

from vetinari.types import ConfidenceLevel, DecisionType

logger = logging.getLogger(__name__)


# -- Data types ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CandidateDecision:
    """An implicit decision surfaced from a code diff.

    Args:
        description: What decision was made (human-readable).
        decision_type: Category of the decision.
        evidence: The diff lines or patterns that suggest this decision.
        confidence: How certain we are this is a real decision (0.0-1.0).
        adr_exists: Whether a matching ADR was found.
        reasoning: Why we think this is a decision worth recording.
    """

    description: str
    decision_type: DecisionType = DecisionType.MODEL_SELECTION
    evidence: str = ""
    confidence: float = 0.5
    adr_exists: bool = False
    reasoning: str = ""

    def __repr__(self) -> str:
        return (
            f"CandidateDecision(desc={self.description!r}, "
            f"confidence={self.confidence:.2f}, adr_exists={self.adr_exists})"
        )


# -- Pattern detectors --------------------------------------------------------

# Patterns that indicate architectural decisions in diffs
_PATTERNS: list[dict[str, Any]] = [
    {
        "name": "new_dependency",
        "regex": re.compile(r"^\+\s*(from|import)\s+(\w+)"),
        "decision_type": DecisionType.MODEL_SELECTION,
        "description_template": "New dependency introduced: {match}",
        "confidence": 0.6,
        "reasoning": "Adding a dependency is an architectural choice with long-term maintenance implications.",
    },
    {
        "name": "new_api_endpoint",
        "regex": re.compile(r"^\+.*@(get|post|put|delete|patch|route)\("),
        "decision_type": DecisionType.TASK_ROUTING,
        "description_template": "New API endpoint: {match}",
        "confidence": 0.7,
        "reasoning": "API design decisions affect all consumers and are hard to change after adoption.",
    },
    {
        "name": "new_dataclass",
        "regex": re.compile(r"^\+\s*@dataclass"),
        "decision_type": DecisionType.MODEL_SELECTION,
        "description_template": "New data model introduced: {context}",
        "confidence": 0.5,
        "reasoning": "Data model changes affect serialization, storage, and all downstream consumers.",
    },
    {
        "name": "new_enum",
        "regex": re.compile(r"^\+\s*class\s+\w+\(.*Enum\)"),
        "decision_type": DecisionType.MODEL_SELECTION,
        "description_template": "New enum type: {match}",
        "confidence": 0.5,
        "reasoning": "Enum definitions constrain valid states across the system.",
    },
    {
        "name": "error_handling_strategy",
        "regex": re.compile(r"^\+\s*(except|raise)\s+(\w+)"),
        "decision_type": DecisionType.QUALITY_THRESHOLD,
        "description_template": "Error handling strategy: {match}",
        "confidence": 0.4,
        "reasoning": "Error handling choices determine failure modes and recovery behavior.",
    },
    {
        "name": "sqlite_table",
        "regex": re.compile(r"^\+.*CREATE TABLE", re.IGNORECASE),
        "decision_type": DecisionType.MODEL_SELECTION,
        "description_template": "New database table: {match}",
        "confidence": 0.8,
        "reasoning": "Schema changes are hard to reverse and affect data persistence layer.",
    },
    {
        "name": "threading_choice",
        "regex": re.compile(r"^\+\s*(threading\.Lock|ThreadPoolExecutor|asyncio)"),
        "decision_type": DecisionType.PARAMETER_TUNING,
        "description_template": "Concurrency model choice: {match}",
        "confidence": 0.6,
        "reasoning": "Concurrency model affects scalability and correctness constraints.",
    },
    {
        "name": "singleton_pattern",
        "regex": re.compile(r"^\+.*def get_\w+\(\).*->"),
        "decision_type": DecisionType.MODEL_SELECTION,
        "description_template": "Singleton accessor introduced: {match}",
        "confidence": 0.4,
        "reasoning": "Singleton lifecycle management affects initialization order and testability.",
    },
]

# Known ADR patterns to check against
_ADR_REF_PATTERN = re.compile(r"ADR-(\d+)")


def _normalize_adr_ref(adr: str) -> str:
    ref = adr.strip()
    match = _ADR_REF_PATTERN.search(ref)
    return match.group(1) if match else ref


def _normalize_adr_refs(adrs: list[str]) -> set[str]:
    """Normalize ADR references to numeric IDs for comparison."""
    return {_normalize_adr_ref(adr) for adr in adrs}


def _extract_pattern_override_decisions(
    diff: str,
    patterns: list[dict[str, Any]],
    active_existing_adrs: set[str],
) -> list[dict[str, Any]]:
    decisions: list[dict[str, Any]] = []
    for pattern in patterns:
        pattern_adr = pattern.get("adr")
        adr_exists = bool(pattern_adr is not None and _normalize_adr_ref(str(pattern_adr)) in active_existing_adrs)
        if adr_exists:
            continue
        decisions.append({
            "pattern_id": pattern.get("id", ""),
            "evidence": diff,
            "adr_exists": adr_exists,
        })
    return decisions


def _candidate_for_pattern(
    *,
    pattern: dict[str, Any],
    line: str,
    lines: list[str],
    index: int,
    diff_adr_refs: set[str],
    active_existing_adrs: set[str],
) -> CandidateDecision:
    match = pattern["regex"].search(line)
    if match is None:
        raise ValueError("pattern did not match line")
    match_text = match.group(0).strip().lstrip("+").strip()
    context_line = _next_added_context_line(lines, index)
    description = pattern["description_template"].format(
        match=match_text,
        context=context_line or match_text,
    )
    confidence = pattern["confidence"]
    if len(match_text) < 10:
        confidence *= 0.7
    return CandidateDecision(
        description=description,
        decision_type=pattern["decision_type"],
        evidence=line.strip(),
        confidence=round(confidence, 2),
        adr_exists=bool(diff_adr_refs & active_existing_adrs) if active_existing_adrs else False,
        reasoning=pattern["reasoning"],
    )


def extract_implicit_decisions(
    diff: str,
    context: dict[str, Any] | None = None,
    *,
    patterns: list[dict[str, Any]] | None = None,
    existing_adrs: list[str] | None = None,
) -> list[CandidateDecision] | list[dict[str, Any]]:
    """Scan a diff for architectural choices not recorded as ADRs.

    Analyzes added lines for patterns indicating significant decisions:
    new dependencies, API endpoints, data models, error handling strategies,
    schema changes, and concurrency choices. Filters out changes that
    already reference an ADR.

    Args:
        diff: Unified diff text (e.g. from ``git diff``).
        context: Optional context dict with keys like "existing_adrs"
            (list of known ADR numbers) to suppress already-documented decisions.
        patterns: Optional override list of decision-pattern specs; defaults to the
            built-in pattern set when omitted.
        existing_adrs: Optional list of known ADR identifiers used to suppress
            decisions that already reference an ADR.

    Returns:
        List of CandidateDecision instances. Low-confidence extractions
        are included but flagged, not auto-committed.
    """
    context = context if context is not None else {}
    active_existing_adrs = _normalize_adr_refs(
        existing_adrs if existing_adrs is not None else context.get("existing_adrs", [])
    )
    if patterns is not None:
        return _extract_pattern_override_decisions(diff, patterns, active_existing_adrs)
    candidates: list[CandidateDecision] = []
    seen_descriptions: set[str] = set()  # Deduplicate

    # Check if the diff itself references ADRs (skip those patterns)
    diff_adr_refs = set(_ADR_REF_PATTERN.findall(diff))

    lines = diff.split("\n")
    for i, line in enumerate(lines):
        for pattern in _PATTERNS:
            match = pattern["regex"].search(line)
            if match is None:
                continue

            candidate = _candidate_for_pattern(
                pattern=pattern,
                line=line,
                lines=lines,
                index=i,
                diff_adr_refs=diff_adr_refs,
                active_existing_adrs=active_existing_adrs,
            )
            if candidate.description in seen_descriptions:
                continue
            seen_descriptions.add(candidate.description)
            candidates.append(candidate)

    # Sort by confidence descending
    candidates.sort(key=lambda c: c.confidence, reverse=True)

    if candidates:
        logger.info(
            "Inspector Extract found %d candidate decisions (%d high-confidence)",
            len(candidates),
            sum(1 for c in candidates if c.confidence >= 0.6),
        )

    return candidates


def _next_added_context_line(lines: list[str], index: int) -> str:
    for j in range(index + 1, min(index + 5, len(lines))):
        if lines[j].startswith("+") and lines[j].strip() != "+":
            return lines[j].lstrip("+").strip()
    return ""


def log_extracted_decisions(candidates: list[CandidateDecision]) -> list[str]:
    """Write candidate decisions to the decision journal with implicit-needs-review status.

    Only logs candidates with confidence >= 0.5 that don't already have
    a matching ADR. Low-confidence candidates are returned but not persisted.

    Args:
        candidates: List of CandidateDecision from ``extract_implicit_decisions()``.

    Returns:
        List of decision_id strings for the logged entries.
    """
    logged_ids: list[str] = []

    try:
        from vetinari.observability.decision_journal import get_decision_journal

        journal = get_decision_journal()
    except Exception:
        logger.warning("Decision journal unavailable — cannot log extracted decisions")
        return logged_ids

    for candidate in candidates:
        if candidate.adr_exists:
            continue  # Already documented
        if candidate.confidence < 0.5:
            continue  # Too uncertain to log

        confidence_level = (
            ConfidenceLevel.HIGH
            if candidate.confidence >= 0.7
            else ConfidenceLevel.MEDIUM
            if candidate.confidence >= 0.5
            else ConfidenceLevel.LOW
        )

        record = journal.log_decision(
            decision_type=candidate.decision_type,
            chosen=candidate.description,
            confidence=confidence_level,
            reasoning=candidate.reasoning,
            status="implicit-needs-review",
            metadata={
                "evidence": candidate.evidence,
                "extraction_confidence": candidate.confidence,
                "source": "inspector_extract",
            },
        )
        logged_ids.append(record.decision_id)

    if logged_ids:
        logger.info(
            "Logged %d extracted decisions to journal (status=implicit-needs-review)",
            len(logged_ids),
        )

    return logged_ids
