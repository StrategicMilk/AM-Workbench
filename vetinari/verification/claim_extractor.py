"""Claim extraction — splits Worker output into individual verifiable assertions.

Given a block of text (Worker output), this module identifies individual claims:
factual assertions, code correctness claims, format claims, and structural claims.
Each claim is tagged with a type that determines which verification strategy applies
(deterministic AST check, regex format check, or model-based entailment).

Part of the Inspector claim-level verification pipeline (US-013):
    Worker Output → **Claim Extraction** → Entailment Checking → Trust Scoring
"""

from __future__ import annotations

import ast
import logging
import re
from dataclasses import dataclass
from enum import Enum

logger = logging.getLogger(__name__)


# -- Enums and data types -----------------------------------------------------


class ClaimType(Enum):
    """Classification of a claim that determines verification strategy.

    CODE claims are checked via AST parsing (deterministic).
    FORMAT claims are checked via regex (deterministic).
    FACTUAL claims require model-based entailment checking.
    STRUCTURAL claims check document/output structure.
    """

    CODE = "code"  # "This function returns X", "No syntax errors"
    FORMAT = "format"  # "Output is valid JSON", "Follows naming convention"
    FACTUAL = "factual"  # "The algorithm has O(n) complexity"
    STRUCTURAL = "structural"  # "The response has 3 sections"


@dataclass(frozen=True, slots=True)
class Claim:
    """A single verifiable assertion extracted from Worker output.

    Attributes:
        text: The claim text as extracted from the output.
        claim_type: Classification determining verification strategy.
        source_span: Tuple of (start_char, end_char) positions in the original text.
        confidence: Extraction confidence -- how likely this is a real claim (0.0-1.0).
        code_snippet: Associated code block, if the claim references code.
    """

    text: str
    claim_type: ClaimType
    source_span: tuple[int, int]
    confidence: float = 1.0
    code_snippet: str | None = None

    def __repr__(self) -> str:
        """Show claim type and truncated text for debugging."""
        truncated = self.text[:60] + "..." if len(self.text) > 60 else self.text
        return f"Claim(type={self.claim_type.value!r}, text={truncated!r})"


# -- Extraction patterns ------------------------------------------------------

# Sentence-ending punctuation for splitting prose into individual claims
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z])")

# Patterns that indicate a code claim (function behavior, return values, etc.)
_CODE_CLAIM_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b(?:function|method|class)\s+\w+\s+(?:returns?|raises?|accepts?)\b", re.IGNORECASE),
    re.compile(r"\bno\s+(?:syntax|import|runtime)\s+errors?\b", re.IGNORECASE),
    re.compile(r"\b(?:compiles?|parses?)\s+(?:successfully|without)\b", re.IGNORECASE),
    re.compile(r"\b(?:passes?|fails?)\s+(?:all|the)?\s*tests?\b", re.IGNORECASE),
    re.compile(r"\bimplements?\s+(?:the\s+)?\w+\s+(?:interface|protocol|pattern)\b", re.IGNORECASE),
)

# Patterns that indicate a format claim (JSON validity, naming, structure)
_FORMAT_CLAIM_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bvalid\s+(?:JSON|YAML|XML|HTML|CSV)\b", re.IGNORECASE),
    re.compile(r"\bfollows?\s+(?:the\s+)?(?:naming|formatting|style)\s+convention\b", re.IGNORECASE),
    re.compile(r"\b(?:conforms?|adheres?)\s+to\s+(?:the\s+)?\w+\s+(?:schema|format|spec)\b", re.IGNORECASE),
    re.compile(r"\bformatted\s+(?:as|in)\s+\w+\b", re.IGNORECASE),
)

# Patterns that indicate a structural claim (sections, parts, items)
_STRUCTURAL_CLAIM_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b(?:contains?|has|includes?)\s+\d+\s+(?:sections?|parts?|items?|steps?|fields?)\b", re.IGNORECASE),
    re.compile(r"\b(?:organized|structured|divided)\s+into\s+\d+\b", re.IGNORECASE),
    re.compile(r"\b(?:each|every)\s+(?:section|part|item)\s+(?:has|contains|includes)\b", re.IGNORECASE),
)


# -- Code block extraction ----------------------------------------------------

# Fenced code block pattern (```python ... ``` or ``` ... ```)
_CODE_BLOCK_RE = re.compile(r"```(?:\w+)?\s*\n(.*?)```", re.DOTALL)


def _extract_code_blocks(text: str) -> list[tuple[str, int, int]]:
    """Extract fenced code blocks from markdown-style text.

    Args:
        text: The full Worker output text.

    Returns:
        List of (code_content, start_pos, end_pos) tuples.
    """
    blocks: list[tuple[str, int, int]] = [
        (match.group(1).strip(), match.start(), match.end()) for match in _CODE_BLOCK_RE.finditer(text)
    ]
    return blocks


# -- Classification -----------------------------------------------------------


def _classify_sentence(sentence: str) -> ClaimType:
    """Classify a single sentence into its claim type based on pattern matching.

    Tests patterns in order: CODE → FORMAT → STRUCTURAL → FACTUAL (default).

    Args:
        sentence: A single sentence to classify.

    Returns:
        The most specific ClaimType that matches, or FACTUAL as default.
    """
    for pattern in _CODE_CLAIM_PATTERNS:
        if pattern.search(sentence):
            return ClaimType.CODE

    for pattern in _FORMAT_CLAIM_PATTERNS:
        if pattern.search(sentence):
            return ClaimType.FORMAT

    for pattern in _STRUCTURAL_CLAIM_PATTERNS:
        if pattern.search(sentence):
            return ClaimType.STRUCTURAL

    return ClaimType.FACTUAL


def _is_claim_like(sentence: str) -> bool:
    """Determine whether a sentence contains a verifiable assertion.

    Filters out questions, filler phrases, and non-assertive text.

    Args:
        sentence: A sentence to evaluate.

    Returns:
        True if the sentence looks like a verifiable claim, False otherwise.
    """
    stripped = sentence.strip()

    # Too short to be a meaningful claim
    if len(stripped) < 10:
        return False

    # Questions are not claims
    if stripped.endswith("?"):
        return False

    # Hedging phrases reduce claim confidence to zero
    hedge_patterns = (
        r"^(?:I think|maybe|perhaps|possibly|it seems|not sure)\b",
        r"^(?:you could|you might|consider)\b",
    )
    return all(not re.match(pattern, stripped, re.IGNORECASE) for pattern in hedge_patterns)


# -- Public API ---------------------------------------------------------------


def extract_claims(text: str) -> list[Claim]:
    """Split Worker output text into individual verifiable claims.

    Returns:
        Value produced for the caller.
    """
    if not text or not text.strip():
        return []
    claims: list[Claim] = []
    code_blocks = _extract_code_blocks(text)
    code_spans: set[tuple[int, int]] = set()
    for code_content, start, end in code_blocks:
        code_spans.add((start, end))
        is_valid_python = True
        try:
            ast.parse(code_content)
        except SyntaxError:
            is_valid_python = False
        claim_text = "Code block is valid Python" if is_valid_python else "Code block contains syntax errors"
        claims.append(
            Claim(
                text=claim_text,
                claim_type=ClaimType.CODE,
                source_span=(start, end),
                confidence=0.95 if is_valid_python else 0.8,
                code_snippet=code_content,
            )
        )
    prose = text
    for _, start, end in sorted(code_blocks, key=lambda x: x[1], reverse=True):
        prose = prose[:start] + " " * (end - start) + prose[end:]
    sentences = _SENTENCE_SPLIT.split(prose)
    _search_cursor = 0
    for sentence in sentences:
        sentence = sentence.strip()
        if not _is_claim_like(sentence):
            continue
        claim_type = _classify_sentence(sentence)
        pos = text.find(sentence, _search_cursor)
        if pos >= 0:
            _search_cursor = pos + len(sentence)
            span = (pos, pos + len(sentence))
        else:
            span = (0, len(sentence))
        claims.append(
            Claim(
                text=sentence,
                claim_type=claim_type,
                source_span=span,
                confidence=0.9,
            )
        )
    claims.sort(key=lambda c: c.source_span[0])
    logger.debug(
        "extract_claims: %d claims extracted (%d code, %d format, %d factual, %d structural)",
        len(claims),
        sum(1 for c in claims if c.claim_type == ClaimType.CODE),
        sum(1 for c in claims if c.claim_type == ClaimType.FORMAT),
        sum(1 for c in claims if c.claim_type == ClaimType.FACTUAL),
        sum(1 for c in claims if c.claim_type == ClaimType.STRUCTURAL),
    )
    return claims


def extract_claims_by_type(text: str) -> dict[ClaimType, list[Claim]]:
    """Extract claims and group them by type for targeted verification.

    Convenience wrapper around extract_claims() that pre-groups the results
    so callers can apply type-specific verification strategies without
    filtering.

    Args:
        text: The full Worker output text to analyse.

    Returns:
        Dictionary mapping ClaimType to list of claims of that type.
        Types with no claims are omitted from the dict.
    """
    claims = extract_claims(text)
    grouped: dict[ClaimType, list[Claim]] = {}
    for claim in claims:
        grouped.setdefault(claim.claim_type, []).append(claim)
    return grouped


__all__ = [
    "Claim",
    "ClaimType",
    "extract_claims",
    "extract_claims_by_type",
]
