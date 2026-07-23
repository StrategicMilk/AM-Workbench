"""12-dimension document quality rubric.

Evaluates text across 12 quality dimensions organized into three tiers:

- **Foundational** (4): accuracy, correctness, completeness, relevance
- **Professional** (4): clarity, conciseness, organization, readability
- **Excellence** (4): consistency, specificity, style, technical_depth

Each dimension produces a score from 0.0 to 1.0.  The overall score is
a weighted average using dimension weights from the document profile.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from vetinari.config_paths import resolve_config_path
from vetinari.validation.document_types import DocumentProfile, get_profile_for_type

logger = logging.getLogger(__name__)


# ── Quality Dimensions ──────────────────────────────────────────────

DIMENSIONS: list[str] = [
    # Foundational tier
    "accuracy",
    "correctness",
    "completeness",
    "relevance",
    # Professional tier
    "clarity",
    "conciseness",
    "organization",
    "readability",
    # Excellence tier
    "consistency",
    "specificity",
    "style",
    "technical_depth",
]

FOUNDATIONAL = DIMENSIONS[:4]
PROFESSIONAL = DIMENSIONS[4:8]
EXCELLENCE = DIMENSIONS[8:]


@dataclass
class DimensionScore:
    """Score for a single quality dimension.

    Args:
        dimension: Name of the quality dimension.
        score: Numeric score from 0.0 to 1.0.
        weight: Importance weight from the document profile.
        findings: Specific issues or observations for this dimension.
    """

    dimension: str
    score: float
    weight: float = 1.0
    findings: list[str] = field(default_factory=list)

    def __repr__(self) -> str:
        """Show key identifying fields for debugging."""
        return f"DimensionScore(dimension={self.dimension!r}, score={self.score!r}, weight={self.weight!r})"


@dataclass
class QualityReport:
    """Complete quality evaluation report for a document.

    Args:
        doc_type: The document type that was evaluated.
        text_length: Character count of the evaluated text.
        dimension_scores: Per-dimension evaluation results.
        overall_score: Weighted average across all dimensions.
        passed: Whether the overall score meets the profile minimum.
        anti_ai_findings: Style violations from writing_style.yaml checks.
        profile_rules_passed: Which profile-specific rules passed.
        profile_rules_failed: Which profile-specific rules failed.
    """

    doc_type: str
    text_length: int
    dimension_scores: list[DimensionScore] = field(default_factory=list)
    overall_score: float = 0.0
    passed: bool = False
    anti_ai_findings: list[str] = field(default_factory=list)
    profile_rules_passed: list[str] = field(default_factory=list)
    profile_rules_failed: list[str] = field(default_factory=list)

    def __repr__(self) -> str:
        """Show key identifying fields for debugging."""
        return (
            f"QualityReport(doc_type={self.doc_type!r}, overall_score={self.overall_score!r}, passed={self.passed!r})"
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-compatible dictionary."""
        return {
            "doc_type": self.doc_type,
            "text_length": self.text_length,
            "overall_score": round(self.overall_score, 3),
            "passed": self.passed,
            "dimensions": {
                ds.dimension: {"score": round(ds.score, 3), "weight": ds.weight, "findings": ds.findings}
                for ds in self.dimension_scores
            },
            "anti_ai_findings": self.anti_ai_findings,
            "rules_passed": self.profile_rules_passed,
            "rules_failed": self.profile_rules_failed,
        }


def validate_program_pack_closure_status(
    payload: object,
    *,
    expected_pack_slug: str,
    expected_source_ids: set[str],
    required_evidence_refs: set[str] | None = None,
    validation_command: str | None = None,
) -> list[str]:
    """Validate a program-pack closure ledger and fail closed on overclaiming.

    The program packs use closure ledgers as the durable handoff between source
    remediation and pack-level evidence. This validator keeps the ledger honest:
    every expected source row must be terminal, evidence-backed, and attached to
    at least one proof row before a pack may be marked passed.

    Returns:
        List of validation errors; an empty list means the closure payload is
        consistent with the expected pack slug, source IDs, and evidence refs.
    """
    errors: list[str] = []
    if not isinstance(payload, dict):
        return ["closure payload is not a JSON object"]
    if not expected_pack_slug:
        errors.append("expected_pack_slug is required")
    if not expected_source_ids:
        errors.append("expected_source_ids is required")

    if payload.get("pack_slug") != expected_pack_slug:
        errors.append("pack_slug mismatch")
    if payload.get("status") != "passed" or payload.get("passed") is not True:
        errors.append("closure is not passed")

    errors.extend(
        f"{counter} is nonzero"
        for counter in (
            "open_rows",
            "partial_rows",
            "deferred_rows",
            "blocked_rows",
            "regressed_rows",
            "ownership_exception_rows",
        )
        if payload.get(counter) != 0
    )
    if payload.get("source_rows_total") != len(expected_source_ids):
        errors.append("source row total mismatch")

    raw_promise_rows = payload.get("promise_rows", [])
    if not isinstance(raw_promise_rows, list):
        errors.append("promise rows must be a list")
        raw_promise_rows = []
    promise_rows = {str(row.get("id")): row for row in raw_promise_rows if isinstance(row, dict)}
    if not promise_rows:
        errors.append("missing promise rows")
    elif validation_command and not any(
        validation_command in (_string_list_field(row, "verification") or []) for row in promise_rows.values()
    ):
        errors.append("promise rows missing validation command")

    required_refs = required_evidence_refs or set()
    raw_source_findings = payload.get("source_findings", [])
    if not isinstance(raw_source_findings, list):
        errors.append("source findings must be a list")
        raw_source_findings = []
    rows = {str(row.get("id")): row for row in raw_source_findings if isinstance(row, dict)}
    missing = sorted(expected_source_ids - rows.keys())
    extra = sorted(rows.keys() - expected_source_ids)
    if missing:
        errors.append(f"missing source ids: {', '.join(missing)}")
    if extra:
        errors.append(f"extra source ids: {', '.join(extra)}")

    for source_id in sorted(expected_source_ids & rows.keys()):
        row = rows[source_id]
        if row.get("status") != "passed":
            errors.append(f"{source_id} is not passed")
        proof_rows = _string_list_field(row, "proof_rows")
        if not proof_rows:
            errors.append(f"{source_id} missing proof rows")
        raw_refs = _string_list_field(row, "evidence_refs")
        if raw_refs is None:
            errors.append(f"{source_id} evidence refs must be a list of strings")
            raw_refs = []
        refs = set(raw_refs)
        missing_refs = sorted(required_refs - refs)
        if missing_refs:
            errors.append(f"{source_id} missing evidence refs: {', '.join(missing_refs)}")
        note = str(row.get("notes", ""))
        if source_id not in note or len(note) < 80:
            errors.append(f"{source_id} note is not source-specific")
    return errors


# ── Heuristic Scorers ───────────────────────────────────────────────


def _string_list_field(row: dict[str, Any], field: str) -> list[str] | None:
    raw = row.get(field, [])
    if not isinstance(raw, list) or any(not isinstance(item, str) or not item for item in raw):
        return None
    return raw


def _score_accuracy(text: str) -> tuple[float, list[str]]:
    """Heuristic accuracy check — flags hedging and vague claims."""
    findings: list[str] = []
    score = 1.0
    hedging = [r"\bprobably\b", r"\bmaybe\b", r"\bperhaps\b", r"\bmight be\b"]
    for pattern in hedging:
        if re.search(pattern, text, re.IGNORECASE):
            score -= 0.1
            findings.append(f"Hedging language detected: {pattern}")
    return max(0.0, score), findings


def _score_correctness(text: str) -> tuple[float, list[str]]:
    """Heuristic correctness — checks for broken references and placeholders."""
    findings: list[str] = []
    score = 1.0
    placeholders = [r"\bTODO\b", r"\bFIXME\b", r"\bXXX\b", r"\bplaceholder\b", r"\blorem ipsum\b"]
    for pattern in placeholders:
        if re.search(pattern, text, re.IGNORECASE):
            score -= 0.2
            findings.append(f"Placeholder found: {pattern}")
    return max(0.0, score), findings


def _score_completeness(text: str) -> tuple[float, list[str]]:
    """Heuristic completeness — checks for empty sections and missing content."""
    findings: list[str] = []
    score = 1.0
    # Empty markdown sections (heading followed by another heading or end)
    empty_sections = re.findall(r"^(#{1,4}\s+.+)\n\s*\n(?=#{1,4}\s|\Z)", text, re.MULTILINE)
    if empty_sections:
        score -= 0.15 * len(empty_sections)
        findings.append(f"{len(empty_sections)} empty section(s) detected")
    if len(text.strip()) < 50:
        score -= 0.3
        findings.append("Very short document (under 50 characters)")
    return max(0.0, score), findings


def _score_relevance(text: str) -> tuple[float, list[str]]:
    """Heuristic relevance based on content substance.

    Without reference context, approximates relevance by checking whether
    the text contains varied, substantive content vs repetitive filler.
    """
    findings: list[str] = []
    words = text.split()
    if not words:
        return 0.0, ["Empty document"]

    unique_ratio = len({w.lower() for w in words}) / len(words)
    score = min(1.0, unique_ratio * 1.5)  # 0.67 unique → 1.0

    if len(words) < 20:
        score *= 0.5
        findings.append("Document is very short (< 20 words)")
    if unique_ratio < 0.3:
        findings.append(f"High word repetition (unique ratio: {unique_ratio:.2f})")

    return max(0.0, min(1.0, score)), findings


def _score_clarity(text: str) -> tuple[float, list[str]]:
    """Heuristic clarity — flags overly long sentences."""
    findings: list[str] = []
    sentences = re.split(r"[.!?]+", text)
    long_count = sum(1 for s in sentences if len(s.split()) > 40)
    score = 1.0
    if sentences and long_count > 0:
        ratio = long_count / max(len(sentences), 1)
        score -= ratio * 0.4
        findings.append(f"{long_count} sentence(s) exceed 40 words")
    return max(0.0, score), findings


def _score_conciseness(text: str) -> tuple[float, list[str]]:
    """Heuristic conciseness — flags filler phrases."""
    findings: list[str] = []
    score = 1.0
    fillers = [
        r"\bit is important to note\b",
        r"\bit should be noted\b",
        r"\bin order to\b",
        r"\bbasically\b",
        r"\bfundamentally\b",
        r"\bat the end of the day\b",
    ]
    for pattern in fillers:
        if re.search(pattern, text, re.IGNORECASE):
            score -= 0.08
            findings.append(f"Filler phrase: {pattern}")
    return max(0.0, score), findings


def _score_organization(text: str) -> tuple[float, list[str]]:
    """Heuristic organization — checks for headings and structure."""
    findings: list[str] = []
    score = 1.0
    lines = text.split("\n")
    if len(lines) > 20:
        headings = [line for line in lines if line.startswith("#")]
        if not headings:
            score -= 0.3
            findings.append("Long document with no headings")
    return max(0.0, score), findings


def _score_readability(text: str) -> tuple[float, list[str]]:
    """Heuristic readability — average word length as proxy."""
    findings: list[str] = []
    words = text.split()
    if not words:
        return 0.5, ["Empty text"]
    avg_word_len = sum(len(w) for w in words) / len(words)
    score = 1.0
    if avg_word_len > 8:
        score -= 0.2
        findings.append(f"High average word length ({avg_word_len:.1f})")
    return max(0.0, score), findings


def _score_consistency(text: str) -> tuple[float, list[str]]:
    """Heuristic consistency — checks for mixed formatting styles."""
    findings: list[str] = []
    score = 1.0
    # Mixed list markers
    has_dash = bool(re.search(r"^\s*-\s", text, re.MULTILINE))
    has_star = bool(re.search(r"^\s*\*\s", text, re.MULTILINE))
    if has_dash and has_star:
        score -= 0.15
        findings.append("Mixed list markers (- and *)")
    return max(0.0, score), findings


def _score_specificity(text: str) -> tuple[float, list[str]]:
    """Heuristic specificity — flags vague language."""
    findings: list[str] = []
    score = 1.0
    vague = [r"\bvarious\b", r"\bseveral\b", r"\bmany\b", r"\bsome\b", r"\betc\.?\b"]
    count = 0
    for pattern in vague:
        count += len(re.findall(pattern, text, re.IGNORECASE))
    if count > 3:
        score -= min(0.3, count * 0.05)
        findings.append(f"{count} vague quantifier(s) found")
    return max(0.0, score), findings


def _score_style(text: str, style_config: dict[str, Any] | None = None) -> tuple[float, list[str]]:
    """Style scoring using anti-AI-tell patterns from writing_style.yaml."""
    findings: list[str] = []
    score = 1.0
    if style_config is None:
        style_config = _load_writing_style()
    tells = style_config.get("anti_ai_tells", [])
    for tell in tells:
        pattern = tell.get("pattern", "")
        if pattern and re.search(pattern, text, re.IGNORECASE):
            severity = tell.get("severity", "info")
            penalty = {"error": 0.15, "warning": 0.08, "info": 0.03}.get(severity, 0.05)
            score -= penalty
            findings.append(f"AI tell ({severity}): {tell.get('suggestion', pattern)}")
    return max(0.0, score), findings


def _score_technical_depth(text: str) -> tuple[float, list[str]]:
    """Heuristic technical depth — checks for code blocks, examples, specifics."""
    findings: list[str] = []
    score = 0.7  # baseline
    if "```" in text:
        score += 0.15
    if re.search(r"\b\d+(\.\d+)?\b", text):
        score += 0.05  # contains specific numbers
    if re.search(r"`[a-zA-Z_]\w*`", text):
        score += 0.1  # inline code references
    return min(1.0, score), findings


_SCORERS: dict[str, Any] = {
    "accuracy": _score_accuracy,
    "correctness": _score_correctness,
    "completeness": _score_completeness,
    "relevance": _score_relevance,
    "clarity": _score_clarity,
    "conciseness": _score_conciseness,
    "organization": _score_organization,
    "readability": _score_readability,
    "consistency": _score_consistency,
    "specificity": _score_specificity,
    "style": _score_style,
    "technical_depth": _score_technical_depth,
}


# ── Style config loader ─────────────────────────────────────────────

_style_config: dict[str, Any] | None = None


def _config_dir() -> Path:
    return resolve_config_path()


def _load_writing_style(config_path: Path | None = None) -> dict[str, Any]:
    """Load writing style configuration from YAML."""
    global _style_config
    if _style_config is not None:
        return _style_config
    path = config_path or (_config_dir() / "writing_style.yaml")
    if not path.exists():
        logger.debug("Writing style config not found at %s", path)
        _style_config = {}
        return _style_config
    with Path(path).open(encoding="utf-8") as f:
        _style_config = yaml.safe_load(f) or {}
    return _style_config


# ── Main evaluator ──────────────────────────────────────────────────


def evaluate_document(
    text: str,
    doc_type: str = "default",
    profile: DocumentProfile | None = None,
) -> QualityReport:
    """Evaluate a document against the 12-dimension quality rubric.

    Args:
        text: The document text to evaluate.
        doc_type: Document type name for profile lookup.
        profile: Optional explicit profile (overrides doc_type lookup).

    Returns:
        QualityReport with per-dimension scores and overall result.
    """
    if profile is None:
        profile = get_profile_for_type(doc_type)

    style_config = _load_writing_style()
    dimension_scores: list[DimensionScore] = []
    weighted_sum = 0.0
    total_weight = 0.0

    for dim in DIMENSIONS:
        weight = profile.dimension_weights.get(dim, 1.0)
        scorer = _SCORERS.get(dim)
        if scorer is None:
            continue

        if dim == "style":
            score, findings = scorer(text, style_config)
        else:
            score, findings = scorer(text)

        ds = DimensionScore(dimension=dim, score=score, weight=weight, findings=findings)
        dimension_scores.append(ds)
        weighted_sum += score * weight
        total_weight += weight

    overall = weighted_sum / total_weight if total_weight > 0 else 0.0
    passed = overall >= profile.min_score

    # Collect anti-AI findings from the style dimension
    anti_ai = []
    for ds in dimension_scores:
        if ds.dimension == "style":
            anti_ai = ds.findings

    return QualityReport(
        doc_type=doc_type,
        text_length=len(text),
        dimension_scores=dimension_scores,
        overall_score=overall,
        passed=passed,
        anti_ai_findings=anti_ai,
    )
