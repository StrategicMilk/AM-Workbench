"""Fail-closed claim verification hooks for the Inspector pipeline.

Enforces six failure modes that MUST produce ``OutcomeSignal(passed=False)``:

1. Missing citation: a claim carries no Provenance or evidence -> UNSUPPORTED.
2. Stale evidence: evidence older than the configured freshness window -> UNSUPPORTED.
3. Unverified file claim: claimed file does not exist or SHA-256 hash mismatches -> UNSUPPORTED.
4. Entailment contradiction: static entailment checker flags a false claim -> UNSUPPORTED.
5. Bare HUMAN_ATTESTED on high-accuracy factual claim (no attested_artifacts) -> UNSUPPORTED.
6. LLM-only on high-accuracy claim: basis <= LLM_JUDGMENT -> failed LLM_JUDGMENT with advisory.

HUMAN_ATTESTED-with-artifact narrowing (used by cases 5b-d):
- A matching COMMIT_SHA whose diff contains the claimed change -> passes.
- A SIGNED_REVIEW whose scope covers the file being claimed about -> passes.
- An ADR_REFERENCE unrelated to the claim scope -> rejected ("artifact does not support claim").
- SIGNED_REVIEW on an intent-confirmation use case -> passes (not a factual closure path).

Rule 2 invariant: ``OutcomeSignal.score`` MUST be 0.0 whenever ``basis`` is
``EvidenceBasis.UNSUPPORTED``.

This is step 4 of the pipeline: Intake → Planning → Execution → **Quality Gate** → Assembly.
"""

from __future__ import annotations

import dataclasses
import hashlib
import logging
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path

from vetinari.agents.contracts import AttestedArtifact, OutcomeSignal, Provenance
from vetinari.types import ArtifactKind, EvidenceBasis

logger = logging.getLogger(__name__)


# -- Module-level constants ---------------------------------------------------

# Default freshness window: evidence older than this is stale.
DEFAULT_FRESHNESS_WINDOW_SECONDS: int = 3600  # 1 hour

# Source label embedded into Provenance records emitted by this module.
_VERIFIER_SOURCE = "vetinari.verification.claim_verifier"


# -- Internal helpers ---------------------------------------------------------


def _now_utc() -> datetime:
    """Return the current UTC datetime.

    Returns:
        Current UTC datetime with tzinfo set.
    """
    return datetime.now(timezone.utc)


def _parse_utc(ts: str) -> datetime | None:
    """Parse an ISO-8601 UTC timestamp string.

    Accepts both ``Z`` suffix and ``+00:00`` offset.  Returns ``None`` when
    the string cannot be parsed, so callers can treat it as stale.

    Args:
        ts: ISO-8601 UTC timestamp string.

    Returns:
        Parsed timezone-aware datetime or None on parse failure.
    """
    if not ts:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f+00:00", "%Y-%m-%dT%H:%M:%S+00:00", "%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.strptime(ts, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            logger.debug("_parse_utc_timestamp: format %r did not match %r — trying next", fmt, ts)
    # Fallback: fromisoformat (Python 3.11+ understands Z suffix natively)
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        logger.debug("_parse_utc_timestamp: could not parse %r — treating as unparseable (stale)", ts)
        return None


def _make_provenance(extra: str = "") -> Provenance:
    """Build a Provenance record stamped to now.

    Args:
        extra: Optional suffix to append to the source label for traceability.

    Returns:
        Provenance with current UTC timestamp.
    """
    src = f"{_VERIFIER_SOURCE}" + (f":{extra}" if extra else "")
    return Provenance(source=src, timestamp_utc=_now_utc().isoformat())


def _unsupported(issues: tuple[str, ...], hook: str = "") -> OutcomeSignal:
    """Produce a fail-closed UNSUPPORTED OutcomeSignal.

    Rule 2 invariant: score is always 0.0 when basis is UNSUPPORTED.

    Args:
        issues: Human-readable reasons for the unsupported verdict.
        hook: Short label of the hook that produced this signal.

    Returns:
        OutcomeSignal with passed=False, score=0.0, basis=UNSUPPORTED.
    """
    return OutcomeSignal(
        passed=False,
        score=0.0,
        basis=EvidenceBasis.UNSUPPORTED,
        issues=issues,
        provenance=_make_provenance(hook),
    )


# -- Failure mode 1: Missing citation -----------------------------------------


def verify_citation_present(
    signal: OutcomeSignal,
    *,
    require_provenance: bool = True,
) -> OutcomeSignal:
    """Verify that a claim signal carries a Provenance citation.

    Failure mode 1: a signal with no provenance or no evidence basis above
    UNSUPPORTED cannot be trusted.  Returns the original signal unchanged
    when evidence is present; returns a new UNSUPPORTED signal otherwise.

    Args:
        signal: The OutcomeSignal to check.
        require_provenance: When True (default), a missing Provenance object
            counts as a missing citation.

    Returns:
        The original signal if a citation is present, otherwise a new
        OutcomeSignal with passed=False, basis=UNSUPPORTED.
    """
    if require_provenance and signal.provenance is None:
        return _unsupported(("no citation attached",), "citation")

    if signal.basis is EvidenceBasis.UNSUPPORTED:
        issues = signal.issues or ("no citation attached",)
        return _unsupported(issues, "citation")

    return signal


# -- Failure mode 2: Stale evidence -------------------------------------------


def verify_evidence_freshness(
    signal: OutcomeSignal,
    *,
    freshness_window_seconds: int = DEFAULT_FRESHNESS_WINDOW_SECONDS,
    clock: Callable[[], datetime] = _now_utc,
) -> OutcomeSignal:
    """Verify that a signal's evidence was produced within the freshness window.

    Failure mode 2: evidence older than ``freshness_window_seconds`` is
    treated as stale.  When the Provenance timestamp cannot be parsed, the
    evidence is also treated as stale (fail-closed).

    Args:
        signal: Outcome signal whose provenance timestamp should be checked.
        freshness_window_seconds: Maximum accepted evidence age.
        clock: Time source used for deterministic freshness checks.

    Args:
        signal: The OutcomeSignal to check.
        freshness_window_seconds: Maximum age of evidence in seconds.
            Defaults to DEFAULT_FRESHNESS_WINDOW_SECONDS (3600).

    Returns:
        The original signal if evidence is fresh enough, otherwise a new
        OutcomeSignal with passed=False and reason "stale evidence (fresh-by X)".
    """
    if signal.provenance is None:
        return _unsupported(
            ("stale evidence (fresh-by unknown — no provenance timestamp)",),
            "freshness",
        )

    ts = _parse_utc(signal.provenance.timestamp_utc)
    if ts is None:
        return _unsupported(
            ("stale evidence (fresh-by unknown — timestamp unparseable)",),
            "freshness",
        )

    now = clock()
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    cutoff = now - timedelta(seconds=freshness_window_seconds)
    if ts < cutoff:
        fresh_by = signal.provenance.timestamp_utc
        return _unsupported(
            (f"stale evidence (fresh-by {fresh_by})",),
            "freshness",
        )

    return signal


# -- Failure mode 3: Unverified file claim ------------------------------------


def verify_file_claim(
    signal: OutcomeSignal,
    *,
    claimed_path: str,
    expected_sha256: str | None = None,
) -> OutcomeSignal:
    """Verify that a claimed file exists and optionally matches a SHA-256 hash.

    Failure mode 3: if the file does not exist, or if ``expected_sha256`` is
    provided and the file's actual hash differs, the claim is unverified.

    Args:
        signal: The OutcomeSignal that makes the file claim.
        claimed_path: Filesystem path that the signal claims exists.
        expected_sha256: Optional SHA-256 hex digest the file must match.
            When omitted, only existence is checked.

    Returns:
        The original signal if the file claim is verified, otherwise a new
        OutcomeSignal with passed=False and reason "file claim unverified: <path>".
    """
    path = Path(claimed_path)
    if not path.exists():
        return _unsupported(
            (f"file claim unverified: {claimed_path}",),
            "file_claim",
        )

    if expected_sha256 is not None:
        try:
            actual_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError as exc:
            logger.warning(
                "Could not read file %r for hash verification — treating as unverified: %s",
                claimed_path,
                exc,
            )
            return _unsupported(
                (f"file claim unverified: {claimed_path} (read error)",),
                "file_claim",
            )

        if actual_hash != expected_sha256.lower():
            return _unsupported(
                (f"file claim unverified: {claimed_path} (hash mismatch)",),
                "file_claim",
            )

    return signal


# -- Failure mode 4: Entailment contradiction ---------------------------------


def verify_no_entailment_contradiction(
    signal: OutcomeSignal,
    *,
    entailment_passed: bool,
    claim_text: str = "",
) -> OutcomeSignal:
    """Verify that the entailment checker did not flag a false claim.

    Failure mode 4: if the static entailment checker (SESSION-01 SHARD-01)
    detected a contradiction, the signal must fail regardless of any other
    evidence.

    Args:
        signal: The OutcomeSignal carrying the claim result.
        entailment_passed: True when the entailment checker found no
            contradiction; False when it detected a false claim.
        claim_text: Optional short description of the claim for diagnostics.

    Returns:
        The original signal if no contradiction was detected, otherwise a new
        OutcomeSignal with passed=False and reason "entailment contradiction".
    """
    if not entailment_passed:
        detail = f" ({claim_text!r})" if claim_text else ""
        return _unsupported(
            (f"entailment contradiction{detail}",),
            "entailment",
        )
    return signal


# -- Failure mode 5: Bare HUMAN_ATTESTED on high-accuracy factual claim -------


def _artifact_supports_claim(artifact: AttestedArtifact, claim_scope: str) -> bool:
    """Check whether a concrete artifact plausibly supports the given claim scope.

    Matching is intentionally liberal: the function checks whether scope tokens
    appear in the artifact payload or kind.  Callers supply the unambiguous
    claim scope (e.g., a file path or change description) so narrow payloads
    produce False.

    Args:
        artifact: The AttestedArtifact to evaluate.
        claim_scope: A string describing what the claim is about (e.g., a file
            path, a change identifier, or a module name).

    Returns:
        True when the artifact plausibly covers the claim scope.
    """
    if not claim_scope:
        # No scope to match: assume the artifact is generic -> does not support
        return False

    scope_lower = claim_scope.lower()

    # COMMIT_SHA: check if repo or SHA hint contains scope tokens
    if artifact.kind is ArtifactKind.COMMIT_SHA:
        payload_str = " ".join(str(v).lower() for v in artifact.payload.values())
        return scope_lower in payload_str

    # SIGNED_REVIEW: check scope against reviewer or payload scope field
    if artifact.kind is ArtifactKind.SIGNED_REVIEW:
        payload_str = " ".join(str(v).lower() for v in artifact.payload.values())
        return scope_lower in payload_str

    # ADR_REFERENCE: check adr_id and status against scope
    if artifact.kind is ArtifactKind.ADR_REFERENCE:
        adr_id = str(artifact.payload.get("adr_id", "")).lower()
        return scope_lower in adr_id

    # COMMAND_INVOCATION: check command string against scope
    if artifact.kind is ArtifactKind.COMMAND_INVOCATION:
        cmd = str(artifact.payload.get("command", "")).lower()
        return scope_lower in cmd

    # EXTERNAL_RECEIPT: check URL and issuer
    if artifact.kind is ArtifactKind.EXTERNAL_RECEIPT:
        payload_str = " ".join(str(v).lower() for v in artifact.payload.values())
        return scope_lower in payload_str

    return False


def verify_human_attestation(
    signal: OutcomeSignal,
    *,
    claim_scope: str = "",
    is_intent_confirmation: bool = False,
) -> OutcomeSignal:
    """Verify HUMAN_ATTESTED signals carry a matching artifact for factual claims.

    Failure mode 5: bare HUMAN_ATTESTED (no ``attested_artifacts``) on a
    high-accuracy factual claim must fail.  An artifact that does not match
    the claim scope also fails.

    Non-factual paths (``is_intent_confirmation=True``) are allowed to carry
    bare attestation — they represent consent or override appeals, not
    evidence for a factual assertion.

    This function only inspects signals with ``basis == HUMAN_ATTESTED``.
    Signals with other bases are returned unchanged.

    Args:
        signal: The OutcomeSignal to check.
        claim_scope: Scope identifier for the claim (e.g. a file path or
            change summary).  Used to check whether attached artifacts actually
            support the specific claim.  Pass ``""`` to skip scope matching.
        is_intent_confirmation: When True, bare attestation is acceptable
            (consent / override-appeal use case).

    Returns:
        The original signal when attestation is acceptable, otherwise a new
        OutcomeSignal with passed=False and an explanatory reason.
    """
    if signal.basis is not EvidenceBasis.HUMAN_ATTESTED:
        return signal

    if is_intent_confirmation:
        # Intent-confirmation paths (destructive-op consent, override appeal)
        # are allowed bare attestation — this is NOT a factual-claim closure path.
        return signal

    # Check that at least one artifact is present
    if not signal.attested_artifacts:
        return _unsupported(
            ("human attestation requires a concrete attested artifact for high-accuracy claim closure",),
            "human_attestation",
        )

    # If a claim scope is given, verify that at least one artifact supports it
    if claim_scope:
        matching = [a for a in signal.attested_artifacts if _artifact_supports_claim(a, claim_scope)]
        if not matching:
            return _unsupported(
                ("attested artifact does not support the specific claim",),
                "human_attestation",
            )

    return signal


# -- Failure mode 6: LLM-only on high-accuracy claim -------------------------


def verify_not_llm_only(
    signal: OutcomeSignal,
    *,
    high_accuracy: bool = True,
) -> OutcomeSignal:
    """Fail high-accuracy LLM-only signals while preserving the advisory.

    Failure mode 6: an LLM-only signal (basis == LLM_JUDGMENT, no tool
    evidence) on a high-accuracy factual claim path must not pass.  The
    function adds a "no tool evidence" issue when missing and returns the
    signal with ``passed=False``.

    When ``high_accuracy`` is False the signal is returned unchanged.

    Args:
        signal: The OutcomeSignal to check.
        high_accuracy: Whether this is a high-accuracy factual claim path.
            When False, no advisory is added.

    Returns:
        The original signal when tool evidence is present; otherwise a failed
        signal with "no tool evidence" advisory added to issues.
    """
    if not high_accuracy:
        return signal

    if signal.basis is not EvidenceBasis.LLM_JUDGMENT:
        return signal

    if signal.tool_evidence:
        # Has tool evidence — no advisory needed
        return signal

    advisory = "no tool evidence — verdict is LLM judgment only"
    issues = signal.issues if advisory in signal.issues else (*signal.issues, advisory)
    return dataclasses.replace(signal, passed=False, score=0.0, issues=issues)


# -- Composite gate: run all six checks in sequence ---------------------------


def verify_claim_fail_closed(
    signal: OutcomeSignal,
    *,
    claim_scope: str = "",
    is_intent_confirmation: bool = False,
    entailment_passed: bool = True,
    claim_text: str = "",
    freshness_window_seconds: int = DEFAULT_FRESHNESS_WINDOW_SECONDS,
    claimed_path: str | None = None,
    expected_sha256: str | None = None,
    high_accuracy: bool = True,
) -> OutcomeSignal:
    """Run all six fail-closed verification checks on a claim signal.

    Applies the six failure modes in sequence.  The first failure short-circuits
    the remaining checks: a failed signal is returned immediately so consumers
    see the earliest/most critical reason for rejection.

    Checks applied in order:
    1. Citation present (provenance not None, basis not UNSUPPORTED).
    2. Evidence freshness (provenance timestamp within freshness_window_seconds).
    3. File claim verified (only when claimed_path is given).
    4. No entailment contradiction.
    5. HUMAN_ATTESTED has a matching artifact (on non-intent-confirmation paths).
    6. LLM-only high-accuracy path fails closed with advisory.

    Args:
        signal: The OutcomeSignal to validate.
        claim_scope: Scope of the claim for HUMAN_ATTESTED artifact matching.
        is_intent_confirmation: Skip artifact matching for consent/override paths.
        entailment_passed: False when the entailment checker detected a
            contradiction.
        claim_text: Short description of the claim for diagnostics.
        freshness_window_seconds: Maximum age of evidence in seconds.
        claimed_path: Optional file path whose existence to verify.
        expected_sha256: Optional hash to match for the claimed file.
        high_accuracy: Whether to apply the LLM-only advisory.

    Returns:
        An OutcomeSignal — either the original (possibly with advisory issues
        added) or a new UNSUPPORTED signal carrying the first failure reason.
    """
    result = verify_citation_present(signal)
    if not result.passed and result.basis is EvidenceBasis.UNSUPPORTED:
        return result

    result = verify_evidence_freshness(result, freshness_window_seconds=freshness_window_seconds)
    if not result.passed and result.basis is EvidenceBasis.UNSUPPORTED:
        return result

    if claimed_path is not None:
        result = verify_file_claim(
            result,
            claimed_path=claimed_path,
            expected_sha256=expected_sha256,
        )
        if not result.passed and result.basis is EvidenceBasis.UNSUPPORTED:
            return result

    result = verify_no_entailment_contradiction(
        result,
        entailment_passed=entailment_passed,
        claim_text=claim_text,
    )
    if not result.passed and result.basis is EvidenceBasis.UNSUPPORTED:
        return result

    result = verify_human_attestation(
        result,
        claim_scope=claim_scope,
        is_intent_confirmation=is_intent_confirmation,
    )
    if not result.passed and result.basis is EvidenceBasis.UNSUPPORTED:
        return result

    return verify_not_llm_only(result, high_accuracy=high_accuracy)


__all__ = [
    "DEFAULT_FRESHNESS_WINDOW_SECONDS",
    "verify_citation_present",
    "verify_claim_fail_closed",
    "verify_evidence_freshness",
    "verify_file_claim",
    "verify_human_attestation",
    "verify_no_entailment_contradiction",
    "verify_not_llm_only",
]
