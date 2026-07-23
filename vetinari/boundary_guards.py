"""Shared fail-closed boundary helpers for audit and workflow glue code."""

from __future__ import annotations

import logging
import math
from collections.abc import Iterable, Mapping
from numbers import Real
from typing import Any

logger = logging.getLogger(__name__)

SCORE_MIN = 0.0
SCORE_MAX = 1.0

__all__ = [
    "SCORE_MAX",
    "SCORE_MIN",
    "account_evidence_drop",
    "assert_dependency_success",
    "clamp_score",
    "require_nonempty",
    "require_score_in_range",
    "route_enum_error",
]


def _finite_score(value: Any, *, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"{field_name} must be a finite numeric score")
    score = float(value)
    if not math.isfinite(score):
        raise ValueError(f"{field_name} must be a finite numeric score")
    return score


def clamp_score(
    value: Any,
    *,
    low: float = SCORE_MIN,
    high: float = SCORE_MAX,
    label: str = "",
    field_name: str = "score",
) -> float:
    """Clamp a finite score to Vetinari's normalized range.

    Args:
        value: Candidate numeric score.
        low: Inclusive lower clamp bound.
        high: Inclusive upper clamp bound.
        label: Optional provenance label for warning logs.
        field_name: Field name used in validation errors.

    Returns:
        The finite score clamped to ``[low, high]``.

    Raises:
        ValueError: If the value or bounds are not finite numeric inputs.
    """
    score = _finite_score(value, field_name=field_name)
    lower = _finite_score(low, field_name="low")
    upper = _finite_score(high, field_name="high")
    if lower > upper:
        raise ValueError("low cannot exceed high")
    clamped = min(max(score, lower), upper)
    if clamped != score:
        context = f" for {label}" if label else ""
        logger.warning("score%s clamped from %r to %r", context, score, clamped)
    return clamped


def require_score_in_range(value: Any, label: str = "", *, field_name: str = "score") -> float:
    """Return a score only when it is finite and already inside ``[0, 1]``.

    Args:
        value: Candidate numeric score.
        label: Optional provenance label for error text.
        field_name: Field name used in validation errors.

    Returns:
        The validated score.

    Raises:
        ValueError: If the score is non-numeric, non-finite, or outside range.
    """
    score = _finite_score(value, field_name=field_name)
    if score < SCORE_MIN or score > SCORE_MAX:
        prefix = f"{label}: " if label else ""
        raise ValueError(f"{prefix}{field_name}={score} is outside [{SCORE_MIN}, {SCORE_MAX}]")
    return score


def route_enum_error(
    enum_cls: object | None = None,
    raw_value: object | None = None,
    correct_blocker: object | None = None,
    label: str = "",
    *,
    field_name: str | None = None,
    received: object | None = None,
    allowed: Iterable[object] | None = None,
    route_id: str | None = None,
) -> dict[str, object]:
    """Build a deterministic blocker payload for invalid enum-like route values.

    Args:
        enum_cls: Optional enum class for legacy callers that expect a blocker enum.
        raw_value: Raw enum value that failed coercion.
        correct_blocker: Blocker enum member to return for legacy callers.
        label: Optional provenance label for warning logs.
        field_name: Field name for structured blocker payloads.
        received: Raw value for structured blocker payloads.
        allowed: Allowed values for structured blocker payloads.
        route_id: Optional route identifier for structured blocker payloads.

    Returns:
        A structured blocker payload, or ``correct_blocker`` for enum-style callers.

    Raises:
        ValueError: If required structured blocker fields are missing.
    """
    if correct_blocker is not None:
        context = f" for {label}" if label else ""
        logger.warning("enum route error%s: %r is invalid for %r", context, raw_value, enum_cls)
        return correct_blocker
    if field_name is None or allowed is None:
        raise ValueError("field_name and allowed enum values are required")
    name = require_nonempty(field_name, field_name="field_name")
    allowed_values = tuple(str(item) for item in allowed)
    if not allowed_values:
        raise ValueError("allowed enum values are required")
    payload: dict[str, object] = {
        "status": "blocked",
        "reason": "invalid-enum",
        "field": name,
        "received": received,
        "allowed": allowed_values,
    }
    if route_id is not None:
        payload["route_id"] = require_nonempty(route_id, field_name="route_id")
    return payload


def account_evidence_drop(
    item: object | None = None,
    queue_name: str | None = None,
    *,
    logger: logging.Logger | None = None,
    evidence_ref: str | None = None,
    reason: str | None = None,
    log: logging.Logger | None = None,
) -> None:
    """Log an evidence-drop event, failing closed when no logger is available.

    Args:
        item: Dropped evidence item for queue-style callers.
        queue_name: Queue name for queue-style callers.
        logger: Logger supplied by queue-style callers.
        evidence_ref: Evidence reference for structured callers.
        reason: Drop reason for structured callers.
        log: Backward-compatible logger alias.

    Raises:
        RuntimeError: If no logger is supplied.
        ValueError: If required evidence or queue fields are empty.
    """
    active_logger = logger if logger is not None else log
    if active_logger is None:
        raise RuntimeError("evidence-drop accounting requires a logger")
    if evidence_ref is not None or reason is not None:
        ref = require_nonempty(evidence_ref, field_name="evidence_ref")
        why = require_nonempty(reason, field_name="reason")
        active_logger.warning("evidence dropped: %s; reason=%s", ref, why)
        return
    queue = require_nonempty(queue_name, field_name="queue_name")
    active_logger.warning("evidence dropped from %s: %r", queue, item)


def assert_dependency_success(
    result: Mapping[str, Any] | bool | str,
    failed_ids: Iterable[str] | None = None,
    *,
    dependency_id: str = "dependency",
) -> None:
    """Require a dependency result to be explicitly successful.

    Args:
        result: Dependency result mapping, boolean, or dependency id.
        failed_ids: Failed dependency ids for dependency-set callers.
        dependency_id: Dependency id for mapping and boolean callers.

    Raises:
        RuntimeError: If the dependency is failed or not explicitly successful.
        ValueError: If the dependency result shape is unsupported.
    """
    if failed_ids is not None:
        dep = require_nonempty(str(result), field_name="dependency_id")
        if dep in set(failed_ids):
            raise RuntimeError(f"Dependency {dep!r} has failed status - cannot proceed")
        return
    dep = require_nonempty(dependency_id, field_name="dependency_id")
    if isinstance(result, bool):
        success = result
    elif isinstance(result, Mapping):
        success = result.get("status") in {"success", "passed", "PASS", "ok"} or result.get("passed") is True
    else:
        raise ValueError(f"{dep} result must be a bool or mapping")
    if not success:
        raise RuntimeError(f"{dep} did not complete successfully")


def require_nonempty(value: str | None, *, field_name: str = "value") -> str:
    """Return stripped text only when the input contains non-whitespace content.

    Args:
        value: Candidate text.
        field_name: Field name used in validation errors.

    Returns:
        Stripped non-empty text.

    Raises:
        ValueError: If the value is missing or whitespace-only.
    """
    if value is None:
        raise ValueError(f"{field_name} is required")
    stripped = value.strip()
    if not stripped:
        raise ValueError(f"{field_name} is required")
    return stripped
