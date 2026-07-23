"""Knowledge lint — scan memory for contradictions, stale entries, orphans, and vocabulary drift.

Extracted from pdca.py to stay under the 550-line file limit.
Called by PDCAController during the Check phase.

Pipeline role: Kaizen Check → **Knowledge Lint** → Plan improvements.
"""

from __future__ import annotations

import difflib
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


# ── Constants ─────────────────────────────────────────────────────────────────

_STALE_THRESHOLD_DAYS = 90  # Entries not accessed in 90 days are stale
_SIMILARITY_THRESHOLD = 0.85  # Content similarity that indicates potential contradiction or duplication
_VOCABULARY_SIMILARITY = 0.7  # Term similarity for vocabulary drift detection

_MS_PER_DAY = 86_400_000  # Milliseconds in one day — used for epoch-ms age calculations
_ORPHAN_THRESHOLD_DAYS = 30  # Entries with zero recalls and older than this are orphaned


# ── Data classes ──────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class LintFinding:
    """A single lint finding produced by KnowledgeLinter.

    Attributes:
        finding_id: Unique identifier for this finding (lint_<hex>).
        category: Lint category — one of: contradiction, stale, orphaned, vocabulary_drift.
        description: Human-readable explanation of the issue.
        severity: How urgent the finding is — info, warning, or error.
        entry_ids: Tuple of memory entry IDs involved in the finding.
        timestamp: ISO-8601 UTC timestamp when the finding was created.
    """

    finding_id: str
    category: str  # "contradiction" | "stale" | "orphaned" | "vocabulary_drift"
    description: str
    severity: str  # "info" | "warning" | "error"
    entry_ids: tuple[str, ...] = ()
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(),
    )

    def __repr__(self) -> str:
        return f"LintFinding(finding_id={self.finding_id!r}, category={self.category!r}, severity={self.severity!r})"


@dataclass(slots=True)
class KnowledgeLintReport:
    """Aggregated result of a full lint pass over memory entries.

    Attributes:
        findings: All lint findings discovered during the pass.
        checked_entries: Total number of entries examined.
        timestamp: ISO-8601 UTC timestamp when the report was produced.
    """

    findings: list[LintFinding] = field(default_factory=list)
    checked_entries: int = 0
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(),
    )

    def __repr__(self) -> str:
        """Compact representation showing finding count and checked entry count."""
        return (
            f"KnowledgeLintReport("
            f"findings={len(self.findings)}, "
            f"checked_entries={self.checked_entries}, "
            f"timestamp={self.timestamp!r})"
        )


# ── Linter ────────────────────────────────────────────────────────────────────


class KnowledgeLinter:
    """Scans memory entries for quality issues and emits lint findings.

    Runs four independent checks over a list of MemoryEntry-like objects:
    contradictions, stale entries, orphaned entries, and vocabulary drift.
    Each finding is emitted as a KaizenLintFinding event via the EventBus.
    """

    def __init__(self) -> None:
        """Initialise the linter — no external dependencies required."""

    def lint_all(self, entries: list[Any]) -> KnowledgeLintReport:
        """Run all four lint checks and return an aggregated report.

        Emits a KaizenLintFinding event for every finding discovered.

        Args:
            entries: List of MemoryEntry-like objects.  Each entry is expected
                to expose: ``id`` (str), ``content`` (str), ``entry_type``
                (str), ``last_accessed`` (int, epoch ms), ``summary`` (str),
                ``recall_count`` (int), and ``timestamp`` (int, epoch ms).

        Returns:
            KnowledgeLintReport containing all findings and the count of
            entries that were examined.

        Raises:
            Exception: If any lint check fails (backing store errors, check
                failures). Failures are never silently converted to empty findings.
        """
        # Late import to avoid circular dependencies — improvement_events
        # imports from vetinari.events which may import from kaizen.
        from vetinari.kaizen.improvement_events import emit_lint_finding

        report = KnowledgeLintReport(checked_entries=len(entries))

        all_findings: list[LintFinding] = []
        try:
            all_findings.extend(self.check_contradictions(entries))
            all_findings.extend(self.check_stale(entries))
            all_findings.extend(self.check_orphaned(entries))
            all_findings.extend(self.check_vocabulary_drift(entries))
        except Exception:
            logger.exception("Knowledge lint check failed — unable to complete linting")
            raise

        report.findings = all_findings

        for finding in all_findings:
            emit_lint_finding(
                finding_id=finding.finding_id,
                category=finding.category,
                description=finding.description,
                severity=finding.severity,
            )

        logger.info(
            "Knowledge lint complete: %d entries checked, %d findings (%d errors, %d warnings, %d info)",
            len(entries),
            len(all_findings),
            sum(1 for f in all_findings if f.severity == "error"),
            sum(1 for f in all_findings if f.severity == "warning"),
            sum(1 for f in all_findings if f.severity == "info"),
        )
        return report

    def check_contradictions(self, entries: list[Any]) -> list[LintFinding]:
        """Identify pairs of same-type entries whose content is suspiciously similar.

        Groups entries by ``entry_type``, then compares every pair within each
        group using ``difflib.SequenceMatcher``.  Pairs whose similarity ratio
        exceeds ``_SIMILARITY_THRESHOLD`` but whose content differs are flagged
        as potential contradictions — they are close enough to be about the
        same topic yet differ in detail, which can confuse agents that recall
        both.

        Args:
            entries: Memory entries to examine.

        Returns:
            List of LintFindings with category ``"contradiction"`` and
            severity ``"warning"``.
        """
        findings: list[LintFinding] = []

        # Group by entry_type so we only compare semantically related entries
        by_type: dict[str, list[Any]] = {}
        for entry in entries:
            entry_type = getattr(entry, "entry_type", "unknown")
            by_type.setdefault(entry_type, []).append(entry)

        for entry_type, group in by_type.items():
            for i in range(len(group)):
                for j in range(i + 1, len(group)):
                    a = group[i]
                    b = group[j]
                    content_a = getattr(a, "content", "") or ""
                    content_b = getattr(b, "content", "") or ""

                    if not content_a or not content_b:
                        continue

                    ratio = difflib.SequenceMatcher(None, content_a, content_b).ratio()

                    if ratio > _SIMILARITY_THRESHOLD and content_a != content_b:
                        finding_id = f"lint_{uuid.uuid4().hex[:8]}"
                        findings.append(
                            LintFinding(
                                finding_id=finding_id,
                                category="contradiction",
                                description=(
                                    f"Entries {getattr(a, 'id', '?')!r} and "
                                    f"{getattr(b, 'id', '?')!r} (type={entry_type!r}) "
                                    f"have {ratio:.0%} content similarity but differ — "
                                    "possible contradiction or outdated duplicate"
                                ),
                                severity="warning",
                                entry_ids=(
                                    str(getattr(a, "id", "")),
                                    str(getattr(b, "id", "")),
                                ),
                            )
                        )
                        logger.debug(
                            "Contradiction candidate: entries %r/%r similarity=%.2f",
                            getattr(a, "id", "?"),
                            getattr(b, "id", "?"),
                            ratio,
                        )

        return findings

    def check_stale(self, entries: list[Any]) -> list[LintFinding]:
        """Flag entries that have not been accessed within the staleness window.

        An entry is stale when its ``last_accessed`` epoch-ms value is non-zero
        (i.e. it has been accessed at least once) and the elapsed time since
        that access exceeds ``_STALE_THRESHOLD_DAYS``.  Never-accessed entries
        (``last_accessed == 0``) are handled by ``check_orphaned`` instead.

        Args:
            entries: Memory entries to examine.

        Returns:
            List of LintFindings with category ``"stale"`` and severity
            ``"info"``.
        """
        findings: list[LintFinding] = []
        now_ms = _now_epoch_ms()

        for entry in entries:
            last_accessed: int = getattr(entry, "last_accessed", 0) or 0
            if last_accessed <= 0:
                # Never accessed — orphan check handles this case
                continue

            age_days = (now_ms - last_accessed) / _MS_PER_DAY
            if age_days > _STALE_THRESHOLD_DAYS:
                finding_id = f"lint_{uuid.uuid4().hex[:8]}"
                findings.append(
                    LintFinding(
                        finding_id=finding_id,
                        category="stale",
                        description=(
                            f"Entry {getattr(entry, 'id', '?')!r} was last accessed "
                            f"{age_days:.0f} days ago (threshold: {_STALE_THRESHOLD_DAYS} days) "
                            "— consider refreshing or archiving"
                        ),
                        severity="info",
                        entry_ids=(str(getattr(entry, "id", "")),),
                    )
                )

        return findings

    def check_orphaned(self, entries: list[Any]) -> list[LintFinding]:
        """Flag entries that have never been recalled and are sufficiently old.

        An entry is orphaned when ``recall_count == 0`` AND the time elapsed
        since it was created (``timestamp`` epoch ms) exceeds
        ``_ORPHAN_THRESHOLD_DAYS``.  Fresh entries with zero recalls are
        normal; only old ones indicate that the knowledge was stored but never
        consumed.

        Args:
            entries: Memory entries to examine.

        Returns:
            List of LintFindings with category ``"orphaned"`` and severity
            ``"warning"``.
        """
        findings: list[LintFinding] = []
        now_ms = _now_epoch_ms()

        for entry in entries:
            recall_count: int = getattr(entry, "recall_count", 0) or 0
            if recall_count > 0:
                continue

            created_ms: int = getattr(entry, "timestamp", 0) or 0
            if created_ms <= 0:
                # No creation timestamp — skip rather than false-positive
                continue

            age_days = (now_ms - created_ms) / _MS_PER_DAY
            if age_days > _ORPHAN_THRESHOLD_DAYS:
                finding_id = f"lint_{uuid.uuid4().hex[:8]}"
                findings.append(
                    LintFinding(
                        finding_id=finding_id,
                        category="orphaned",
                        description=(
                            f"Entry {getattr(entry, 'id', '?')!r} has never been recalled "
                            f"and is {age_days:.0f} days old — knowledge stored but never used"
                        ),
                        severity="warning",
                        entry_ids=(str(getattr(entry, "id", "")),),
                    )
                )

        return findings

    def check_vocabulary_drift(self, entries: list[Any]) -> list[LintFinding]:
        """Detect entries whose summaries are nearly identical but whose content diverges.

        Compares every pair of entry summaries using ``difflib.SequenceMatcher``.
        When two summaries are more similar than ``_VOCABULARY_SIMILARITY`` yet
        the full content differs significantly (similarity below
        ``_SIMILARITY_THRESHOLD``), the entries likely describe the same concept
        using inconsistent vocabulary — vocabulary drift.

        Args:
            entries: Memory entries to examine.

        Returns:
            List of LintFindings with category ``"vocabulary_drift"`` and
            severity ``"info"``.
        """
        findings: list[LintFinding] = []

        for i in range(len(entries)):
            for j in range(i + 1, len(entries)):
                a = entries[i]
                b = entries[j]

                summary_a = getattr(a, "summary", "") or ""
                summary_b = getattr(b, "summary", "") or ""

                if not summary_a or not summary_b:
                    continue

                summary_ratio = difflib.SequenceMatcher(None, summary_a, summary_b).ratio()

                if summary_ratio <= _VOCABULARY_SIMILARITY:
                    continue

                content_a = getattr(a, "content", "") or ""
                content_b = getattr(b, "content", "") or ""

                if not content_a or not content_b:
                    continue

                content_ratio = difflib.SequenceMatcher(None, content_a, content_b).ratio()

                # Summaries converged but content diverged — vocabulary drift
                if content_ratio < _SIMILARITY_THRESHOLD:
                    finding_id = f"lint_{uuid.uuid4().hex[:8]}"
                    findings.append(
                        LintFinding(
                            finding_id=finding_id,
                            category="vocabulary_drift",
                            description=(
                                f"Entries {getattr(a, 'id', '?')!r} and "
                                f"{getattr(b, 'id', '?')!r} have similar summaries "
                                f"({summary_ratio:.0%}) but different content "
                                f"({content_ratio:.0%}) — terminology may have drifted"
                            ),
                            severity="info",
                            entry_ids=(
                                str(getattr(a, "id", "")),
                                str(getattr(b, "id", "")),
                            ),
                        )
                    )
                    logger.debug(
                        "Vocabulary drift: entries %r/%r summary_sim=%.2f content_sim=%.2f",
                        getattr(a, "id", "?"),
                        getattr(b, "id", "?"),
                        summary_ratio,
                        content_ratio,
                    )

        return findings


# ── Module-level helpers ──────────────────────────────────────────────────────


def propose_lint_findings(log: Any, report: KnowledgeLintReport) -> list[str]:
    """Convert significant lint findings into PDCA improvement proposals.

    Args:
        log: ImprovementLog instance to propose against.
        report: KnowledgeLintReport with findings to propose.

    Returns:
        List of improvement IDs that were proposed.

    Raises:
        Exception: If any proposal fails. Failures are never silently dropped
            and the function will not return a partial success list.
    """
    proposed: list[str] = []
    failures: list[tuple[str, Exception]] = []

    for finding in report.findings:
        if finding.severity not in ("error", "warning"):
            continue
        try:
            imp_id = log.propose(
                hypothesis=f"Fix knowledge lint: {finding.description}",
                metric="knowledge_quality",
                baseline=0.0,
                target=1.0,
                applied_by="knowledge_lint",
                rollback_plan="Revert knowledge entry to pre-lint state",
            )
            proposed.append(imp_id)
        except Exception as exc:
            failures.append((finding.finding_id, exc))
            logger.error(
                "Could not propose improvement for lint finding %s — finding will not "
                "enter the PDCA lifecycle; check ImprovementLog availability",
                finding.finding_id,
                exc_info=True,
            )

    if failures:
        failed_ids = ", ".join(fid for fid, _ in failures)
        logger.error(
            "Knowledge lint proposals failed for %d/%d findings: %s",
            len(failures),
            len([f for f in report.findings if f.severity in ("error", "warning")]),
            failed_ids,
        )
        # Re-raise the first failure to ensure automation gates fail
        _, first_exc = failures[0]
        raise first_exc

    return proposed


def _now_epoch_ms() -> int:
    """Return the current UTC time as epoch milliseconds.

    Returns:
        Current UTC timestamp in milliseconds since Unix epoch.
    """
    return int(datetime.now(timezone.utc).timestamp() * 1000)
