"""System 1 / System 2 routing — cognitive dual-process task classification.

System 1 (fast): Simple, familiar tasks → direct Worker inference, skip Foreman
planning, smallest adequate model. Inspector bypass allowed only when strict
safety rules are met (US-001 truth table).

System 2 (slow): Complex, novel tasks → full Foreman→Worker→Inspector pipeline,
full planning and verification, most capable model.

This is pipeline stage 0.1, immediately after intake classification (stage 0).
Intake tier and complexity classification feed into this decision.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


# -- Constants --

# Safety-critical operations that MUST go through System 2 (from US-001 truth table)
_SAFETY_CRITICAL_PATTERNS = frozenset({
    "security",
    "auth",
    "credential",
    "password",
    "token",
    "deploy",
    "production",
    "database",
    "migration",
    "delete",
    "remove all",
    "drop",
    "truncate",
    "git push",
    "force push",
})

_MIN_PRIOR_SUCCESSES = 3  # Minimum prior successes before Inspector bypass allowed
_HIGH_CONFIDENCE_THRESHOLD = 0.85  # Confidence required for System 1
_LOW_CONFIDENCE_THRESHOLD = 0.6  # Below this, always System 2

# -- Enums --


class SystemType(Enum):
    """Dual-process routing classification."""

    SYSTEM_1 = "system_1"  # Fast/automatic: small model, skip planning
    SYSTEM_2 = "system_2"  # Slow/deliberate: full pipeline, most capable model


class ModelTier(Enum):
    """Model capability tier selected by the system router."""

    SMALL = "small"  # Fastest, lowest cost — for simple familiar tasks
    MEDIUM = "medium"  # Balanced — for standard tasks
    LARGE = "large"  # Most capable — for complex novel tasks


# -- Dataclasses --


@dataclass(frozen=True, slots=True)
class InspectorBypassCheck:
    """Result of the US-001 truth table safety check for Inspector bypass.

    All conditions must be satisfied simultaneously; any single failure blocks bypass.
    """

    allowed: bool  # True only when ALL safety conditions are met
    reason: str  # Human-readable explanation of the decision
    safety_flags: tuple[str, ...] = ()  # Which safety patterns triggered denial


@dataclass(frozen=True, slots=True)
class SystemDecision:
    """Complete routing decision produced by route_system().

    Encodes which pipeline stages to engage, which model tier to use,
    and the reasoning behind the decision.
    """

    system_type: SystemType  # System 1 (fast) or System 2 (deliberate)
    model_tier: ModelTier  # Capability tier to select
    skip_foreman: bool  # True → bypass Foreman planning stage
    skip_inspector: bool  # True → bypass Inspector verification stage
    inspector_bypass: InspectorBypassCheck  # Safety check detail
    reasoning: str  # Human-readable routing rationale
    routing_signals: dict[str, Any] = field(default_factory=dict)  # Raw signal data

    def __repr__(self) -> str:
        """Show key routing fields without dumping the full signal dict."""
        return (
            f"SystemDecision("
            f"system_type={self.system_type.value!r}, "
            f"model_tier={self.model_tier.value!r}, "
            f"skip_foreman={self.skip_foreman}, "
            f"skip_inspector={self.skip_inspector})"
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dict suitable for storing in pipeline context.

        Returns:
            Dictionary with all routing fields using primitive types.
        """
        return {
            "system_type": self.system_type.value,
            "model_tier": self.model_tier.value,
            "skip_foreman": self.skip_foreman,
            "skip_inspector": self.skip_inspector,
            "inspector_bypass": {
                "allowed": self.inspector_bypass.allowed,
                "reason": self.inspector_bypass.reason,
                "safety_flags": list(self.inspector_bypass.safety_flags),
            },
            "reasoning": self.reasoning,
            "routing_signals": self.routing_signals,
        }


# -- Module-level tracking state --
# Protected by _decisions_lock; keeps last 1000 entries to bound memory usage.
_system_decisions: list[dict[str, Any]] = []
_decisions_lock = threading.Lock()
_MAX_TRACKED_DECISIONS = 1000


# -- Private helpers --


def _get_historical_success_rate(description: str) -> float:
    """Query the meta-adapter for historical success rate on similar tasks.

    Gracefully degrades — returns -1.0 when the meta-adapter is unavailable
    so that callers can treat -1.0 as "no signal available".

    Args:
        description: Task description to look up.

    Returns:
        Success rate between 0.0 and 1.0, or -1.0 if unavailable.
    """
    try:
        from vetinari.learning.meta_adapter import get_meta_adapter

        adapter = get_meta_adapter()
        result = adapter.select_strategy(description)
        # select_strategy returns a strategy dict; extract confidence if present
        if isinstance(result, dict):
            return float(result.get("confidence", -1.0))
        return -1.0
    except Exception:
        logger.warning("Meta-adapter unavailable for historical success rate lookup — returning -1.0")
        return -1.0


def _check_skill_library_match(description: str) -> bool:
    """Check whether the skill library has a matching skill for this description.

    Gracefully degrades — returns False when the skill registry is unavailable.

    Args:
        description: Task description to search for.

    Returns:
        True if a matching skill exists, False otherwise or on failure.
    """
    try:
        from vetinari.skills.registry import get_skill_registry

        registry = get_skill_registry()
        # Registry lookup: check if any skill matches the description keywords
        desc_lower = description.lower()
        skills = registry.list_skills() if hasattr(registry, "list_skills") else []
        for skill in skills:
            skill_name = (skill.get("name") or "") if isinstance(skill, dict) else str(skill)
            if skill_name.lower() in desc_lower or desc_lower in skill_name.lower():
                return True
        return False
    except Exception:
        logger.warning("Skill registry unavailable for skill match lookup — returning False")
        return False


def _track_system_decision(decision: SystemDecision, description: str) -> None:
    """Append a routing decision to the in-memory tracking list.

    Thread-safe. Keeps at most _MAX_TRACKED_DECISIONS entries by dropping
    the oldest when the list grows past the limit.

    Args:
        decision: The routing decision to record.
        description: Task description (first 80 chars stored).
    """
    entry: dict[str, Any] = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "system_type": decision.system_type.value,
        "model_tier": decision.model_tier.value,
        "skip_foreman": decision.skip_foreman,
        "skip_inspector": decision.skip_inspector,
        "description_prefix": description[:80],
    }
    with _decisions_lock:
        _system_decisions.append(entry)
        if len(_system_decisions) > _MAX_TRACKED_DECISIONS:
            del _system_decisions[: len(_system_decisions) - _MAX_TRACKED_DECISIONS]


# -- Public API --


class SystemRouter:
    """Configurable dual-process routing facade."""

    def __init__(
        self,
        min_prior_successes: int = _MIN_PRIOR_SUCCESSES,
        high_confidence_threshold: float = _HIGH_CONFIDENCE_THRESHOLD,
        low_confidence_threshold: float = _LOW_CONFIDENCE_THRESHOLD,
    ) -> None:
        self.min_prior_successes = min_prior_successes
        self.high_confidence_threshold = high_confidence_threshold
        self.low_confidence_threshold = low_confidence_threshold

    def check_inspector_bypass_safety(
        self,
        description: str,
        confidence: float,
        prior_successes: int = 0,
        involves_code_generation: bool = False,
        autonomy_level: int = 2,
    ) -> InspectorBypassCheck:
        """Apply the US-001 truth table to determine if Inspector bypass is safe.

        Args:
            description: Task description to scan for safety-critical patterns.
            confidence: Router confidence in the System 1 classification.
            prior_successes: Prior successful runs on similar tasks.
            involves_code_generation: Whether the task produces executable code.
            autonomy_level: Pipeline autonomy level.

        Returns:
            Bypass decision with denial reason and safety flags when blocked.
        """
        desc_lower = description.lower()
        triggered_flags: list[str] = [pattern for pattern in _SAFETY_CRITICAL_PATTERNS if pattern in desc_lower]
        if triggered_flags:
            return InspectorBypassCheck(
                allowed=False,
                reason=f"Safety-critical operation detected - Inspector required. Triggered patterns: {triggered_flags}",
                safety_flags=tuple(triggered_flags),
            )
        if involves_code_generation:
            return InspectorBypassCheck(
                allowed=False,
                reason="Code generation tasks require Inspector verification",
            )
        if confidence < self.high_confidence_threshold:
            return InspectorBypassCheck(
                allowed=False,
                reason=f"Confidence {confidence:.2f} below threshold {self.high_confidence_threshold} - Inspector required",
            )
        if autonomy_level < 2:
            return InspectorBypassCheck(
                allowed=False,
                reason=f"Autonomy level L{autonomy_level} insufficient - L2+ required for bypass",
            )
        if prior_successes < self.min_prior_successes:
            return InspectorBypassCheck(
                allowed=False,
                reason=(
                    f"Insufficient prior successes ({prior_successes} < {self.min_prior_successes}) "
                    "- Inspector required until track record established"
                ),
            )
        return InspectorBypassCheck(
            allowed=True,
            reason=(
                f"All bypass conditions met: confidence={confidence:.2f}, "
                f"prior_successes={prior_successes}, autonomy_level={autonomy_level}, "
                "no safety-critical patterns, no code generation"
            ),
        )

    def _system_2_reasoning(
        self,
        is_complex: bool,
        is_custom_tier: bool,
        is_low_confidence: bool,
        confidence: float,
    ) -> str:
        reasons: list[str] = []
        if is_complex:
            reasons.append("complexity=COMPLEX")
        if is_custom_tier:
            reasons.append("tier=CUSTOM")
        if is_low_confidence:
            reasons.append(f"confidence={confidence:.2f} < threshold {self.low_confidence_threshold}")
        return f"System 2 (full pipeline): {', '.join(reasons)}"

    def route_system(
        self,
        description: str,
        intake_tier: str | None = None,
        complexity: str | None = None,
        confidence: float = 0.5,
        prior_successes: int = 0,
        involves_code_generation: bool = True,
        autonomy_level: int = 2,
    ) -> SystemDecision:
        """Route a task to System 1 or System 2 processing.

        Args:
            description: Task description to route.
            intake_tier: Intake tier classification.
            complexity: Complexity classification.
            confidence: Router confidence.
            prior_successes: Prior successful runs on similar tasks.
            involves_code_generation: Whether the task produces executable code.
            autonomy_level: Pipeline autonomy level.

        Returns:
            System routing decision with model tier and bypass state.
        """
        tier_lower = (intake_tier or "").lower()
        complexity_lower = (complexity or "").lower()
        routing_signals = _collect_routing_signals(description, intake_tier, complexity, confidence)
        is_complex = complexity_lower == "complex"
        is_custom_tier = tier_lower == "custom"
        is_low_confidence = confidence < self.low_confidence_threshold
        if is_complex or is_custom_tier or is_low_confidence:
            bypass_check = self.check_inspector_bypass_safety(
                description,
                confidence,
                prior_successes,
                involves_code_generation,
                autonomy_level,
            )
            reasoning = self._system_2_reasoning(is_complex, is_custom_tier, is_low_confidence, confidence)
            return _record_system_decision(
                _build_system_2_decision(bypass_check, reasoning, routing_signals),
                description,
            )
        is_express_tier = tier_lower == "express"
        is_simple = complexity_lower == "simple"
        is_high_confidence = confidence >= self.high_confidence_threshold
        if (is_express_tier or is_simple) and is_high_confidence:
            bypass_check = self.check_inspector_bypass_safety(
                description,
                confidence,
                prior_successes,
                involves_code_generation,
                autonomy_level,
            )
            tier_reason = "tier=EXPRESS" if is_express_tier else "complexity=SIMPLE"
            reasoning = (
                f"System 1 (fast path): {tier_reason}, confidence={confidence:.2f} >= {self.high_confidence_threshold}"
            )
            return _record_system_decision(
                _build_system_1_decision(bypass_check, reasoning, routing_signals),
                description,
            )
        bypass_check = self.check_inspector_bypass_safety(
            description,
            confidence,
            prior_successes,
            involves_code_generation,
            autonomy_level,
        )
        return _record_system_decision(
            _build_standard_decision(bypass_check, intake_tier, complexity, confidence, routing_signals),
            description,
        )


def check_inspector_bypass_safety(
    description: str,
    confidence: float,
    prior_successes: int = 0,
    involves_code_generation: bool = False,
    autonomy_level: int = 2,
) -> InspectorBypassCheck:
    """Apply the US-001 truth table to determine if Inspector bypass is safe."""
    return SystemRouter().check_inspector_bypass_safety(
        description=description,
        confidence=confidence,
        prior_successes=prior_successes,
        involves_code_generation=involves_code_generation,
        autonomy_level=autonomy_level,
    )


def _collect_routing_signals(
    description: str,
    intake_tier: str | None,
    complexity: str | None,
    confidence: float,
) -> dict[str, Any]:
    routing_signals: dict[str, Any] = {}
    try:
        routing_signals["historical_success_rate"] = _get_historical_success_rate(description)
    except Exception:
        logger.warning("Historical success rate lookup failed - routing signal omitted")
    try:
        routing_signals["skill_library_match"] = _check_skill_library_match(description)
    except Exception:
        logger.warning("Skill library match check failed - routing signal omitted")
    routing_signals["intake_tier"] = intake_tier
    routing_signals["complexity"] = complexity
    routing_signals["confidence"] = confidence
    return routing_signals


def _inspector_bypass_for_route(
    description: str,
    confidence: float,
    prior_successes: int,
    involves_code_generation: bool,
    autonomy_level: int,
) -> InspectorBypassCheck:
    return check_inspector_bypass_safety(
        description=description,
        confidence=confidence,
        prior_successes=prior_successes,
        involves_code_generation=involves_code_generation,
        autonomy_level=autonomy_level,
    )


def _record_system_decision(decision: SystemDecision, description: str) -> SystemDecision:
    logger.info("[Router] %s", decision.reasoning)
    _track_system_decision(decision, description)
    return decision


def _system_2_reasoning(is_complex: bool, is_custom_tier: bool, is_low_confidence: bool, confidence: float) -> str:
    reasons: list[str] = []
    if is_complex:
        reasons.append("complexity=COMPLEX")
    if is_custom_tier:
        reasons.append("tier=CUSTOM")
    if is_low_confidence:
        reasons.append(f"confidence={confidence:.2f} < threshold {_LOW_CONFIDENCE_THRESHOLD}")
    return f"System 2 (full pipeline): {', '.join(reasons)}"


def _build_system_2_decision(
    bypass_check: InspectorBypassCheck,
    reasoning: str,
    routing_signals: dict[str, Any],
) -> SystemDecision:
    return SystemDecision(
        system_type=SystemType.SYSTEM_2,
        model_tier=ModelTier.LARGE,
        skip_foreman=False,
        skip_inspector=False,
        inspector_bypass=bypass_check,
        reasoning=reasoning,
        routing_signals=routing_signals,
    )


def _build_system_1_decision(
    bypass_check: InspectorBypassCheck,
    reasoning: str,
    routing_signals: dict[str, Any],
) -> SystemDecision:
    return SystemDecision(
        system_type=SystemType.SYSTEM_1,
        model_tier=ModelTier.SMALL,
        skip_foreman=True,
        skip_inspector=bypass_check.allowed,
        inspector_bypass=bypass_check,
        reasoning=reasoning,
        routing_signals=routing_signals,
    )


def _build_standard_decision(
    bypass_check: InspectorBypassCheck,
    intake_tier: str | None,
    complexity: str | None,
    confidence: float,
    routing_signals: dict[str, Any],
) -> SystemDecision:
    reasoning = (
        f"System 2 (standard pipeline): tier={intake_tier!r}, complexity={complexity!r}, confidence={confidence:.2f}"
    )
    return SystemDecision(
        system_type=SystemType.SYSTEM_2,
        model_tier=ModelTier.MEDIUM,
        skip_foreman=False,
        skip_inspector=False,
        inspector_bypass=bypass_check,
        reasoning=reasoning,
        routing_signals=routing_signals,
    )


def route_system(
    description: str,
    intake_tier: str | None = None,
    complexity: str | None = None,
    confidence: float = 0.5,
    prior_successes: int = 0,
    involves_code_generation: bool = True,
    autonomy_level: int = 2,
) -> SystemDecision:
    """Route a task to System 1 or System 2 processing."""
    return SystemRouter().route_system(
        description=description,
        intake_tier=intake_tier,
        complexity=complexity,
        confidence=confidence,
        prior_successes=prior_successes,
        involves_code_generation=involves_code_generation,
        autonomy_level=autonomy_level,
    )


def get_system_routing_stats() -> dict[str, Any]:
    """Return aggregated statistics over all recorded routing decisions.

    Returns:
        Dictionary with keys: total, system_1_count, system_2_count, system_1_rate.
    """
    with _decisions_lock:
        total = len(_system_decisions)
        system_1_count = sum(1 for d in _system_decisions if d.get("system_type") == SystemType.SYSTEM_1.value)
    system_2_count = total - system_1_count
    return {
        "total": total,
        "system_1_count": system_1_count,
        "system_2_count": system_2_count,
        "system_1_rate": system_1_count / total if total > 0 else 0.0,
    }


def reset_system_routing_stats() -> None:
    """Clear all tracked routing decisions.

    Intended for use in tests to isolate state between test cases.
    """
    with _decisions_lock:
        _system_decisions.clear()
