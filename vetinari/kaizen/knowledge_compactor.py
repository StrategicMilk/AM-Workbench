"""Knowledge compactor — compression ladder from episodes to rules.

Implements the knowledge compaction pipeline:
  Episodes (base entries) → Patterns → Principles → Rules

Each level clusters and generalizes the level below. Higher-level entries
set ``supersedes_id`` on the memories they replace, enabling the linter
to detect cross-level contradictions.

Pipeline role: Kaizen Check → **Knowledge Compaction** → memory cleanup.
"""

from __future__ import annotations

import difflib
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from vetinari.kaizen.knowledge_lint import LintFinding
from vetinari.memory.interfaces import MemoryEntry
from vetinari.types import MemoryType

logger = logging.getLogger(__name__)


# ── Constants ─────────────────────────────────────────────────────────────────

# Similarity thresholds for each compaction stage
_EPISODE_CLUSTER_THRESHOLD: float = 0.6  # Episodes → Patterns
_PATTERN_CLUSTER_THRESHOLD: float = 0.5  # Patterns → Principles
_CROSS_LEVEL_CONTRADICTION: float = 0.85  # Near-match across levels = contradiction

# Minimum cluster sizes to produce a higher-level entry
_MIN_EPISODES_PER_PATTERN: int = 3  # At least 3 episodes to form a pattern
_MIN_PATTERNS_PER_PRINCIPLE: int = 2  # At least 2 patterns to form a principle

# Minimum recall count for a principle to be codified as a rule
_MIN_RECALLS_FOR_RULE: int = 3

# Entry types that are produced by compaction — used to identify "episode" types
_COMPACTION_LEVELS: set[MemoryType] = {
    MemoryType.PATTERN,
    MemoryType.PRINCIPLE,
    MemoryType.RULE,
}
_LEVEL_RANK: dict[MemoryType, int] = {
    MemoryType.PATTERN: 1,
    MemoryType.PRINCIPLE: 2,
    MemoryType.RULE: 3,
}


# ── Report dataclass ──────────────────────────────────────────────────────────


@dataclass(slots=True)
class CompactionReport:
    """Result of a full compaction pass over a set of memory entries.

    Attributes:
        patterns_created: New PATTERN entries produced from episode clusters.
        principles_created: New PRINCIPLE entries produced from pattern clusters.
        rules_created: New RULE entries produced from confirmed principles.
        contradictions: Cross-level contradiction findings from the linter.
        input_entries: Total number of entries passed to :meth:`KnowledgeCompactor.compact`.
        timestamp: ISO-8601 UTC timestamp when the report was created.
    """

    patterns_created: list[MemoryEntry] = field(default_factory=list)
    principles_created: list[MemoryEntry] = field(default_factory=list)
    rules_created: list[MemoryEntry] = field(default_factory=list)
    contradictions: list[LintFinding] = field(default_factory=list)
    input_entries: int = 0
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(),
    )

    def __repr__(self) -> str:
        """Compact representation showing counts of produced entries and contradictions."""
        return (
            f"CompactionReport(patterns={len(self.patterns_created)}, "
            f"principles={len(self.principles_created)}, "
            f"rules={len(self.rules_created)}, "
            f"contradictions={len(self.contradictions)})"
        )


# ── Compactor ─────────────────────────────────────────────────────────────────


class KnowledgeCompactor:
    """Compresses memory entries up the abstraction ladder: episodes → patterns → principles → rules.

    Each stage clusters similar entries at the current level and emits a single
    higher-level entry that generalises the cluster.  The new entry carries a
    ``supersedes_id`` pointing at the first member of its source cluster,
    creating a fact-graph chain that the linter can follow.
    """

    def __init__(self) -> None:
        """Initialise a stateless KnowledgeCompactor."""

    # ── Stage 1: Episodes → Patterns ─────────────────────────────────────────

    def extract_patterns(self, episodes: list[MemoryEntry]) -> list[MemoryEntry]:
        """Cluster similar episodes and emit a PATTERN entry for each large cluster.

        Uses ``difflib.SequenceMatcher`` to measure pairwise content similarity.
        A cluster must contain at least 3 episodes before a pattern is produced.
        Each pattern sets ``supersedes_id`` to the ``id`` of the first episode in
        the cluster and summarises the cluster content.

        Args:
            episodes: Episode-level memory entries (entries whose ``entry_type``
                is not in ``_COMPACTION_LEVELS``).

        Returns:
            A list of new PATTERN-type MemoryEntry objects, one per qualifying
            cluster.  May be empty if no cluster reaches the minimum size.
        """
        clusters = _cluster_entries(episodes, _EPISODE_CLUSTER_THRESHOLD)
        patterns: list[MemoryEntry] = []

        for cluster in clusters:
            if len(cluster) < _MIN_EPISODES_PER_PATTERN:
                continue

            representative = cluster[0]
            pattern_id = f"mem_{uuid.uuid4().hex[:8]}"
            content = f"{representative.content} [pattern across {len(cluster)} episodes]"
            entry = MemoryEntry(
                id=pattern_id,
                agent=representative.agent,
                entry_type=MemoryType.PATTERN,
                content=content,
                summary=f"Pattern derived from {len(cluster)} similar episodes",
                provenance="knowledge_compactor",
                supersedes_id=representative.id,
                source_entry_ids=[entry.id for entry in cluster],
            )
            patterns.append(entry)
            logger.debug(
                "Pattern %r created from %d episodes (first=%r)",
                pattern_id,
                len(cluster),
                representative.id,
            )

        return patterns

    # ── Stage 2: Patterns → Principles ───────────────────────────────────────

    def synthesize_principles(self, patterns: list[MemoryEntry]) -> list[MemoryEntry]:
        """Group similar patterns and emit a PRINCIPLE entry for each qualifying group.

        Uses a similarity threshold of 0.5.  Groups with at least 2 patterns
        produce one PRINCIPLE entry whose content is prefixed with ``"Principle: "``
        and whose ``supersedes_id`` is the first pattern in the group.

        Args:
            patterns: PATTERN-type memory entries, typically produced by
                :meth:`extract_patterns`.

        Returns:
            A list of new PRINCIPLE-type MemoryEntry objects.  May be empty if
            no group reaches the minimum size.
        """
        clusters = _cluster_entries(patterns, _PATTERN_CLUSTER_THRESHOLD)
        principles: list[MemoryEntry] = []

        for cluster in clusters:
            if len(cluster) < _MIN_PATTERNS_PER_PRINCIPLE:
                continue

            representative = cluster[0]
            principle_id = f"mem_{uuid.uuid4().hex[:8]}"
            content = f"Principle: {representative.content}"
            entry = MemoryEntry(
                id=principle_id,
                agent=representative.agent,
                entry_type=MemoryType.PRINCIPLE,
                content=content,
                summary=f"Principle synthesized from {len(cluster)} patterns",
                provenance="knowledge_compactor",
                supersedes_id=representative.id,
                source_entry_ids=_lineage_ids(cluster),
            )
            principles.append(entry)
            logger.debug(
                "Principle %r synthesized from %d patterns (first=%r)",
                principle_id,
                len(cluster),
                representative.id,
            )

        return principles

    # ── Stage 3: Principles → Rules ───────────────────────────────────────────

    def codify_rules(self, principles: list[MemoryEntry]) -> list[MemoryEntry]:
        """Promote confirmed principles to RULE entries.

        A principle is "confirmed" when its ``recall_count`` is at least
        ``_MIN_RECALLS_FOR_RULE`` (3).  Each qualifying principle produces one
        RULE entry whose content is prefixed with ``"Rule: "`` and whose
        ``supersedes_id`` points back to the source principle.

        Args:
            principles: PRINCIPLE-type memory entries to evaluate.

        Returns:
            A list of new RULE-type MemoryEntry objects, one per confirmed
            principle.  May be empty if no principle has sufficient recall.
        """
        rules: list[MemoryEntry] = []

        for principle in principles:
            if principle.recall_count < _MIN_RECALLS_FOR_RULE:
                continue

            rule_id = f"mem_{uuid.uuid4().hex[:8]}"
            content = f"Rule: {principle.content}"
            entry = MemoryEntry(
                id=rule_id,
                agent=principle.agent,
                entry_type=MemoryType.RULE,
                content=content,
                summary=f"Rule codified from principle {principle.id!r}",
                provenance="knowledge_compactor",
                supersedes_id=principle.id,
                source_entry_ids=_lineage_ids([principle]),
            )
            rules.append(entry)
            logger.debug(
                "Rule %r codified from principle %r (recall_count=%d)",
                rule_id,
                principle.id,
                principle.recall_count,
            )

        return rules

    # ── Cross-level contradiction check ───────────────────────────────────────

    def check_cross_level_contradictions(
        self,
        entries: list[MemoryEntry],
    ) -> list[LintFinding]:
        """Detect lower-level entries that nearly-match a higher-level entry but differ.

        For each higher-level entry (PATTERN, PRINCIPLE, or RULE), compares its
        content against every lower-level entry using
        ``difflib.SequenceMatcher``.  A pair where similarity exceeds
        ``_CROSS_LEVEL_CONTRADICTION`` (0.85) but content is not identical
        constitutes a contradiction finding.

        Args:
            entries: All memory entries to inspect, across any mix of types.

        Returns:
            A list of :class:`~vetinari.kaizen.knowledge_lint.LintFinding`
            objects with category ``"contradiction"`` and severity
            ``"warning"``.
        """
        findings: list[LintFinding] = []

        for high_entry, low_entry in _cross_level_pairs(entries):
            high_content = (high_entry.content or "").lower().strip()
            if not high_content:
                continue

            low_content = (low_entry.content or "").lower().strip()
            if not low_content:
                continue

            ratio = difflib.SequenceMatcher(None, high_content, low_content).ratio()

            if ratio > _CROSS_LEVEL_CONTRADICTION and high_content != low_content:
                finding_id = f"lint_{uuid.uuid4().hex[:8]}"
                findings.append(
                    LintFinding(
                        finding_id=finding_id,
                        category="contradiction",
                        description=(
                            f"Higher-level entry {high_entry.id!r} "
                            f"({high_entry.entry_type.value}) "
                            f"and lower-level entry {low_entry.id!r} "
                            f"({low_entry.entry_type.value}) "
                            f"share {ratio:.0%} content similarity but differ - "
                            "possible cross-level contradiction"
                        ),
                        severity="warning",
                        entry_ids=(high_entry.id, low_entry.id),
                    )
                )
                logger.debug(
                    "Cross-level contradiction: %r vs %r similarity=%.2f",
                    high_entry.id,
                    low_entry.id,
                    ratio,
                )

        return findings

    # ── Full pipeline ─────────────────────────────────────────────────────────

    def compact(self, entries: list[MemoryEntry]) -> CompactionReport:
        """Run the full compression ladder over a collection of memory entries.

        Filters episodes from the input, then runs:
        1. :meth:`extract_patterns` — episodes → patterns
        2. :meth:`synthesize_principles` — patterns → principles
        3. :meth:`codify_rules` — principles → rules
        4. :meth:`check_cross_level_contradictions` — detects cross-level issues
           across the original entries plus all newly created entries.

        Args:
            entries: All memory entries to compact.  Entries at PATTERN,
                PRINCIPLE, or RULE level are passed through to the contradiction
                check but are not re-processed as episodes.

        Returns:
            A :class:`CompactionReport` summarising what was created and what
            contradictions were found.
        """
        report = CompactionReport(input_entries=len(entries))

        episodes = [e for e in entries if e.entry_type not in _COMPACTION_LEVELS]

        logger.debug(
            "Compaction started: %d total entries, %d episodes",
            len(entries),
            len(episodes),
        )

        # Stage 1
        report.patterns_created = self.extract_patterns(episodes)

        # Stage 2
        report.principles_created = self.synthesize_principles(report.patterns_created)

        # Stage 3
        report.rules_created = self.codify_rules(report.principles_created)

        # Contradiction check across all entries + newly created entries
        all_entries = entries + report.patterns_created + report.principles_created + report.rules_created
        report.contradictions = self.check_cross_level_contradictions(all_entries)

        logger.debug(
            "Compaction complete: patterns=%d, principles=%d, rules=%d, contradictions=%d",
            len(report.patterns_created),
            len(report.principles_created),
            len(report.rules_created),
            len(report.contradictions),
        )

        return report


# ── Helpers ───────────────────────────────────────────────────────────────────


def _cluster_entries(
    entries: list[MemoryEntry],
    threshold: float,
) -> list[list[MemoryEntry]]:
    """Group entries into similarity clusters using greedy single-link clustering.

    Each unassigned entry seeds a new cluster.  Subsequent unassigned entries
    are added to the first cluster whose representative content exceeds
    *threshold* similarity.  This is O(n²) but the entry counts involved in
    compaction are small (typically < 1000).

    Args:
        entries: Memory entries to cluster.
        threshold: Minimum ``SequenceMatcher`` ratio to consider two entries
            similar enough to belong to the same cluster.

    Returns:
        A list of clusters; each cluster is a non-empty list of MemoryEntry.
    """
    clusters: list[list[MemoryEntry]] = []
    assigned: list[bool] = [False] * len(entries)

    for i, entry in enumerate(entries):
        if assigned[i]:
            continue

        cluster = [entry]
        assigned[i] = True
        content_i = (entry.content or "").lower().strip()

        for j in range(i + 1, len(entries)):
            if assigned[j]:
                continue

            content_j = (entries[j].content or "").lower().strip()
            ratio = difflib.SequenceMatcher(None, content_i, content_j).ratio()
            if ratio >= threshold:
                cluster.append(entries[j])
                assigned[j] = True

        clusters.append(cluster)

    return clusters


def _entry_rank(entry: MemoryEntry) -> int:
    return _LEVEL_RANK.get(entry.entry_type, 0)


def _cross_level_pairs(entries: list[MemoryEntry]) -> list[tuple[MemoryEntry, MemoryEntry]]:
    pairs: list[tuple[MemoryEntry, MemoryEntry]] = []
    for index, left in enumerate(entries):
        for right in entries[index + 1 :]:
            left_rank = _entry_rank(left)
            right_rank = _entry_rank(right)
            if left_rank == right_rank:
                continue
            pairs.append((left, right) if left_rank > right_rank else (right, left))
    return pairs


def _lineage_ids(entries: list[MemoryEntry]) -> list[str]:
    lineage: list[str] = []
    for entry in entries:
        if entry.id and entry.id not in lineage:
            lineage.append(entry.id)
        for source_id in entry.source_entry_ids:
            if source_id not in lineage:
                lineage.append(source_id)
    return lineage


def run_compaction_step() -> CompactionReport:
    """Run knowledge compaction as a standalone PDCA step.

    Fetches all memory entries and runs the compaction pipeline.
    Handles exceptions internally so callers need no try/except.

    Returns:
        CompactionReport summarizing what was compacted.
    """
    try:
        from vetinari.memory.unified import UnifiedMemoryStore

        entries = UnifiedMemoryStore().search("", limit=10_000)
        return KnowledgeCompactor().compact(entries)
    except Exception as exc:
        logger.warning("Knowledge compaction failed — returning empty report: %s", exc)
        return CompactionReport()
