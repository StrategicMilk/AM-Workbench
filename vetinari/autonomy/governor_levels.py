"""Level and confidence routing helpers for the autonomy governor."""

from __future__ import annotations

from vetinari.types import AutonomyLevel, AutonomyMode, PermissionDecision

_MODE_DEFAULTS: dict[AutonomyMode, dict[str, AutonomyLevel]] = {
    AutonomyMode.CONSERVATIVE: {
        "risky": AutonomyLevel.L1_SUGGEST,
        "medium": AutonomyLevel.L2_ACT_REPORT,
        "safe": AutonomyLevel.L3_ACT_LOG,
    },
    AutonomyMode.BALANCED: {
        "risky": AutonomyLevel.L2_ACT_REPORT,
        "medium": AutonomyLevel.L3_ACT_LOG,
        "safe": AutonomyLevel.L4_FULL_AUTO,
    },
    AutonomyMode.AGGRESSIVE: {
        "risky": AutonomyLevel.L3_ACT_LOG,
        "medium": AutonomyLevel.L4_FULL_AUTO,
        "safe": AutonomyLevel.L4_FULL_AUTO,
    },
}

_CONFIDENCE_BANDS = [
    ("high", 0.85),
    ("medium", 0.6),
    ("low", 0.4),
    ("very_low", 0.0),
]

_MODE_CONFIDENCE_LEVELS: dict[AutonomyMode, dict[str, AutonomyLevel]] = {
    AutonomyMode.CONSERVATIVE: {
        "high": AutonomyLevel.L3_ACT_LOG,
        "medium": AutonomyLevel.L2_ACT_REPORT,
        "low": AutonomyLevel.L1_SUGGEST,
        "very_low": AutonomyLevel.L0_MANUAL,
    },
    AutonomyMode.BALANCED: {
        "high": AutonomyLevel.L4_FULL_AUTO,
        "medium": AutonomyLevel.L3_ACT_LOG,
        "low": AutonomyLevel.L2_ACT_REPORT,
        "very_low": AutonomyLevel.L1_SUGGEST,
    },
    AutonomyMode.AGGRESSIVE: {
        "high": AutonomyLevel.L4_FULL_AUTO,
        "medium": AutonomyLevel.L4_FULL_AUTO,
        "low": AutonomyLevel.L3_ACT_LOG,
        "very_low": AutonomyLevel.L2_ACT_REPORT,
    },
}

_LEVEL_ORDER = [
    AutonomyLevel.L0_MANUAL,
    AutonomyLevel.L1_SUGGEST,
    AutonomyLevel.L2_ACT_REPORT,
    AutonomyLevel.L3_ACT_LOG,
    AutonomyLevel.L4_FULL_AUTO,
]


def _level_to_decision(level: AutonomyLevel) -> PermissionDecision:
    """Map an autonomy level to a permission decision."""
    if level == AutonomyLevel.L0_MANUAL:
        return PermissionDecision.DENY
    if level == AutonomyLevel.L1_SUGGEST:
        return PermissionDecision.DEFER
    return PermissionDecision.APPROVE


def _confidence_to_band(confidence: float) -> str:
    """Map a confidence score to its named confidence band."""
    for band, threshold in _CONFIDENCE_BANDS:
        if confidence >= threshold:
            return band
    return "very_low"


def _confidence_to_level(confidence: float, mode: AutonomyMode) -> AutonomyLevel:
    """Map confidence to an autonomy level under the active mode."""
    band = _confidence_to_band(confidence)
    return _MODE_CONFIDENCE_LEVELS[mode][band]


def _min_level(first: AutonomyLevel, second: AutonomyLevel) -> AutonomyLevel:
    """Return the lower, more conservative autonomy level."""
    return first if _LEVEL_ORDER.index(first) <= _LEVEL_ORDER.index(second) else second


def _mode_default(mode: AutonomyMode, risk_tier: str) -> AutonomyLevel:
    """Return the default autonomy level for a risk tier under an autonomy mode."""
    return _MODE_DEFAULTS.get(mode, {}).get(risk_tier, AutonomyLevel.L1_SUGGEST)


def _demote_one_level(level: AutonomyLevel) -> AutonomyLevel:
    """Return the autonomy level one step below the given level."""
    idx = _LEVEL_ORDER.index(level)
    if idx == 0:
        return level
    return _LEVEL_ORDER[idx - 1]


def _promote_one_level(level: AutonomyLevel) -> AutonomyLevel:
    """Return the autonomy level one step above the given level."""
    idx = _LEVEL_ORDER.index(level)
    if idx >= len(_LEVEL_ORDER) - 1:
        return level
    return _LEVEL_ORDER[idx + 1]
