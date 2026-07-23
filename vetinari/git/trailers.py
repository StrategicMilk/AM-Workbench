"""Git commit trailer generation for decision lineage.

Appends ``Decision-Ref: @ADR-XXXX`` and ``Decision-Ref: @DJ-XXXX`` trailers
to git commits, linking code changes to the ADRs and decision journal entries
that motivated them. Lightweight complement to ADRs: ADRs document the "why"
at design time, trailers link runtime decisions to commits.
"""

from __future__ import annotations

import logging
import re

from vetinari.boundary_guards import account_evidence_drop, require_nonempty

logger = logging.getLogger(__name__)


_ADR_PATTERN = re.compile(r"ADR-(\d+)")


def generate_trailers(
    decision_ids: list[str] | None = None,
    trace_id: str | None = None,
    adr_ids: list[str] | None = None,
) -> list[str]:
    """Generate Decision-Ref git trailers for commit messages.

    Args:
        decision_ids: Explicit decision journal IDs to reference.
        trace_id: Pipeline trace ID used to look up decision IDs.
        adr_ids: Explicit ADR numbers to reference.

    Returns:
        Trailer lines ready to append to a commit message.
    """
    trailers: list[str] = []
    seen: set[str] = set()

    all_decision_ids = list(decision_ids) if decision_ids is not None else []
    if trace_id is not None:
        trace_id = require_nonempty(trace_id, field_name="trace_id")
        all_decision_ids.extend(_get_decisions_for_trace(trace_id))

    for did in all_decision_ids:
        did = require_nonempty(did, field_name="decision_id")
        trailer = f"Decision-Ref: @DJ-{did}"
        if trailer not in seen:
            trailers.append(trailer)
            seen.add(trailer)

    adr_refs = _extract_adr_refs_from_decisions(all_decision_ids)
    if adr_ids:
        adr_refs.extend(adr_ids)

    for adr_id in adr_refs:
        adr_id = require_nonempty(adr_id, field_name="adr_id")
        trailer = f"Decision-Ref: @ADR-{adr_id}"
        if trailer not in seen:
            trailers.append(trailer)
            seen.add(trailer)

    if trailers:
        logger.info("Generated %d decision trailers for commit", len(trailers))
    return trailers


def format_trailers_for_commit(trailers: list[str]) -> str:
    """Format trailer lines for appending to a git commit message.

    Returns:
        Commit-message trailer block, or an empty string.
    """
    if not trailers:
        return ""
    return "\n" + "\n".join(trailers)


def _get_decisions_for_trace(trace_id: str) -> list[str]:
    """Look up decision journal IDs associated with a pipeline trace."""
    try:
        from vetinari.observability.decision_journal import get_decision_journal

        journal = get_decision_journal()
        return journal.get_decision_ids_for_trace(trace_id)
    except Exception:
        account_evidence_drop(
            "decision-journal-trace-lookup",
            "decision-trailers",
            logger=logger,
            evidence_ref=trace_id,
            reason="journal lookup failed",
        )
        logger.warning(
            "Could not look up decisions for trace %s; returning LOOKUP-FAILED sentinel for trailers",
            trace_id,
        )
        return ["LOOKUP-FAILED"]


def _extract_adr_refs_from_decisions(decision_ids: list[str]) -> list[str]:
    """Extract ADR references from decision journal entry metadata."""
    if not decision_ids:
        return []

    adr_refs: list[str] = []
    try:
        from vetinari.observability.decision_journal import get_decision_journal

        journal = get_decision_journal()
        all_records = journal.get_decisions(limit=10_000)
        record_index = {r.decision_id: r for r in all_records}

        for did in decision_ids:
            did = require_nonempty(did, field_name="decision_id")
            record = record_index.get(did)
            if record is None:
                continue
            text = " ".join([
                record.description,
                record.action_taken,
                record.outcome,
                str(record.context),
                str(record.confidence_factors),
            ])
            for match in _ADR_PATTERN.finditer(text):
                adr_num = match.group(1).zfill(4)
                if adr_num not in adr_refs:
                    adr_refs.append(adr_num)
    except Exception:
        account_evidence_drop(
            "decision-journal-adr-lookup",
            "decision-trailers",
            logger=logger,
            evidence_ref=",".join(decision_ids),
            reason="ADR lookup failed",
        )
        logger.warning("Could not extract ADR refs from decisions; preserving available decision trailers")

    return adr_refs
