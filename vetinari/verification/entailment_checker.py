"""Entailment checker — per-claim trust scoring with deterministic and model-based checks.

Verifies individual claims extracted by claim_extractor.py using a tiered
strategy: deterministic checks (AST for code, regex for format) run BEFORE
any model-based verification. This reduces inference costs and provides
fast, reliable verdicts for mechanically verifiable claims.

Trust scoring: each claim receives a score from 0.0 (certainly false) to
1.0 (certainly true). The aggregate trust score is the mean of individual
claim scores, weighted by claim confidence.

Part of the Inspector claim-level verification pipeline (US-013):
    Worker Output → Claim Extraction → **Entailment Checking** → Trust Scoring
"""

from __future__ import annotations

import ast
import json
import logging
import re
from dataclasses import dataclass

from vetinari.boundary_guards import route_enum_error
from vetinari.verification.claim_extractor import Claim, ClaimType

logger = logging.getLogger(__name__)
_VERDICT_METHODS = frozenset({
    "ast",
    "regex",
    "structural",
    "model",
    "stub",
    "structural_failure",
    "entailment_unavailable",
    "unverified",
})


class EntailmentUnavailableError(RuntimeError):
    """Raised by adapters when entailment support is temporarily unavailable."""


class EntailmentNotSupportedError(RuntimeError):
    """Raised by adapters when an entailment method is permanently unsupported."""


def _validated_method(method: str) -> str:
    if method not in _VERDICT_METHODS:
        route_enum_error(
            field_name="method",
            received=method,
            allowed=_VERDICT_METHODS,
            route_id="entailment-verdict-method",
        )
        raise ValueError(f"invalid entailment verdict method: {method}")
    return method


# -- Result types -------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ClaimVerdict:
    """Verification result for a single claim.

    Attributes:
        claim: The original Claim that was verified.
        trust_score: Trust level from 0.0 (false) to 1.0 (true).
        method: Which verification method was used — "ast", "regex",
            "structural", "model", or "unverified".
        explanation: Human-readable explanation of the verdict.
    """

    claim: Claim
    trust_score: float
    method: str
    explanation: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "method", _validated_method(self.method))

    def __repr__(self) -> str:
        """Show claim type, trust score, and method for debugging."""
        return (
            f"ClaimVerdict(type={self.claim.claim_type.value!r}, trust={self.trust_score:.2f}, method={self.method!r})"
        )


@dataclass(frozen=True, slots=True)
class VerificationReport:
    """Aggregated verification result across all claims.

    Attributes:
        verdicts: Individual verdicts for each claim.
        overall_trust: Weighted average trust score across all claims.
        total_claims: Number of claims that were verified.
        deterministic_count: Claims verified by deterministic checks (AST/regex).
        model_count: Claims that required model-based verification.
        unverified_count: Claims that could not be verified by any method.
    """

    verdicts: tuple[ClaimVerdict, ...]
    overall_trust: float
    total_claims: int
    deterministic_count: int
    model_count: int
    unverified_count: int
    structural_failure_count: int = 0

    def __repr__(self) -> str:
        """Show aggregate trust and verification breakdown."""
        return (
            f"VerificationReport(trust={self.overall_trust:.2f}, "
            f"claims={self.total_claims}, "
            f"deterministic={self.deterministic_count}, "
            f"model={self.model_count}, "
            f"unverified={self.unverified_count}, "
            f"structural_failures={self.structural_failure_count})"
        )


# -- Deterministic checkers ---------------------------------------------------


def _check_code_claim(claim: Claim) -> ClaimVerdict:
    """Verify a CODE claim using AST parsing.

    If the claim has an associated code snippet, parse it with ast.parse.
    A successful parse proves "valid Python" claims; a SyntaxError disproves them.

    Args:
        claim: A Claim with claim_type == ClaimType.CODE.

    Returns:
        ClaimVerdict with trust_score based on parse result.
    """
    if not claim.code_snippet:
        return ClaimVerdict(
            claim=claim,
            trust_score=0.5,
            method="ast",
            explanation="No code snippet attached — cannot verify deterministically",
        )

    try:
        ast.parse(claim.code_snippet)
        is_valid = True
    except SyntaxError as exc:
        is_valid = False
        error_msg = f"line {exc.lineno}: {exc.msg}" if exc.lineno else str(exc.msg)

    # Check if the claim asserts validity or invalidity
    claims_valid = "valid" in claim.text.lower() or ("no" in claim.text.lower() and "error" in claim.text.lower())
    claims_invalid = "syntax error" in claim.text.lower() or "invalid" in claim.text.lower()

    if claims_valid and is_valid:
        return ClaimVerdict(
            claim=claim,
            trust_score=1.0,
            method="ast",
            explanation="AST parse succeeded — code is syntactically valid",
        )
    if claims_invalid and not is_valid:
        return ClaimVerdict(
            claim=claim,
            trust_score=1.0,
            method="ast",
            explanation=f"AST parse failed as claimed — {error_msg}",
        )
    if claims_valid and not is_valid:
        return ClaimVerdict(
            claim=claim,
            trust_score=0.0,
            method="ast",
            explanation=f"Claim says valid but AST parse failed — {error_msg}",
        )
    # claims_invalid but code is valid
    return ClaimVerdict(
        claim=claim,
        trust_score=0.0,
        method="ast",
        explanation="Claim says invalid but AST parse succeeded",
    )


def _check_format_claim(claim: Claim) -> ClaimVerdict:
    """Verify a FORMAT claim using regex and format validators.

    Attempts to validate format assertions by checking against known formats
    (JSON, specific patterns). Falls back to unverified if format is unknown.

    Args:
        claim: A Claim with claim_type == ClaimType.FORMAT.

    Returns:
        ClaimVerdict with trust_score based on format validation.
    """
    text_lower = claim.text.lower()

    # Check for JSON validity claims
    if "json" in text_lower and claim.code_snippet:
        try:
            json.loads(claim.code_snippet)
            return ClaimVerdict(
                claim=claim,
                trust_score=1.0,
                method="regex",
                explanation="JSON parse succeeded — format claim verified",
            )
        except (json.JSONDecodeError, TypeError):
            logger.warning("JSON parse failed for claim code snippet — disproved format claim")
            return ClaimVerdict(
                claim=claim,
                trust_score=0.0,
                method="regex",
                explanation="JSON parse failed — format claim disproved",
            )

    # Check for naming convention claims
    if ("naming" in text_lower or "convention" in text_lower) and claim.code_snippet:
        # Check if code uses snake_case (Python convention)
        has_snake = bool(re.search(r"\b[a-z]+_[a-z]+\b", claim.code_snippet))
        has_camel = bool(re.search(r"\b[a-z]+[A-Z][a-z]+\b", claim.code_snippet))
        if "snake" in text_lower and has_snake:
            return ClaimVerdict(
                claim=claim,
                trust_score=0.9,
                method="regex",
                explanation="Snake case naming detected in code snippet",
            )
        if "camel" in text_lower and has_camel:
            return ClaimVerdict(
                claim=claim,
                trust_score=0.9,
                method="regex",
                explanation="Camel case naming detected in code snippet",
            )

    # Cannot deterministically verify this format claim
    return ClaimVerdict(
        claim=claim,
        trust_score=0.5,
        method="unverified",
        explanation="Format claim type not deterministically verifiable",
    )


def _check_structural_claim(claim: Claim) -> ClaimVerdict:
    """Verify a STRUCTURAL claim by counting sections/items in referenced content.

    Looks for numeric assertions ("contains 3 sections") and attempts to
    validate them against the claim's code snippet or the claim text itself.

    Args:
        claim: A Claim with claim_type == ClaimType.STRUCTURAL.

    Returns:
        ClaimVerdict with trust_score based on structural validation.
    """
    # Extract the expected count from the claim
    count_match = re.search(r"\b(\d+)\s+(?:sections?|parts?|items?|steps?|fields?)\b", claim.text, re.IGNORECASE)
    if not count_match:
        return ClaimVerdict(
            claim=claim,
            trust_score=0.0,
            method="structural_failure",
            explanation="Could not extract numeric count from structural claim — reporting as unsupported",
        )

    expected_count = int(count_match.group(1))

    # If there's a code snippet, try to count structural elements
    if claim.code_snippet:
        # Count top-level items (lines, dict keys, list items)
        lines = [line.strip() for line in claim.code_snippet.splitlines() if line.strip()]
        if abs(len(lines) - expected_count) <= 1:
            return ClaimVerdict(
                claim=claim,
                trust_score=0.8,
                method="structural",
                explanation=f"Found {len(lines)} non-empty lines, expected {expected_count}",
            )

    return ClaimVerdict(
        claim=claim,
        trust_score=0.0,
        method="structural_failure",
        explanation=f"Cannot verify structural claim of {expected_count} elements without content — reporting as unsupported",
    )


def _check_factual_claim(claim: Claim) -> ClaimVerdict:
    """Handle a FACTUAL claim that requires model-based verification.

    Fails closed: without a loaded entailment model, factual claims are reported
    as unsupported (trust_score=0.0) rather than given a neutral default score.
    When model-based entailment is wired (bi-encoder + cross-encoder), this
    will delegate to the semantic matching pipeline.

    Args:
        claim: A Claim with claim_type == ClaimType.FACTUAL.

    Returns:
        ClaimVerdict with trust_score=0.0 and method="entailment_unavailable" while no model is loaded.
    """
    # Rule 2 (governance-rules.md): no default-pass verifiers. Until a real
    # bi-encoder/cross-encoder entailment model is wired here, factual claims
    # cannot be verified and MUST be reported as unsupported, not "neutral".
    logger.warning(
        "Entailment model unavailable — factual claim reported as unsupported until model is loaded (claim=%r)",
        claim.text[:80],
    )
    return ClaimVerdict(
        claim=claim,
        trust_score=0.0,
        method="entailment_unavailable",
        explanation="Factual claim requires model-based entailment — model not loaded, reporting as unsupported",
    )


# -- Public API ---------------------------------------------------------------


def verify_claim(claim: Claim) -> ClaimVerdict:
    """Verify a single claim using the appropriate strategy for its type.

    Dispatches to deterministic checkers (AST, regex) for CODE and FORMAT
    claims, structural counting for STRUCTURAL claims, and model-based
    checking for FACTUAL claims. Deterministic checks always run first.

    Args:
        claim: The Claim to verify.

    Returns:
        ClaimVerdict with trust_score and verification method.
    """
    if claim.claim_type == ClaimType.CODE:
        return _check_code_claim(claim)
    if claim.claim_type == ClaimType.FORMAT:
        return _check_format_claim(claim)
    if claim.claim_type == ClaimType.STRUCTURAL:
        return _check_structural_claim(claim)
    return _check_factual_claim(claim)


def verify_claims(claims: list[Claim]) -> VerificationReport:
    """Verify a list of claims and produce an aggregate trust report.

    Runs deterministic checks first (CODE, FORMAT, STRUCTURAL), then
    model-based checks for remaining FACTUAL claims. The overall trust
    score is a weighted average using each claim's extraction confidence.

    Args:
        claims: List of Claims to verify (from extract_claims).

    Returns:
        VerificationReport with per-claim verdicts and aggregate scores.
    """
    if not claims:
        # No claims to verify — return zero trust, not 1.0. Certifying blank input
        # as fully verified is the "default-pass verifier" anti-pattern.
        return VerificationReport(
            verdicts=(),
            overall_trust=0.0,
            total_claims=0,
            deterministic_count=0,
            model_count=0,
            unverified_count=0,
            structural_failure_count=0,
        )

    verdicts: list[ClaimVerdict] = []
    deterministic_count = 0
    model_count = 0
    unverified_count = 0
    structural_failure_count = 0

    for claim in claims:
        verdict = verify_claim(claim)
        verdicts.append(verdict)

        if verdict.method in ("ast", "regex", "structural"):
            deterministic_count += 1
        elif verdict.method == "model":
            model_count += 1
        elif verdict.method == "structural_failure":
            structural_failure_count += 1
        else:
            unverified_count += 1

    # Weighted average trust score. Rule 2: if no claim carries any confidence
    # weight, report overall_trust=0.0 (unsupported) rather than a neutral 0.5
    # default-pass.
    total_weight = sum(v.claim.confidence for v in verdicts)
    if total_weight > 0:
        overall_trust = sum(v.trust_score * v.claim.confidence for v in verdicts) / total_weight
    else:
        overall_trust = 0.0

    logger.info(
        "verify_claims: %d claims -> trust=%.2f (deterministic=%d, model=%d, unverified=%d)",
        len(claims),
        overall_trust,
        deterministic_count,
        model_count,
        unverified_count,
    )

    return VerificationReport(
        verdicts=tuple(verdicts),
        overall_trust=overall_trust,
        total_claims=len(claims),
        deterministic_count=deterministic_count,
        model_count=model_count,
        unverified_count=unverified_count,
        structural_failure_count=structural_failure_count,
    )


__all__ = [
    "ClaimVerdict",
    "EntailmentNotSupportedError",
    "EntailmentUnavailableError",
    "VerificationReport",
    "verify_claim",
    "verify_claims",
]
