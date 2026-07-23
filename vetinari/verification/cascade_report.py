"""Verification cascade report with an iterable top-level unsupported_claims field.

The cascade report is the structured output of running the fail-closed claim
verification hooks over a set of factual claims.  It is designed so downstream
consumers (Work Receipts SESSION-06, Claims Ledger SESSION-04) can iterate
``unsupported_claims`` directly without parsing free-form text.

Relationship to existing modules:
- :mod:`vetinari.verification.claim_verifier` — produces OutcomeSignals for
  individual claims via the six fail-closed hooks.
- :mod:`vetinari.skills.inspector_outcome` — aggregates signals; this report
  wraps that aggregation with an explicit ``unsupported_claims`` index.

This is step 4 of the pipeline: Intake → Planning → Execution → **Quality Gate** → Assembly.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from vetinari.agents.contracts import OutcomeSignal
from vetinari.skills.inspector_outcome import aggregate_outcome_signals
from vetinari.types import EvidenceBasis
from vetinari.verification.claim_verifier import verify_claim_fail_closed

logger = logging.getLogger(__name__)


_REPORT_SOURCE = "vetinari.verification.cascade_report"


# -- CascadeClaimRecord ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CascadeClaimRecord:
    """A single claim entry in the verification cascade report.

    Attributes:
        claim_id: Unique identifier for this claim (caller-supplied or auto-generated).
        claim_text: Human-readable text of the claim being verified.
        signal: The OutcomeSignal produced by the fail-closed verification hooks.
        is_unsupported: True when the signal basis is UNSUPPORTED (convenience flag).
    """

    claim_id: str
    claim_text: str
    signal: OutcomeSignal
    is_unsupported: bool

    def __repr__(self) -> str:
        return f"CascadeClaimRecord(claim_id={self.claim_id!r}, passed={self.signal.passed!r}, basis={self.signal.basis.value!r})"


# -- VerificationCascadeReport ------------------------------------------------


@dataclass(frozen=True, slots=True)
class VerificationCascadeReport:
    """Aggregated result of running all fail-closed checks over a set of claims.

    The ``unsupported_claims`` field is the primary consumer surface: it is a
    list of CascadeClaimRecords whose signals have basis UNSUPPORTED.  Downstream
    consumers iterate this list directly; they do NOT need to parse the
    ``aggregate_signal.issues`` free-form text.

    Attributes:
        claim_records: All CascadeClaimRecords produced during the cascade run, in
            submission order.
        unsupported_claims: Subset of claim_records where
            ``signal.basis == UNSUPPORTED``.  Guaranteed to be a strict subset
            of claim_records and always iterable even when empty.
        aggregate_signal: A single OutcomeSignal that summarises the full set
            (produced by ``aggregate_outcome_signals``).
        generated_at_utc: ISO-8601 UTC timestamp of report generation.
    """

    claim_records: tuple[CascadeClaimRecord, ...]
    unsupported_claims: tuple[CascadeClaimRecord, ...]
    aggregate_signal: OutcomeSignal
    generated_at_utc: str

    def __repr__(self) -> str:
        return (
            f"VerificationCascadeReport("
            f"total={len(self.claim_records)},"
            f" unsupported={len(self.unsupported_claims)},"
            f" passed={self.aggregate_signal.passed!r})"
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialise the report to a JSON-compatible dictionary.

        The ``unsupported_claims`` key is a top-level list of dicts — consumers
        do NOT need to walk ``aggregate_signal.issues`` to find unsupported items.

        Returns:
            JSON-compatible dictionary representation of the report.
        """

        def _record_to_dict(r: CascadeClaimRecord) -> dict[str, Any]:
            return {
                "claim_id": r.claim_id,
                "claim_text": r.claim_text,
                "passed": r.signal.passed,
                "basis": r.signal.basis.value,
                "score": r.signal.score,
                "issues": list(r.signal.issues),
                "is_unsupported": r.is_unsupported,
            }

        return {
            "generated_at_utc": self.generated_at_utc,
            "total_claims": len(self.claim_records),
            "unsupported_count": len(self.unsupported_claims),
            "passed": self.aggregate_signal.passed,
            "aggregate_basis": self.aggregate_signal.basis.value,
            "aggregate_score": self.aggregate_signal.score,
            "aggregate_issues": list(self.aggregate_signal.issues),
            # Top-level iterable — no free-form text parsing required.
            "unsupported_claims": [_record_to_dict(r) for r in self.unsupported_claims],
            "claim_records": [_record_to_dict(r) for r in self.claim_records],
        }


# -- Factory ------------------------------------------------------------------


def build_cascade_report(
    claim_signals: list[tuple[str, str, OutcomeSignal]],
    *,
    claim_scope: str = "",
    is_intent_confirmation: bool = False,
    entailment_passed: bool = True,
    freshness_window_seconds: int = 3600,
    high_accuracy: bool = True,
) -> VerificationCascadeReport:
    """Run the fail-closed verification cascade over a set of pre-produced signals.

    Each signal is passed through ``verify_claim_fail_closed`` before being
    recorded.  After all claims are processed, ``aggregate_outcome_signals``
    collapses them into a single verdict that becomes ``aggregate_signal``.

    The aggregate step CANNOT silently convert a failed claim to a passing
    report: if ANY claim has basis UNSUPPORTED, the aggregate is also
    UNSUPPORTED (enforced by ``aggregate_outcome_signals``).

    Args:
        claim_signals: List of ``(claim_id, claim_text, OutcomeSignal)`` tuples
            representing the signals to validate.
        claim_scope: Scope identifier for HUMAN_ATTESTED artifact matching.
        is_intent_confirmation: Skip artifact matching for consent/override paths.
        entailment_passed: False when the entailment checker flagged a
            contradiction across the batch (applies uniformly to all signals).
        freshness_window_seconds: Maximum evidence age in seconds.
        high_accuracy: Whether to apply the LLM-only advisory.

    Returns:
        VerificationCascadeReport with all records, unsupported_claims index,
        and aggregate signal.
    """
    now_ts = datetime.now(timezone.utc).isoformat()
    records: list[CascadeClaimRecord] = []

    for claim_id, claim_text, raw_signal in claim_signals:
        verified = verify_claim_fail_closed(
            raw_signal,
            claim_scope=claim_scope,
            is_intent_confirmation=is_intent_confirmation,
            entailment_passed=entailment_passed,
            claim_text=claim_text,
            freshness_window_seconds=freshness_window_seconds,
            high_accuracy=high_accuracy,
        )
        is_unsupported = verified.basis is EvidenceBasis.UNSUPPORTED
        records.append(
            CascadeClaimRecord(
                claim_id=claim_id,
                claim_text=claim_text,
                signal=verified,
                is_unsupported=is_unsupported,
            )
        )

    unsupported = tuple(r for r in records if r.is_unsupported)

    # aggregate_outcome_signals enforces: any UNSUPPORTED constituent ->
    # aggregate is also UNSUPPORTED (no silent trust inflation).
    aggregate = aggregate_outcome_signals([r.signal for r in records])

    logger.info(
        "cascade_report: %d claims, %d unsupported, aggregate_passed=%s",
        len(records),
        len(unsupported),
        aggregate.passed,
    )

    return VerificationCascadeReport(
        claim_records=tuple(records),
        unsupported_claims=unsupported,
        aggregate_signal=aggregate,
        generated_at_utc=now_ts,
    )


__all__ = [
    "CascadeClaimRecord",
    "VerificationCascadeReport",
    "build_cascade_report",
]
