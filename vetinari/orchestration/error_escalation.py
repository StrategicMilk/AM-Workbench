"""Error Escalation — 4-level failure classification and recovery routing.

Classifies agent task failures into four escalation levels and recommends
recovery actions.  The ErrorClassifier uses regex patterns to distinguish
transient failures (retry same agent) from semantic failures (rephrase),
capability failures (reassign), and fatal failures (escalate to human).

This is the orchestration layer — it does NOT retry or escalate itself;
it produces a classification that the caller acts on.
"""

from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from vetinari.boundary_guards import route_enum_error
from vetinari.security.fail_closed import UntrustedInputError, sanitize_untrusted_text

logger = logging.getLogger(__name__)


# Generic fallback brief used when the LLM is unavailable — answers the minimum
# question a retrying agent needs: what to do differently.
_FALLBACK_RETRY_BRIEF = "Review the error and adjust approach."


class EscalationLevel(int, Enum):
    """Four-level escalation taxonomy for agent task failures.

    Higher values indicate more severe failures requiring more intervention.
    """

    TRANSIENT = 0  # Timeout, temp OOM, rate-limit — retry same agent
    SEMANTIC = 1  # Misunderstanding — rephrase prompt, retry same agent
    CAPABILITY = 2  # Wrong agent type — reassign to a more capable agent
    FATAL = 3  # Unsolvable by any agent — escalate to human


class ErrorKind(str, Enum):
    """Explicit error-kind routing for failures that must bypass retry branches."""

    POLICY_VIOLATION = "policy_violation"


@dataclass(frozen=True, slots=True)
class ErrorClassification:
    """Result of classifying an agent task failure.

    Args:
        level: The escalation level assigned to this failure.
        reason: Human-readable explanation of the classification.
        suggested_action: Recommended recovery action string.
        matched_pattern: The regex pattern that triggered classification,
            or empty string if classified by fallback heuristics.
        is_retryable: True if the failure is worth retrying at all.
    """

    level: EscalationLevel
    reason: str
    suggested_action: str = ""
    matched_pattern: str = ""
    is_retryable: bool = True

    def __repr__(self) -> str:
        return (
            f"ErrorClassification(level={self.level.name!r}, reason={self.reason!r}, retryable={self.is_retryable!r})"
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-safe dictionary.

        Returns:
            Dictionary with all classification fields.
        """
        return {
            "level": self.level.value,
            "level_name": self.level.name,
            "reason": self.reason,
            "suggested_action": self.suggested_action,
            "matched_pattern": self.matched_pattern,
            "is_retryable": self.is_retryable,
        }


@dataclass
class RecoveryMetrics:
    """Tracks recovery attempt statistics for a plan execution session.

    Args:
        attempts: Total error classification/recovery attempts.
        resolved: Attempts that led to successful resolution.
        by_level: Count of classifications at each escalation level.
    """

    attempts: int = 0
    resolved: int = 0
    by_level: dict[int, int] = field(default_factory=lambda: {0: 0, 1: 0, 2: 0, 3: 0})

    def resolution_rate(self) -> float:
        """Return the fraction of attempts that were resolved.

        Returns:
            Float in [0.0, 1.0], or 0.0 if no attempts recorded.
        """
        if self.attempts == 0:
            return 0.0
        return self.resolved / self.attempts

    def record(self, level: EscalationLevel, resolved: bool) -> None:
        """Record one classification result.

        Args:
            level: The EscalationLevel assigned to the failure.
            resolved: Whether the recovery was ultimately successful.
        """
        self.attempts += 1
        self.by_level[level.value] = self.by_level.get(level.value, 0) + 1
        if resolved:
            self.resolved += 1

    def __repr__(self) -> str:
        return (
            f"RecoveryMetrics(attempts={self.attempts!r}, "
            f"resolved={self.resolved!r}, "
            f"rate={self.resolution_rate():.2%})"
        )


# -- Classification patterns per escalation level --------------------------
# Patterns are compiled once at module load time (not per-call)

_TRANSIENT_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"timeout|timed.?out", re.IGNORECASE), "Request timed out — transient"),
    (re.compile(r"rate.?limit|too many request", re.IGNORECASE), "Rate limited — transient"),
    (re.compile(r"connection (refused|reset|aborted)", re.IGNORECASE), "Connection failure — transient"),
    (re.compile(r"out.?of.?memory|oom|memory error", re.IGNORECASE), "Memory pressure — transient"),
    (re.compile(r"temporarily unavailable|503|service unavailable", re.IGNORECASE), "Service unavailable — transient"),
]

_SEMANTIC_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"ambiguous|unclear|ambiguity", re.IGNORECASE), "Ambiguous request — rephrase prompt"),
    (re.compile(r"invalid (format|json|yaml|xml)", re.IGNORECASE), "Invalid format — clarify output spec"),
    (re.compile(r"missing (field|key|parameter|argument)", re.IGNORECASE), "Missing input — add to prompt"),
    (re.compile(r"cannot parse|parse error|syntax error", re.IGNORECASE), "Parse failure — rephrase"),
    (re.compile(r"contradictory|contradiction|conflicting", re.IGNORECASE), "Contradictory requirements — clarify"),
]

_CAPABILITY_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"not capable|capability|beyond.{0,30}scope", re.IGNORECASE), "Capability mismatch — reassign"),
    (re.compile(r"requires.{0,40}(specialist|expert|domain)", re.IGNORECASE), "Needs specialist agent — reassign"),
    (
        re.compile(r"tool not available|no tool|missing tool", re.IGNORECASE),
        "Missing tool — reassign to equipped agent",
    ),
    (re.compile(r"model.*not.*support|unsupported.*model", re.IGNORECASE), "Model limitation — reassign"),
    (re.compile(r"access denied|permission denied|forbidden", re.IGNORECASE), "Permission boundary — reassign"),
]

_FATAL_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"impossible|cannot be done|no solution", re.IGNORECASE), "Unsolvable — escalate to human"),
    (re.compile(r"infinite loop|circular|deadlock", re.IGNORECASE), "Circular dependency — escalate"),
    (re.compile(r"data corruption|corrupt(ed)? data", re.IGNORECASE), "Data integrity failure — escalate"),
    (re.compile(r"security violation|policy violation|forbidden action", re.IGNORECASE), "Policy violation — escalate"),
]


_POLICY_ERROR_PATTERN = re.compile(r"security violation|policy violation|forbidden action", re.IGNORECASE)


class ErrorClassifier:
    """Classify agent task errors into the four escalation levels.

    Patterns are matched in order of increasing severity: TRANSIENT first,
    then SEMANTIC, CAPABILITY, FATAL.  The first matching pattern wins.
    Unmatched errors default to TRANSIENT with a fallback reason so the
    orchestrator always has an action to take.

    Instance is stateless — safe to share across threads.
    """

    def _classify_explicit_level(self, explicit_level: Any) -> ErrorClassification | None:
        if explicit_level is None:
            return None
        try:
            level = EscalationLevel(int(explicit_level))
        except (ValueError, TypeError):
            logger.warning(
                "Context escalation_level %r is not a valid EscalationLevel integer"
                " - falling through to pattern-based classification",
                explicit_level,
            )
            return None
        return ErrorClassification(
            level=level,
            reason=f"Explicit escalation_level={explicit_level} in context",
            suggested_action=self._default_action(level),
            is_retryable=level < EscalationLevel.FATAL,
        )

    @staticmethod
    def _classification_for_patterns(
        text: str,
        patterns: list[tuple[re.Pattern[str], str]],
        level: EscalationLevel,
        action: str,
        *,
        retryable: bool,
    ) -> ErrorClassification | None:
        for pattern, reason in patterns:
            if pattern.search(text):
                return ErrorClassification(
                    level=level,
                    reason=reason,
                    suggested_action=action,
                    matched_pattern=pattern.pattern,
                    is_retryable=retryable,
                )
        return None

    @staticmethod
    def _fallback_classification(retry_count: Any) -> ErrorClassification:
        if retry_count > 2:
            return ErrorClassification(
                level=EscalationLevel.SEMANTIC,
                reason=f"Repeated failure (retry_count={retry_count}) with unrecognized error",
                suggested_action="Rephrase agent prompt - repeated failure suggests misunderstanding",
                is_retryable=True,
            )
        return ErrorClassification(
            level=EscalationLevel.TRANSIENT,
            reason="Unrecognized error pattern - treating as transient on first occurrence",
            suggested_action="Retry same agent with unchanged prompt",
            is_retryable=True,
        )

    def classify(self, error_message: str, context: dict[str, Any] | None = None) -> ErrorClassification:
        """Classify an error message into an escalation level.

        Args:
            error_message: The error string from an agent task failure.
            context: Optional dict with extra context (e.g. retry_count,
                agent_type) that may influence classification.

        Returns:
            ErrorClassification with the assigned level and recovery action.
        """
        context = context or {}
        if not isinstance(error_message, str) or not error_message.strip():
            return ErrorClassification(
                level=EscalationLevel.TRANSIENT,
                reason="Empty error message - treating as transient",
                suggested_action="Retry same agent with unchanged prompt",
                is_retryable=True,
            )
        try:
            text = sanitize_untrusted_text(error_message, max_length=5000)
        except UntrustedInputError as exc:
            logger.warning("Rejected unsafe error message during escalation classification: %s", exc)
            return ErrorClassification(
                level=EscalationLevel.FATAL,
                reason=f"Unsafe error message rejected: {exc}",
                suggested_action="Escalate to human operator - unsafe retry context",
                is_retryable=False,
            )

        explicit = self._classify_explicit_level(context.get("escalation_level"))
        if explicit is not None:
            return explicit
        if _POLICY_ERROR_PATTERN.search(text):
            route_enum_error(
                ErrorKind,
                ErrorKind.POLICY_VIOLATION.value,
                correct_blocker=ErrorKind.POLICY_VIOLATION,
                label="policy_error",
            )
            return ErrorClassification(
                level=EscalationLevel.FATAL,
                reason="Policy violation - escalate",
                suggested_action="Escalate to human operator - agent cannot resolve this",
                matched_pattern=_POLICY_ERROR_PATTERN.pattern,
                is_retryable=False,
            )

        checks = (
            (
                _TRANSIENT_PATTERNS,
                EscalationLevel.TRANSIENT,
                "Retry same agent with unchanged prompt after brief delay",
                True,
            ),
            (_SEMANTIC_PATTERNS, EscalationLevel.SEMANTIC, "Rephrase or clarify the agent prompt and retry", True),
            (
                _CAPABILITY_PATTERNS,
                EscalationLevel.CAPABILITY,
                "Reassign task to a more capable or specialist agent",
                True,
            ),
            (_FATAL_PATTERNS, EscalationLevel.FATAL, "Escalate to human operator - agent cannot resolve this", False),
        )
        for patterns, level, action, retryable in checks:
            classification = self._classification_for_patterns(text, patterns, level, action, retryable=retryable)
            if classification is not None:
                return classification

        return self._fallback_classification(context.get("retry_count", 0))

    @staticmethod
    def _default_action(level: EscalationLevel) -> str:
        """Return the standard recovery action for a given level.

        Args:
            level: The escalation level.

        Returns:
            Human-readable action string.
        """
        actions = {
            EscalationLevel.TRANSIENT: "Retry same agent",
            EscalationLevel.SEMANTIC: "Rephrase prompt and retry",
            EscalationLevel.CAPABILITY: "Reassign to specialist agent",
            EscalationLevel.FATAL: "Escalate to human operator",
        }
        return actions.get(level, "Unknown")


# -- Singleton ----------------------------------------------------------------

_classifier_instance: ErrorClassifier | None = None
_classifier_lock = threading.Lock()


def get_error_classifier() -> ErrorClassifier:
    """Return the singleton ErrorClassifier, constructing it if necessary.

    Uses double-checked locking for thread safety.

    Returns:
        The shared ErrorClassifier instance.
    """
    global _classifier_instance
    if _classifier_instance is None:
        with _classifier_lock:
            if _classifier_instance is None:
                _classifier_instance = ErrorClassifier()
                logger.debug("[ErrorClassifier] Singleton created")
    return _classifier_instance


# -- Retry brief --------------------------------------------------------------


def generate_retry_brief(failure_context: str, agent_type: str) -> str:
    """Generate a brief with 3 specific changes for retrying a failed agent task.

    Calls the LLM with the failure context and agent type to produce an
    actionable numbered list of 3 changes the retrying agent should make.
    The brief is intended to be injected into the retrying agent's prompt so
    it does not repeat the same mistake.

    Falls back to ``_FALLBACK_RETRY_BRIEF`` when the LLM is unavailable or
    returns an unusable response, so callers always receive a non-empty string.

    Args:
        failure_context: The error message or failure description from the
            previous attempt.
        agent_type: The type of agent that failed (e.g. "worker", "foreman").
            Used to give the LLM agent-specific context.

    Returns:
        A non-empty string containing the retry brief.  When the LLM is
        available this is a numbered list of 3 specific changes.  When the
        LLM is unavailable this is the generic fallback string.
    """
    try:
        from vetinari.llm_helpers import generate_retry_brief as _llm_brief

        safe_failure_context = sanitize_untrusted_text(failure_context, max_length=5000)
        safe_agent_type = sanitize_untrusted_text(agent_type, max_length=120) if agent_type else ""
        context_prompt = safe_failure_context
        if safe_agent_type:
            context_prompt = f"Agent type: {safe_agent_type}\nFailure: {safe_failure_context}"

        result = _llm_brief(error_description=context_prompt)
        if result and result.strip():
            logger.debug(
                "LLM retry brief generated for agent_type=%s (%d chars)",
                agent_type,
                len(result),
            )
            return result.strip()

        logger.debug(
            "LLM returned empty retry brief for agent_type=%s — using fallback",
            agent_type,
        )
        return _FALLBACK_RETRY_BRIEF

    except Exception as exc:
        logger.warning(
            "Could not generate LLM retry brief for agent_type=%s — using fallback",
            agent_type,
            exc_info=exc,
        )
        return _FALLBACK_RETRY_BRIEF
