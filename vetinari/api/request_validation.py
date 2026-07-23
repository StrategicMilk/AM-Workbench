"""Request body validation helpers for JSON API payloads."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

_MAX_BODY_DEPTH = 5
_MAX_KEY_LENGTH = 256


def _measure_depth(value: Any, current: int = 0) -> int:
    """Recursively measure the nesting depth of a JSON-like value."""
    if isinstance(value, dict):
        if not value:
            return current
        return max(_measure_depth(v, current + 1) for v in value.values())
    if isinstance(value, list):
        if not value:
            return current
        return max(_measure_depth(v, current + 1) for v in value)
    return current


def body_has_oversized_key(data: dict[str, Any] | None, max_key_length: int = _MAX_KEY_LENGTH) -> bool:
    """Return True when any key in the request body exceeds the byte budget.

    Args:
        data: Parsed JSON object to inspect.
        max_key_length: Maximum allowed key length.

    Returns:
        Whether any nested key exceeds ``max_key_length``.
    """
    if data is None:
        return False
    return _has_oversized_key(data, max_key_length)


def json_object_body(data: Any | None) -> dict[str, Any] | None:
    """Return a JSON object body or ``None`` when the payload has the wrong shape.

    Returns:
        A dictionary payload, an empty dictionary for a missing body, or
        ``None`` when the payload is not a JSON object.
    """
    if data is None:
        return {}
    if isinstance(data, dict):
        return data
    logger.warning("Request body rejected - expected JSON object, got %s", type(data).__name__)
    return None


def _has_oversized_key(value: Any, max_key_length: int) -> bool:
    """Recursively scan ``value`` for any dict key longer than ``max_key_length``."""
    if isinstance(value, dict):
        for k, v in value.items():
            if isinstance(k, str) and len(k) > max_key_length:
                logger.warning(
                    "Request body rejected - key length %d exceeds maximum %d",
                    len(k),
                    max_key_length,
                )
                return True
            if _has_oversized_key(v, max_key_length):
                return True
    elif isinstance(value, list):
        for item in value:
            if _has_oversized_key(item, max_key_length):
                return True
    return False


def body_depth_exceeded(data: dict[str, Any] | None, max_depth: int = _MAX_BODY_DEPTH) -> bool:
    """Return True when the request body nesting depth exceeds ``max_depth``.

    Args:
        data: Parsed JSON object to inspect.
        max_depth: Maximum allowed nesting depth.

    Returns:
        Whether the parsed body is nested more deeply than ``max_depth``.
    """
    if data is None:
        return False
    depth = _measure_depth(data)
    if depth > max_depth:
        logger.warning(
            "Request body rejected - nesting depth %d exceeds maximum %d",
            depth,
            max_depth,
        )
        return True
    return False
