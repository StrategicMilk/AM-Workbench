"""Prevention rule generation helpers for the failure registry facade."""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from vetinari.analytics.failure_registry import FailureRegistryEntry, PreventionRule

logger = logging.getLogger(__name__)

_PREVENTION_RULE_STOP_WORDS = frozenset({
    "the",
    "a",
    "an",
    "is",
    "was",
    "were",
    "are",
    "be",
    "been",
    "to",
    "of",
    "in",
    "for",
    "on",
    "with",
    "at",
    "by",
    "from",
    "and",
    "or",
    "not",
    "no",
    "but",
    "this",
    "that",
    "it",
})


class FailureRegistryRulesMixin:
    """Prevention rule generation behavior for FailureRegistry."""

    _rules_cache: Any
    _rules_cache_lock: Any
    _save_rule: Callable[[PreventionRule], None]
    get_failures: Callable[..., list[FailureRegistryEntry]]
    get_prevention_rules: Callable[[], list[PreventionRule]]

    def check_and_generate_prevention_rules(self, category: str) -> PreventionRule | None:
        """Check if a category has enough failures to generate a prevention rule.

        Groups active failures by category and generates a rule when the count
        reaches ``_RULE_GENERATION_THRESHOLD``. Skips categories that already
        have a prevention rule.

        Args:
            category: The failure category to check.

        Returns:
            The newly generated PreventionRule, or None if threshold not met.
        """
        from vetinari.analytics.failure_registry import _RULE_GENERATION_THRESHOLD, FailureStatus

        entries = self.get_failures(category=category)
        active = [e for e in entries if e.status == FailureStatus.ACTIVE.value]

        if len(active) < _RULE_GENERATION_THRESHOLD:
            return None

        existing_rules = self.get_prevention_rules()
        if any(r.category == category for r in existing_rules):
            return None

        rule = self._generate_rule(category, active)
        if rule:
            self._save_rule(rule)
            with self._rules_cache_lock:
                self._rules_cache = None
            logger.info(
                "Prevention rule generated - id=%s category=%s from %d failures",
                rule.rule_id,
                category,
                len(active),
            )
        return rule

    @staticmethod
    def _generate_rule(
        category: str,
        failures: list[FailureRegistryEntry],
    ) -> PreventionRule | None:
        """Generate a prevention rule from a set of related failures.

        Extracts common patterns from failure descriptions. Uses PATTERN type
        for failures with identifiable regex patterns, SEMANTIC for structural
        issues, and EXTRACTED for general failure history analysis.

        Args:
            category: The failure category.
            failures: List of related failure entries.

        Returns:
            A PreventionRule, or None if no meaningful rule can be extracted.
        """
        from vetinari.analytics.failure_registry import PreventionRule, PreventionRuleType

        descriptions = [f.description for f in failures]
        failure_ids = [f.failure_id for f in failures]

        word_counts: dict[str, int] = {}
        for desc in descriptions:
            words = set(desc.lower().split()) - _PREVENTION_RULE_STOP_WORDS
            for word in words:
                word_counts[word] = word_counts.get(word, 0) + 1

        common = [w for w, c in word_counts.items() if c >= len(failures)]

        if not common:
            rule_type = PreventionRuleType.EXTRACTED.value
            pattern = f"Repeated {category} failures detected"
            description = (
                f"Auto-generated from {len(failures)} failures in category '{category}'. "
                f"Descriptions: {'; '.join(d[:80] for d in descriptions[:3])}"
            )
        elif any(kw in common for kw in ("missing", "lacking", "absent", "without", "no")):
            rule_type = PreventionRuleType.SEMANTIC.value
            pattern = " ".join(sorted(common)[:5])
            description = f"Output must not have: {pattern} (from {len(failures)} failures)"
        else:
            rule_type = PreventionRuleType.PATTERN.value
            pattern = "|".join(re.escape(w) for w in sorted(common)[:5])
            description = f"Pattern match for known failure: {pattern} (from {len(failures)} failures)"

        return PreventionRule(
            rule_id=f"prev_{uuid.uuid4().hex[:12]}",
            rule_type=rule_type,
            category=category,
            pattern=pattern,
            description=description,
            created_from_failures=failure_ids,
            created_at=datetime.now(timezone.utc).isoformat(),
        )
