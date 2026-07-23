"""Retry intelligence — failure-aware retry strategy selection.

Analyzes failure traces and error messages to determine whether a failure
matches a known pattern (with a proven fix) or is novel (requiring an LLM
retry brief).  Known patterns are resolved cheaply via the failure registry
and remediation stats; novel failures are flagged for LLM-assisted retry.

Pipeline role: consulted by ``TaskRetryLoopRunner`` before each retry attempt
to decide *how* to retry rather than blindly re-executing.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Any

from vetinari.boundary_guards import require_score_in_range

logger = logging.getLogger(__name__)
_PREVENTION_RULE_CONFIDENCE: float = 0.8  # Uncalibrated baseline tracked by FSA-8716.


@dataclass(frozen=True, slots=True)
class RetryStrategy:
    """Recommended retry approach for a failed task.

    Attributes:
        known: True if the failure matches a known pattern with a proven fix.
        fix_action: Description of the fix to apply (only when ``known=True``).
        confidence: Confidence in the fix (0.0-1.0), derived from remediation stats.
        llm_brief_needed: True if the failure is novel and needs LLM analysis.
        matching_rule_id: ID of the matching prevention rule, if any.
        failure_mode: Classified failure mode string, if identified.
        context: Additional context for the retry decision.
    """

    known: bool = False
    fix_action: str = ""
    confidence: float = 0.0
    llm_brief_needed: bool = False
    matching_rule_id: str = ""
    failure_mode: str = ""
    context: dict[str, Any] = field(default_factory=dict)

    def __repr__(self) -> str:
        if self.known:
            return f"RetryStrategy(known=True, fix={self.fix_action!r}, confidence={self.confidence:.2f})"
        return f"RetryStrategy(known=False, llm_brief_needed={self.llm_brief_needed})"


# Confidence threshold above which a known fix is applied without LLM
_AUTO_FIX_CONFIDENCE_THRESHOLD = 0.7


class RetryAnalyzer:
    """Analyzes failures and recommends retry strategies.

    Checks the failure registry for matching prevention rules and
    remediation stats.  High-confidence fixes are applied directly;
    novel failures are flagged for LLM-assisted retry.

    Side effects:
        - Reads from FailureRegistry (prevention rules, remediation stats).
        - No writes — purely advisory.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()

    def analyze(
        self,
        failure_trace: str,
        error_msg: str,
        task_type: str = "",
    ) -> RetryStrategy:
        """Analyze a failure and recommend a retry strategy.

        Checks three sources in order:
        1. Prevention rules — regex/semantic match on the error text.
        2. Remediation stats — high-confidence fixes for known failure modes.
        3. Fallback — flag as novel failure needing LLM retry brief.

        Args:
            failure_trace: Full traceback or failure trace string.
            error_msg: The error message from the failed attempt.
            task_type: Optional task type for context-aware matching.

        Returns:
            RetryStrategy with the recommended approach.
        """
        combined_text = f"{error_msg}\n{failure_trace}"

        # 1. Check prevention rules for pattern match
        rule_strategy = self._check_prevention_rules(combined_text)
        if rule_strategy is not None:
            return rule_strategy

        # 2. Check remediation stats for high-confidence fixes
        remediation_strategy = self._check_remediation_stats(combined_text, task_type)
        if remediation_strategy is not None:
            return remediation_strategy

        # 3. Novel failure — needs LLM analysis
        logger.info(
            "No known pattern for failure (error=%s) — flagging for LLM retry brief",
            error_msg[:120],
        )
        return RetryStrategy(
            known=False,
            llm_brief_needed=True,
            context={"error_msg": error_msg[:500], "task_type": task_type},
        )

    @staticmethod
    def _check_prevention_rules(text: str) -> RetryStrategy | None:
        """Check failure text against registered prevention rules.

        Args:
            text: Combined error message and trace to match against.

        Returns:
            RetryStrategy if a matching rule is found, None otherwise.
        """
        try:
            from vetinari.analytics.failure_registry import get_failure_registry

            registry = get_failure_registry()
            rules = registry.get_prevention_rules()

            for rule in rules:
                if rule.matches(text):
                    logger.info(
                        "Failure matches prevention rule %s (category=%s)",
                        rule.rule_id,
                        rule.category,
                    )
                    return RetryStrategy(
                        known=True,
                        fix_action=f"Apply prevention rule: {rule.description}",
                        confidence=require_score_in_range(
                            _PREVENTION_RULE_CONFIDENCE,
                            label="prevention_rule_confidence",
                        ),
                        matching_rule_id=rule.rule_id,
                        context={"rule_category": rule.category},
                    )
        except Exception:
            logger.warning("Could not check prevention rules — proceeding to remediation stats")

        return None

    def _check_remediation_stats(
        self,
        text: str,
        task_type: str,
    ) -> RetryStrategy | None:
        """Check remediation stats for high-confidence fixes.

        Maps error text to known failure modes and checks whether proven
        remediation actions exist with sufficient confidence.

        Args:
            text: Combined error message and trace.
            task_type: Task type for context.

        Returns:
            RetryStrategy if a high-confidence fix exists, None otherwise.
        """
        # Map error text to failure modes
        failure_mode = self._classify_failure_mode(text)
        if not failure_mode:
            return None

        try:
            from vetinari.analytics.failure_registry import get_failure_registry

            registry = get_failure_registry()
            stats = registry.get_remediation_stats()

            # Find best action for this failure mode
            best_action = ""
            best_confidence = 0.0

            for (mode, action), counts in stats.items():
                if mode != failure_mode:
                    continue
                total = counts["success"] + counts["failure"]
                if total == 0:
                    continue
                confidence = counts["success"] / total
                if confidence > best_confidence:
                    best_confidence = confidence
                    best_action = action

            if best_action and best_confidence >= _AUTO_FIX_CONFIDENCE_THRESHOLD:
                logger.info(
                    "Found high-confidence fix for %s: %s (confidence=%.2f)",
                    failure_mode,
                    best_action,
                    best_confidence,
                )
                return RetryStrategy(
                    known=True,
                    fix_action=best_action,
                    confidence=best_confidence,
                    failure_mode=failure_mode,
                    context={"task_type": task_type},
                )

            # Known mode but low confidence — still flag as known but suggest LLM
            if best_action and best_confidence > 0.0:
                logger.info(
                    "Known failure mode %s but low confidence (%.2f) — suggesting LLM brief",
                    failure_mode,
                    best_confidence,
                )
                return RetryStrategy(
                    known=True,
                    fix_action=best_action,
                    confidence=best_confidence,
                    llm_brief_needed=True,
                    failure_mode=failure_mode,
                    context={"task_type": task_type},
                )

        except Exception:
            logger.warning("Could not check remediation stats — treating as novel failure")

        return None

    @staticmethod
    def _classify_failure_mode(text: str) -> str:
        """Classify error text into a known failure mode.

        Uses keyword matching on the combined error text to identify
        the failure mode. Returns empty string if no mode matches.

        Args:
            text: The combined error message and trace.

        Returns:
            Failure mode string (e.g. ``"oom"``, ``"hang"``), or ``""``
            if unclassifiable.
        """
        text_lower = text.lower()

        oom_keywords = ("out of memory", "oom", "cuda out of memory", "memory allocation failed", "mmap failed")
        if any(kw in text_lower for kw in oom_keywords):
            return "oom"

        hang_keywords = ("timeout", "timed out", "stalled", "no progress", "deadlock")
        if any(kw in text_lower for kw in hang_keywords):
            return "hang"

        quality_keywords = ("quality below", "quality_drop", "score too low", "verification failed")
        if any(kw in text_lower for kw in quality_keywords):
            return "quality_drop"

        disk_keywords = ("disk full", "no space left", "disk_full", "enospc")
        if any(kw in text_lower for kw in disk_keywords):
            return "disk_full"

        thermal_keywords = ("thermal", "throttling", "overheating", "temperature")
        if any(kw in text_lower for kw in thermal_keywords):
            return "thermal"

        return ""


# ── Singleton ────────────────────────────────────────────────────────────────

_analyzer: RetryAnalyzer | None = None
_analyzer_lock = threading.Lock()


def get_retry_analyzer() -> RetryAnalyzer:
    """Return the process-wide RetryAnalyzer singleton.

    Uses double-checked locking so the common read-path never acquires the lock.

    Returns:
        The singleton RetryAnalyzer instance.
    """
    global _analyzer
    if _analyzer is None:
        with _analyzer_lock:
            if _analyzer is None:
                _analyzer = RetryAnalyzer()
    return _analyzer


def reset_retry_analyzer() -> None:
    """Reset the singleton for test isolation."""
    global _analyzer
    with _analyzer_lock:
        _analyzer = None
