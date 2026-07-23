"""Self-Evolving Instruction Architecture — 4-tier mutability system.

Agent instructions evolve automatically based on execution outcomes. Four tiers
of mutability ensure safety while enabling continuous improvement:

1. Constitutional (IMMUTABLE): Safety rules, ethical constraints — hash-verified, human-only changes
2. Structural (HUMAN-APPROVAL): Architecture decisions, role definitions — system proposes, human gates
3. Behavioral (AUTO-EVOLVE + VERIFY): Task instructions, prompt templates — auto-modify with verification
4. Tactical (FREELY EVOLVE): Few-shot examples, style preferences — evolve without gate

Decision: 4-tier mutability architecture for self-evolving instructions.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from vetinari.boundary_guards import assert_dependency_success, require_nonempty
from vetinari.database import get_connection

logger = logging.getLogger(__name__)


class InstructionTier(Enum):
    """Mutability tier for an instruction component."""

    CONSTITUTIONAL = "constitutional"  # Immutable, hash-verified
    STRUCTURAL = "structural"  # Human approval required
    BEHAVIORAL = "behavioral"  # Auto-evolve with verification
    TACTICAL = "tactical"  # Freely evolvable


@dataclass
class InstructionRule:
    """A versioned instruction rule with evidence tracking."""

    rule_id: str
    tier: str  # InstructionTier.value string
    content: str
    agent_type: str = "all"  # Which agent(s) this rule applies to
    source: str = "manual"  # manual, evolved, transferred
    evidence_count: int = 0  # Times this rule improved outcomes
    last_triggered: str | None = None
    effectiveness_score: float = 0.5
    content_hash: str = ""  # SHA-256 for constitutional tier verification
    version: int = 1
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    status: str = "active"  # active, quarantined

    def __repr__(self) -> str:
        return f"InstructionRule(rule_id={self.rule_id!r}, tier={self.tier!r}, agent_type={self.agent_type!r})"

    def compute_hash(self) -> str:
        """Compute SHA-256 hash of rule content for integrity verification.

        Returns:
            Hex-encoded SHA-256 digest of the rule content.
        """
        return hashlib.sha256(self.content.encode("utf-8")).hexdigest()


_CREATE_TABLE_SQL = """
    CREATE TABLE IF NOT EXISTS instruction_rules (
        rule_id TEXT PRIMARY KEY,
        tier TEXT NOT NULL,
        content TEXT NOT NULL,
        agent_type TEXT DEFAULT 'all',
        source TEXT DEFAULT 'manual',
        evidence_count INTEGER DEFAULT 0,
        last_triggered TEXT,
        effectiveness_score REAL DEFAULT 0.5,
        content_hash TEXT,
        version INTEGER DEFAULT 1,
        created_at TEXT,
        status TEXT DEFAULT 'active'
    )
"""

# Migration: add the status column to databases created before this column existed.
_MIGRATE_STATUS_COLUMN_SQL = """
    ALTER TABLE instruction_rules ADD COLUMN status TEXT DEFAULT 'active'
"""


class InstructionEvolver:
    """Manages the evolution of agent instructions across four mutability tiers.

    Constitutional rules are hash-verified at startup. Behavioral and tactical
    rules evolve based on execution outcomes. Structural changes require human
    approval. Cross-agent transfer propagates generally-applicable rules.

    Side effects:
      - Creates instruction_rules table in unified SQLite DB on first init
      - Verifies constitutional rule integrity at init (logs WARNING on tampering)
    """

    # Rules untriggered for this many days are flagged for review
    RULE_DECAY_DAYS = 30

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._init_db()
        self._verify_constitutional_integrity()

    def _init_db(self) -> None:
        """Create the instruction_rules table if it doesn't exist.

        Also runs a migration to add the ``status`` column to databases
        created before that column was introduced.
        """
        conn = get_connection()
        conn.execute(_CREATE_TABLE_SQL)
        # Idempotent migration: silently ignore the error when the column
        # already exists (SQLite raises OperationalError in that case).
        with contextlib.suppress(Exception):
            conn.execute(_MIGRATE_STATUS_COLUMN_SQL)
        conn.commit()

    def add_rule(
        self,
        content: str,
        tier: InstructionTier,
        agent_type: str = "all",
        source: str = "manual",
        allow_constitutional: bool = False,
        verification_receipt: dict[str, Any] | None = None,
    ) -> str:
        """Add a new instruction rule and persist it to the database.

        Constitutional rules require explicit opt-in via ``allow_constitutional``
        to prevent accidental privilege escalation.  Callers that legitimately
        need to add a constitutional rule must pass ``allow_constitutional=True``
        — this is an intentional friction point to make escalation visible.

        Args:
            content: The rule text.
            tier: Mutability tier.
            agent_type: Which agent(s) this applies to.
            source: How this rule was created (manual, evolved, transferred).
            allow_constitutional: Set True only when the caller explicitly
                intends to add an immutable constitutional rule.
            verification_receipt: Independent verification metadata required
                before persisting evolved behavioral rules.

        Returns:
            The generated rule_id.

        Raises:
            PermissionError: If ``tier`` is CONSTITUTIONAL and
                ``allow_constitutional`` is False.
        """
        content = require_nonempty(content, field_name="rule_body")
        if tier == InstructionTier.CONSTITUTIONAL and not allow_constitutional:
            raise PermissionError(
                f"Adding a CONSTITUTIONAL rule requires allow_constitutional=True. "
                f"Call add_rule(..., allow_constitutional=True) only when the rule "
                f"truly must be immutable and cannot be evolved. Attempted content: "
                f"{content[:80]!r}"
            )
        if tier == InstructionTier.CONSTITUTIONAL:
            receipt = verification_receipt if isinstance(verification_receipt, dict) else {}
            dependency_id = str(receipt.get("dependency_id") or "constitutional_rule_dependency")
            failed_ids = receipt.get("failed_dependency_ids")
            if failed_ids is not None:
                assert_dependency_success(dependency_id, failed_ids)
        if (
            tier == InstructionTier.BEHAVIORAL
            and source == "evolved"
            and not _is_independent_verification_receipt(verification_receipt, agent_type=agent_type)
        ):
            raise PermissionError(
                "Adding an evolved BEHAVIORAL rule requires an independent verification receipt "
                "from a verifier that is not the evolving agent."
            )
        rule_id = f"rule_{uuid.uuid4().hex[:8]}"
        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()

        with self._lock:
            conn = get_connection()
            conn.execute(
                """INSERT INTO instruction_rules
                   (rule_id, tier, content, agent_type, source, content_hash, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    rule_id,
                    tier.value,
                    content,
                    agent_type,
                    source,
                    content_hash,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            conn.commit()

        logger.info(
            "[InstructionEvolver] Added %s rule %s for %s",
            tier.value,
            rule_id,
            agent_type,
        )
        return rule_id

    def get_rules(
        self,
        agent_type: str = "all",
        tier: InstructionTier | None = None,
    ) -> list[InstructionRule]:
        """Get active rules for an agent type.

        Returns rules scoped to the given agent type as well as universal
        rules (agent_type = 'all').

        Args:
            agent_type: Agent type to filter by (also includes 'all' rules).
            tier: Optional tier filter.

        Returns:
            List of matching InstructionRule instances.
        """
        # Exclude quarantined rules — they failed the integrity check and must not
        # influence agent behaviour until the violation is resolved.
        query = (
            "SELECT * FROM instruction_rules WHERE (agent_type = ? OR agent_type = 'all') AND status != 'quarantined'"
        )
        params: list[Any] = [agent_type]
        if tier:
            query += " AND tier = ?"
            params.append(tier.value)

        conn = get_connection()
        cursor = conn.execute(query, params)
        columns = [desc[0] for desc in cursor.description]
        rows = cursor.fetchall()

        return [InstructionRule(**dict(zip(columns, row))) for row in rows]

    def evolve_behavioral(
        self,
        agent_type: str,
        quality_score: float,
        trace: dict[str, Any] | None = None,
    ) -> str | None:
        """Attempt to evolve a behavioral rule based on execution outcome.

        Only evolves if quality is below threshold, indicating room for improvement.
        Uses PromptOptimizer trace analysis when a trace is provided.

        Args:
            agent_type: Agent that produced the outcome.
            quality_score: Quality score of the execution (0.0-1.0).
            trace: Optional execution trace for diagnosis.

        Returns:
            New rule_id if a rule was evolved, None if quality is acceptable
            or no improvement was found.
        """
        if quality_score >= 0.7:
            return None  # Good enough — no evolution needed

        try:
            from vetinari.learning.prompt_optimizer import get_prompt_optimizer

            optimizer = get_prompt_optimizer()

            if trace:
                exp = optimizer.optimize_via_trace(
                    agent_type=agent_type,
                    baseline_instruction="",
                    failed_trace=trace,
                )
                if exp and exp.avg_quality > quality_score:
                    rule_id = self.add_rule(
                        content=exp.instruction,
                        tier=InstructionTier.BEHAVIORAL,
                        agent_type=agent_type,
                        source="evolved",
                        verification_receipt=_verification_receipt_from_trace(trace, agent_type=agent_type),
                    )
                    return rule_id
        except Exception:
            logger.warning(
                "Behavioral evolution failed for %s — non-fatal, skipping this cycle",
                agent_type,
            )

        return None

    def record_trigger(self, rule_id: str, effectiveness: float) -> None:
        """Record that a rule was triggered and update its running effectiveness.

        Uses exponential moving average (alpha=0.2) to weight recent
        observations without discarding historical signal.

        Args:
            rule_id: The rule that was triggered.
            effectiveness: Quality improvement attributed to this rule (0.0-1.0).
        """
        with self._lock:
            conn = get_connection()
            conn.execute(
                """UPDATE instruction_rules
                   SET evidence_count = evidence_count + 1,
                       last_triggered = ?,
                       effectiveness_score = (effectiveness_score * 0.8) + (? * 0.2)
                   WHERE rule_id = ?""",
                (datetime.now(timezone.utc).isoformat(), effectiveness, rule_id),
            )
            conn.commit()

    @staticmethod
    def _get_rule_by_id(rule_id: str) -> InstructionRule | None:
        """Look up a single rule directly by its primary key.

        Args:
            rule_id: The rule primary key.

        Returns:
            The InstructionRule, or None if not found.
        """
        conn = get_connection()
        cursor = conn.execute("SELECT * FROM instruction_rules WHERE rule_id = ?", (rule_id,))
        columns = [desc[0] for desc in cursor.description]
        row = cursor.fetchone()
        if row is None:
            return None
        return InstructionRule(**dict(zip(columns, row)))

    def transfer_rule(self, rule_id: str, target_agent_type: str) -> str | None:
        """Transfer a rule to another agent type (cross-agent learning).

        Copies the rule content into a new rule scoped to the target agent.
        Constitutional rules are copied as behavioral to prevent bypassing
        the integrity check on the original.

        Args:
            rule_id: Source rule to transfer.
            target_agent_type: Agent type to receive the rule.

        Returns:
            New rule_id for the transferred rule, or None if source not found.
        """
        source_rule = self._get_rule_by_id(rule_id)
        if not source_rule:
            logger.warning(
                "[InstructionEvolver] Transfer failed — rule %s not found",
                rule_id,
            )
            return None

        # Constitutional rules may not be transferred verbatim; demote to behavioral
        target_tier = InstructionTier(source_rule.tier)
        if target_tier == InstructionTier.CONSTITUTIONAL:
            target_tier = InstructionTier.BEHAVIORAL

        return self.add_rule(
            content=source_rule.content,
            tier=target_tier,
            agent_type=target_agent_type,
            source=f"transferred_from:{rule_id}",
        )

    def _verify_constitutional_integrity(self) -> list[str]:
        """Verify that constitutional rules haven't been tampered with.

        Logs an ERROR and quarantines (sets status='quarantined') each rule
        whose stored hash no longer matches its current content.  Quarantined
        rules remain in the database for forensic review but are excluded from
        active rule lookups until the integrity issue is resolved.

        Returns:
            List of rule_ids that were quarantined in this call.  An empty list
            means all constitutional rules passed the integrity check.
        """
        conn = get_connection()
        cursor = conn.execute(
            "SELECT * FROM instruction_rules WHERE tier = ? AND status != 'quarantined'",
            (InstructionTier.CONSTITUTIONAL.value,),
        )
        columns = [desc[0] for desc in cursor.description]
        rules = [InstructionRule(**dict(zip(columns, row))) for row in cursor.fetchall()]
        quarantined: list[str] = []
        for rule in rules:
            expected_hash = rule.content_hash
            actual_hash = rule.compute_hash()
            if expected_hash and expected_hash != actual_hash:
                logger.error(
                    "[InstructionEvolver] INTEGRITY VIOLATION: constitutional rule %s "
                    "has been modified! Expected hash %s, got %s — quarantining rule "
                    "until the integrity issue is resolved",
                    rule.rule_id,
                    expected_hash[:12],
                    actual_hash[:12],
                )
                try:
                    conn = get_connection()
                    conn.execute(
                        "UPDATE instruction_rules SET status = 'quarantined' WHERE rule_id = ?",
                        (rule.rule_id,),
                    )
                    conn.commit()
                    quarantined.append(rule.rule_id)
                except Exception:
                    logger.error(
                        "[InstructionEvolver] Failed to quarantine tampered rule %s — "
                        "rule remains active; immediate database inspection required",
                        rule.rule_id,
                        exc_info=True,
                    )
        return quarantined

    def get_stale_rules(self, days: int | None = None) -> list[InstructionRule]:
        """Get rules that haven't been triggered recently and may need review.

        Constitutional rules never decay and are excluded from this list.

        Args:
            days: Staleness threshold in days. Defaults to RULE_DECAY_DAYS (30).

        Returns:
            List of stale rules that may need review or removal.
        """
        days = days or self.RULE_DECAY_DAYS
        all_rules = self.get_rules()
        stale: list[InstructionRule] = []
        cutoff = datetime.now(timezone.utc).timestamp() - (days * 86400)

        for rule in all_rules:
            if rule.tier == InstructionTier.CONSTITUTIONAL.value:
                continue  # Constitutional rules never decay

            if rule.last_triggered:
                triggered_ts = datetime.fromisoformat(rule.last_triggered).timestamp()
                if triggered_ts < cutoff:
                    stale.append(rule)
            elif rule.created_at:
                created_ts = datetime.fromisoformat(rule.created_at).timestamp()
                if created_ts < cutoff:
                    stale.append(rule)

        return stale


# Module-level singleton — written by get_instruction_evolver(), read by callers.
# Protected by _evolver_lock (double-checked locking).
def _is_independent_verification_receipt(
    receipt: dict[str, Any] | None,
    *,
    agent_type: str,
) -> bool:
    """Return True only for receipts from an independent verifier with a pass verdict."""
    if not isinstance(receipt, dict):
        return False
    verdict = str(receipt.get("verdict", "")).strip().lower()
    verifier = str(receipt.get("verifier", "")).strip()
    subject_agent = str(receipt.get("subject_agent", "")).strip()
    evidence_ref = str(receipt.get("evidence_ref", "")).strip()
    if verdict not in {"pass", "passed", "approved"}:
        return False
    if not verifier or verifier == agent_type:
        return False
    if subject_agent and subject_agent != agent_type:
        return False
    return bool(evidence_ref)


def _verification_receipt_from_trace(
    trace: dict[str, Any] | None,
    *,
    agent_type: str,
) -> dict[str, Any] | None:
    """Extract independent verification metadata from a trace payload."""
    if not isinstance(trace, dict):
        return None
    raw = trace.get("verification_receipt") or trace.get("independent_verification")
    if isinstance(raw, dict):
        receipt = dict(raw)
    else:
        receipt = {}
        verifier = trace.get("verifier") or trace.get("inspector_agent")
        verdict = trace.get("verification_verdict") or trace.get("inspector_verdict")
        evidence_ref = trace.get("verification_evidence_ref") or trace.get("evidence_ref")
        if isinstance(verdict, dict):
            verdict = verdict.get("verdict") or verdict.get("status")
        if verifier:
            receipt["verifier"] = verifier
        if verdict:
            receipt["verdict"] = verdict
        if evidence_ref:
            receipt["evidence_ref"] = evidence_ref
    receipt.setdefault("subject_agent", agent_type)
    return receipt


_evolver: InstructionEvolver | None = None
_evolver_lock = threading.Lock()


def get_instruction_evolver() -> InstructionEvolver:
    """Return the singleton InstructionEvolver instance (thread-safe).

    Uses double-checked locking to avoid unnecessary synchronization on
    the hot path once the instance is created.

    Returns:
        The shared InstructionEvolver instance.
    """
    global _evolver
    if _evolver is None:
        with _evolver_lock:
            if _evolver is None:
                _evolver = InstructionEvolver()
    return _evolver
