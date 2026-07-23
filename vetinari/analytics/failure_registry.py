"""Failure registry — append-only JSONL store for pipeline failures and prevention rules.

Closes the kaizen feedback loop: every pipeline failure (Inspector rejection,
model timeout, anomaly detection hit) is logged here with structured root-cause
data.  After repeated failures in the same category, prevention rules are
auto-generated and fed back to the Inspector to catch known patterns early.

This is step 5 of the analytics pipeline:
Inference → Orchestration → Quality Gate → Failure Classification → **Failure Registry** → Prevention.
"""

from __future__ import annotations

import logging
import re
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from vetinari.analytics.failure_registry_remediation import FailureRegistryRemediationMixin
from vetinari.analytics.failure_registry_rules import FailureRegistryRulesMixin
from vetinari.analytics.failure_registry_storage import FailureRegistryStorageError, FailureRegistryStorageMixin
from vetinari.analytics.failure_registry_storage import _load_jsonl_dicts as _load_jsonl_dicts
from vetinari.analytics.failure_registry_storage import _rotating_jsonl_store as _rotating_jsonl_store
from vetinari.workbench.cost.token_cost_split import load_rotation_settings as load_rotation_settings

logger = logging.getLogger(__name__)


# ── Configuration ────────────────────────────────────────────────────────────

# Lazy-initialized to avoid Path.home() at import time
_REGISTRY_DIR: Path | None = None
_REGISTRY_USER_DIR: Path | None = None
_RULES_DIR: Path | None = None
_REGISTRY_DIR_LOCK = threading.RLock()

# Minimum failures in the same category before auto-generating a prevention rule
_RULE_GENERATION_THRESHOLD = 3
_FAILURE_REGISTRY_ROTATION_KEY = "failure_registry_jsonl"
_PREVENTION_RULES_ROTATION_KEY = "prevention_rules_jsonl"


def _get_registry_dir() -> Path:
    """Return the failure registry data directory, resolving lazily via get_user_dir().

    Uses get_user_dir() so tests can override the location via VETINARI_USER_DIR
    without the directory being pinned at import time.
    """
    global _REGISTRY_DIR, _REGISTRY_USER_DIR
    with _REGISTRY_DIR_LOCK:
        from vetinari.constants import get_user_dir

        current = get_user_dir()
        if _REGISTRY_DIR is not None and _REGISTRY_USER_DIR is None:
            return _REGISTRY_DIR
        if _REGISTRY_DIR is None or current != _REGISTRY_USER_DIR:
            _REGISTRY_DIR = current
            _REGISTRY_USER_DIR = current
        return _REGISTRY_DIR


def _registry_path() -> Path:
    """Return the path to the failure registry JSONL file."""
    return _get_registry_dir() / "failure-registry.jsonl"


def _rules_path() -> Path:
    """Return the path to the prevention rules JSONL file."""
    return _get_registry_dir() / "prevention-rules.jsonl"


# ── Enums ────────────────────────────────────────────────────────────────────


class PreventionRuleType(Enum):
    """Types of prevention rules generated from failure patterns."""

    PATTERN = "pattern"  # Regex-based pattern match on output
    SEMANTIC = "semantic"  # Structural check (e.g., missing error handling)
    EXTRACTED = "extracted"  # Distilled from failure history analysis


class FailureStatus(Enum):
    """Lifecycle status of a failure registry entry."""

    ACTIVE = "active"
    RESOLVED = "resolved"
    DEPRECATED = "deprecated"


# ── Dataclasses ──────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class FailureRegistryEntry:
    """Immutable record of a single pipeline failure.

    Attributes:
        failure_id: Unique identifier prefixed with ``fail_``.
        timestamp: ISO-8601 UTC timestamp of the failure.
        category: Failure category (e.g., ``"inspector_rejection"``).
        severity: One of ``"warning"``, ``"error"``, ``"critical"``.
        description: Human-readable description of what failed.
        root_cause: Root cause analysis text.
        affected_components: List of component names impacted by the failure.
        prevention_rule: Prevention rule text if one was generated.
        status: Lifecycle status (active, resolved, deprecated).
    """

    failure_id: str
    timestamp: str
    category: str
    severity: str
    description: str
    root_cause: str = ""
    affected_components: list[str] = field(default_factory=list)
    prevention_rule: str = ""
    status: str = "active"

    def __repr__(self) -> str:
        return (
            f"FailureRegistryEntry(id={self.failure_id!r}, category={self.category!r}, "
            f"severity={self.severity!r}, status={self.status!r})"
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-compatible dictionary.

        Returns:
            Plain dict with all entry fields as JSON-serializable values.
        """
        return asdict(self)


@dataclass(frozen=True, slots=True)
class PreventionRule:
    """A rule generated from repeated failure patterns to prevent recurrence.

    Attributes:
        rule_id: Unique identifier prefixed with ``prev_``.
        rule_type: Type of rule (pattern, semantic, or extracted).
        category: Failure category this rule guards against.
        pattern: Regex pattern, structural check description, or extracted heuristic.
        description: Human-readable explanation of what the rule catches.
        created_from_failures: List of failure_ids that triggered this rule's creation.
        created_at: ISO-8601 UTC timestamp of rule creation.
    """

    rule_id: str
    rule_type: str
    category: str
    pattern: str
    description: str
    created_from_failures: list[str] = field(default_factory=list)
    created_at: str = ""

    def __repr__(self) -> str:
        return f"PreventionRule(id={self.rule_id!r}, type={self.rule_type!r}, category={self.category!r})"

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-compatible dictionary.

        Returns:
            Plain dict with all rule fields as JSON-serializable values.
        """
        return asdict(self)

    def matches(self, text: str) -> bool:
        """Check whether this rule's pattern matches the given text.

        For PATTERN rules, applies regex matching. For SEMANTIC and EXTRACTED
        rules, checks whether the pattern string appears as a substring
        (case-insensitive).

        Args:
            text: The output text to check against the rule.

        Returns:
            True if the rule matches.
        """
        if self.rule_type == PreventionRuleType.PATTERN.value:
            try:
                return bool(re.search(self.pattern, text, re.IGNORECASE))
            except re.error:
                logger.warning(
                    "Invalid regex in prevention rule %s — skipping: %s",
                    self.rule_id,
                    self.pattern,
                )
                return False
        # Semantic and extracted rules: substring match
        return self.pattern.lower() in text.lower()


# ── FailureRegistry ─────────────────────────────────────────────────────────


class FailureRegistry(
    FailureRegistryRulesMixin,
    FailureRegistryStorageMixin,
    FailureRegistryRemediationMixin,
):
    """Append-only registry of pipeline failures with prevention rule generation.

    Thread-safe. Writes each failure as a single JSONL line. Prevention rules
    are auto-generated when ``_RULE_GENERATION_THRESHOLD`` failures accumulate
    in the same category.

    Side effects:
        - Writes to ``~/.vetinari/failure-registry.jsonl`` on every ``log_failure()`` call
        - Writes to ``~/.vetinari/prevention-rules.jsonl`` when rules are generated
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # In-memory cache of generated rules for fast lookup during inspection
        self._rules_cache: list[PreventionRule] | None = None
        self._rules_cache_lock = threading.Lock()

    # -- Write operations ------------------------------------------------------

    def log_failure(
        self,
        category: str,
        severity: str,
        description: str,
        root_cause: str = "",
        affected_components: list[str] | None = None,
        prevention_rule: str = "",
    ) -> FailureRegistryEntry:
        """Log a pipeline failure to the append-only registry.

        Creates a new entry with a unique ID and appends it as a single JSON
        line to the registry file. After logging, checks whether repeated
        failures in the same category should trigger prevention rule generation.

        Args:
            category: Failure category (e.g., ``"inspector_rejection"``,
                ``"model_timeout"``, ``"anomaly_detected"``).
            severity: One of ``"warning"``, ``"error"``, ``"critical"``.
            description: Human-readable description of the failure.
            root_cause: Root cause analysis text.
            affected_components: List of component names impacted.
            prevention_rule: Prevention rule text if already known.

        Returns:
            The newly created FailureRegistryEntry.
        """
        components = list(affected_components) if affected_components is not None else []
        entry = FailureRegistryEntry(
            failure_id=f"fail_{uuid.uuid4().hex[:12]}",
            timestamp=datetime.now(timezone.utc).isoformat(),
            category=category,
            severity=severity,
            description=description,
            root_cause=root_cause,
            affected_components=components,
            prevention_rule=prevention_rule,
            status=FailureStatus.ACTIVE.value,
        )

        with self._lock:
            self._append_entry(entry)

        logger.info(
            "Failure logged — id=%s category=%s severity=%s: %s",
            entry.failure_id,
            category,
            severity,
            description[:120],
        )

        # Check if this category has enough failures to generate a prevention rule
        self.check_and_generate_prevention_rules(category)

        return entry

    def resolve_failure(self, failure_id: str) -> bool:
        """Mark a failure as resolved by rewriting its status in the registry.

        Loads all entries, updates the matching entry's status to ``"resolved"``,
        and rewrites the file. This is an infrequent operation so the full
        rewrite is acceptable.

        Args:
            failure_id: The failure_id to resolve.

        Returns:
            True if the failure was found and resolved, False if not found.
        """
        with self._lock:
            entries = self._load_all_entries()
            found = False
            updated: list[dict[str, Any]] = []

            for entry_dict in entries:
                if entry_dict.get("failure_id") == failure_id:
                    entry_dict["status"] = FailureStatus.RESOLVED.value
                    found = True
                updated.append(entry_dict)

            if found:
                try:
                    self._rewrite_entries(updated)
                    logger.info("Failure %s resolved", failure_id)
                except FailureRegistryStorageError:
                    logger.error(
                        "Failure %s resolution could not be persisted; registry file left unchanged", failure_id
                    )

            return found

    # -- Read operations -------------------------------------------------------

    def get_failures(
        self,
        category: str | None = None,
        since: float | None = None,
    ) -> list[FailureRegistryEntry]:
        """Load and filter failure entries from the registry file.

        Args:
            category: When set, only entries with this category are returned.
            since: When set, only entries after this Unix timestamp are returned.

        Returns:
            List of matching FailureRegistryEntry instances.

        Raises:
            ValueError: If a stored registry row cannot be converted into a
                FailureRegistryEntry.
        """
        raw_entries = self._load_all_entries()
        results: list[FailureRegistryEntry] = []

        for entry_dict in raw_entries:
            if category and entry_dict.get("category") != category:
                continue
            if since:
                try:
                    entry_ts = datetime.fromisoformat(entry_dict.get("timestamp", "")).timestamp()
                    if entry_ts < since:
                        continue
                except (ValueError, TypeError):
                    # Unparseable timestamp — include the entry rather than silently dropping it
                    logger.debug("Could not parse timestamp for entry — including unfiltered")

            try:
                results.append(
                    FailureRegistryEntry(**{
                        k: v for k, v in entry_dict.items() if k in FailureRegistryEntry.__dataclass_fields__
                    })
                )
            except TypeError as exc:
                failure_id = entry_dict.get("failure_id", "unknown")
                raise ValueError(f"malformed failure registry entry: {failure_id}") from exc

        return results

    def get_prevention_rules(self) -> list[PreventionRule]:
        """Load all prevention rules from the dedicated rules file.

        Returns cached rules if available, otherwise loads from disk.

        Returns:
            List of PreventionRule instances.
        """
        with self._rules_cache_lock:
            if self._rules_cache is not None:
                return list(self._rules_cache)

        rules = self._load_rules_from_disk()
        with self._rules_cache_lock:
            self._rules_cache = rules
        return list(rules)

    def reset(self) -> None:
        """Clear all in-memory state. Intended for test isolation only."""
        with self._rules_cache_lock:
            self._rules_cache = None


# ── Singleton management ─────────────────────────────────────────────────────

_registry: FailureRegistry | None = None
_registry_lock = threading.Lock()


def get_failure_registry() -> FailureRegistry:
    """Return the process-wide FailureRegistry singleton (thread-safe).

    Uses double-checked locking so the lock is only acquired during
    first initialization.

    Returns:
        The shared FailureRegistry instance.
    """
    global _registry
    if _registry is None:
        with _registry_lock:
            if _registry is None:
                _registry = FailureRegistry()
    return _registry


def reset_failure_registry() -> None:
    """Destroy the singleton so the next call creates a fresh instance.

    Resets both the registry singleton and the cached registry directory so
    that tests can change VETINARI_USER_DIR between calls and get a clean slate.

    Intended for test isolation only.
    """
    global _registry, _REGISTRY_DIR, _REGISTRY_USER_DIR
    with _registry_lock:
        if _registry is not None:
            _registry.reset()
        _registry = None
        with _REGISTRY_DIR_LOCK:
            _REGISTRY_DIR = None
            _REGISTRY_USER_DIR = None
