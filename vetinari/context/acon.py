"""ACON — Adaptive Context Optimization Network.

Failure-driven context compression that learns which information types must
be preserved.  When compressed context leads to a task failure that full
context would have avoided, the compression rules are updated to preserve
that information category in future compressions.

This is step between Planning and Execution in the pipeline:
Intake → Planning → **Context Compression** → Execution → Quality Gate → Assembly.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from vetinari.constants import VETINARI_STATE_DIR
from vetinari.learning.atomic_writers import write_json_atomic

logger = logging.getLogger(__name__)


# Decision: ACON failure-driven compression preserves critical context types (ADR-pending)


class ACONRulesError(RuntimeError):
    """Raised when governed ACON compression rules cannot be trusted."""


class InfoCategory(Enum):
    """Categories of information that can appear in agent context."""

    TASK_DESCRIPTION = "task_description"
    DEPENDENCIES = "dependencies"
    CONSTRAINTS = "constraints"
    PRIOR_RESULTS = "prior_results"
    ERROR_HISTORY = "error_history"
    CODE_CONTEXT = "code_context"
    USER_PREFERENCES = "user_preferences"
    MODEL_METADATA = "model_metadata"
    QUALITY_CRITERIA = "quality_criteria"
    SYSTEM_STATE = "system_state"


@dataclass(frozen=True, slots=True)
class CompressionRule:
    """A rule governing whether an information category should be preserved.

    Attributes:
        category: The information category this rule governs.
        preserve: Whether to preserve this category during compression.
        priority: Higher priority categories are preserved first (0-100).
        reason: Why this rule exists (e.g., "failure on task X at 2026-04-14").
        created_at: When this rule was created or last updated.
    """

    category: InfoCategory
    preserve: bool
    priority: int
    reason: str
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def __repr__(self) -> str:
        """Return a concise string representation of the compression rule."""
        return f"CompressionRule(category={self.category.value!r}, preserve={self.preserve}, priority={self.priority})"


@dataclass
class CompressionOutcome:
    """Records the result of using compressed vs full context.

    Attributes:
        task_id: Identifier for the task that was executed.
        compressed_success: Whether the task succeeded with compressed context.
        full_context_hash: Hash of the full context for comparison.
        compressed_context_hash: Hash of the compressed context.
        removed_categories: Which information categories were removed.
        failure_reason: If compressed context failed, why.
    """

    task_id: str
    compressed_success: bool
    full_context_hash: str
    compressed_context_hash: str
    removed_categories: list[InfoCategory] = field(default_factory=list)
    failure_reason: str | None = None

    def __repr__(self) -> str:
        """Return a concise string representation of the compression outcome."""
        return f"CompressionOutcome(task_id={self.task_id!r}, compressed_success={self.compressed_success}, removed_categories={len(self.removed_categories)})"


class ACONCompressor:
    """Adaptive context compressor that learns from failures.

    Maintains a set of compression rules that evolve based on observed
    outcomes.  Categories that cause failures when removed are marked
    as must-preserve with increased priority.

    Args:
        rules_path: Path to persist compression rules as JSON.
        max_history: Maximum number of outcomes to retain for analysis.
    """

    def __init__(
        self,
        rules_path: Path | None = None,
        max_history: int = 100,
    ) -> None:
        self._rules_path = rules_path or VETINARI_STATE_DIR / "acon_rules.json"
        self._max_history = max_history
        self._rules: dict[InfoCategory, CompressionRule] = {}
        self._history: deque[CompressionOutcome] = deque(maxlen=max_history)
        self._lock = threading.Lock()
        self._load_rules()

    def _load_rules(self) -> None:
        """Load compression rules from disk, merging over defaults.

        Starts from defaults so any category not present in the persisted file
        still has a rule.  Loaded entries are overlaid on top, so persisted
        updates (e.g. learned must-preserve flags) take precedence.
        """
        self._init_default_rules()
        if not self._rules_path.exists():
            return
        try:
            with open(self._rules_path, encoding="utf-8") as f:
                data = json.load(f)
            for entry in data.get("rules", []):
                cat = InfoCategory(entry["category"])
                self._rules[cat] = CompressionRule(
                    category=cat,
                    preserve=entry["preserve"],
                    priority=entry["priority"],
                    reason=entry.get("reason", "loaded from disk"),
                    created_at=entry.get("created_at", ""),
                )
            logger.info(
                "ACON loaded %d compression rules from %s",
                len(self._rules),
                self._rules_path,
            )
        except Exception as exc:
            raise ACONRulesError(f"failed to load ACON rules from {self._rules_path}") from exc

    def _init_default_rules(self) -> None:
        """Initialize default compression rules — preserve critical categories."""
        defaults = {
            InfoCategory.TASK_DESCRIPTION: (True, 100, "Core task identity — always required"),
            InfoCategory.CONSTRAINTS: (True, 90, "Constraints prevent invalid outputs"),
            InfoCategory.QUALITY_CRITERIA: (True, 85, "Quality gates need criteria to evaluate"),
            InfoCategory.DEPENDENCIES: (True, 80, "Task ordering requires dependency awareness"),
            InfoCategory.ERROR_HISTORY: (True, 75, "Error patterns prevent repeat failures"),
            InfoCategory.CODE_CONTEXT: (False, 60, "Can be re-derived from codebase"),
            InfoCategory.PRIOR_RESULTS: (False, 50, "Summaries sufficient for most tasks"),
            InfoCategory.USER_PREFERENCES: (False, 40, "Low priority for compression savings"),
            InfoCategory.MODEL_METADATA: (False, 30, "Derivable from model registry"),
            InfoCategory.SYSTEM_STATE: (False, 20, "Ephemeral — often stale by execution time"),
        }
        for cat, (preserve, priority, reason) in defaults.items():
            self._rules[cat] = CompressionRule(
                category=cat,
                preserve=preserve,
                priority=priority,
                reason=reason,
            )

    def _save_rules(self) -> None:
        """Write current compression rules to disk as JSON."""
        try:
            self._rules_path.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "rules": [
                    {
                        "category": rule.category.value,
                        "preserve": rule.preserve,
                        "priority": rule.priority,
                        "reason": rule.reason,
                        "created_at": rule.created_at,
                    }
                    for rule in sorted(self._rules.values(), key=lambda r: -r.priority)
                ],
            }
            write_json_atomic(self._rules_path, data)
        except Exception as exc:
            raise ACONRulesError(f"failed to save ACON rules to {self._rules_path}") from exc

    def compress(self, context: dict[str, Any]) -> dict[str, Any]:
        """Compress context by removing non-essential information categories.

        Removes categories marked as non-preserved, ordered by lowest priority
        first.  Categories marked as must-preserve are always retained.

        Args:
            context: Full context dict with categorized information.

        Returns:
            Compressed context dict with non-essential categories removed.
        """
        with self._lock:
            compressed = {}
            removed = []
            for key, value in context.items():
                try:
                    cat = InfoCategory(key)
                except ValueError:
                    # Unknown category — preserve by default
                    logger.warning(
                        "Unknown context category %r — preserving in compressed output",
                        key,
                    )
                    compressed[key] = value
                    continue

                rule = self._rules.get(cat)
                if rule and not rule.preserve:
                    removed.append(cat)
                    logger.debug(
                        "ACON: removing category %s (priority=%d)",
                        cat.value,
                        rule.priority,
                    )
                else:
                    compressed[key] = value

            if removed:
                compressed["_acon_removed"] = [c.value for c in removed]
                compressed["_acon_full_hash"] = self._hash_context(context)
                compressed["_acon_compressed_hash"] = self._hash_context(compressed)

            return compressed

    def record_outcome(self, outcome: CompressionOutcome) -> None:
        """Record the outcome of a task executed with compressed context.

        If the task failed and categories were removed, updates rules to
        preserve those categories in future compressions.

        Args:
            outcome: The compression outcome to record.
        """
        with self._lock:
            self._history.append(outcome)

            if not outcome.compressed_success and outcome.removed_categories:
                self._update_rules_from_failure(outcome)

    def _update_rules_from_failure(self, outcome: CompressionOutcome) -> None:
        """Update compression rules based on a failure caused by compression.

        Marks all removed categories as must-preserve and increases their
        priority.  This is the core learning mechanism.

        Args:
            outcome: The failed compression outcome.
        """
        reason = f"Failure on task {outcome.task_id}: {outcome.failure_reason or 'compressed context insufficient'}"
        for cat in outcome.removed_categories:
            old_rule = self._rules.get(cat)
            new_priority = min(100, (old_rule.priority if old_rule else 50) + 15)
            self._rules[cat] = CompressionRule(
                category=cat,
                preserve=True,
                priority=new_priority,
                reason=reason,
            )
            logger.info(
                "ACON: category %s promoted to must-preserve (priority=%d) after failure on task %s",
                cat.value,
                new_priority,
                outcome.task_id,
            )
        self._save_rules()

    def get_compression_stats(self) -> dict[str, Any]:
        """Return current compression statistics.

        Returns:
            Dict with rule counts, history size, and preservation rates.
        """
        with self._lock:
            preserved = sum(1 for r in self._rules.values() if r.preserve)
            total = len(self._rules)
            successes = sum(1 for o in self._history if o.compressed_success)
            failures = sum(1 for o in self._history if not o.compressed_success)
            return {
                "total_categories": total,
                "preserved_categories": preserved,
                "compressible_categories": total - preserved,
                "history_size": len(self._history),
                "success_rate": successes / (successes + failures) if (successes + failures) > 0 else 1.0,
                "failure_count": failures,
            }

    @staticmethod
    def _hash_context(context: dict[str, Any]) -> str:
        """Generate a stable hash of context content for comparison.

        Args:
            context: Context dict to hash.

        Returns:
            Hex digest of the context hash.
        """
        content = json.dumps(context, sort_keys=True, default=str)
        return hashlib.sha256(content.encode()).hexdigest()[:16]


# -- Module-level singleton ---------------------------------------------------

# Who writes: get_acon_compressor() (lazy initialisation on first call)
# Who reads: any module needing failure-driven context compression
# Lifecycle: created once per process; lazily initialised
# Lock: double-checked locking protects simultaneous first-callers
_instance: ACONCompressor | None = None
_instance_lock = threading.Lock()


def get_acon_compressor(rules_path: Path | None = None) -> ACONCompressor:
    """Get or create the ACON compressor singleton.

    Uses double-checked locking per project quality gates.

    Args:
        rules_path: Optional custom path for rules persistence.

    Returns:
        The shared ACONCompressor instance.
    """
    global _instance
    if _instance is None:
        with _instance_lock:
            if _instance is None:
                _instance = ACONCompressor(rules_path=rules_path)
    return _instance


__all__ = [
    "ACONCompressor",
    "CompressionOutcome",
    "CompressionRule",
    "InfoCategory",
    "get_acon_compressor",
]
