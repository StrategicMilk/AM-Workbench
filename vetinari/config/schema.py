"""Configuration schema helpers used by runtime validation tests."""

from __future__ import annotations

from typing import Any

_KEY_TYPES: dict[str, type[Any]] = {
    "max_tokens": int,
    "temperature": float,
    "top_p": float,
    "model": str,
    "enabled": bool,
}


def resolve_key_type(name: str) -> type[Any]:
    """Return the expected Python type for a known configuration key.

    Returns:
        The Python type associated with ``name``.

    Raises:
        ValueError: If the configuration key is unknown.
    """
    try:
        return _KEY_TYPES[name]
    except KeyError as exc:
        raise ValueError(f"unknown configuration key type: {name}") from exc


def validate_key_value(name: str, value: Any) -> Any:
    """Validate a configuration key value against the schema type.

    Args:
        name: Configuration key name.
        value: Candidate value to validate.

    Returns:
        The original value when it matches the schema.

    Raises:
        TypeError: If the value does not match the schema type.
        ValueError: If the configuration key is unknown.
    """
    expected = resolve_key_type(name)
    if expected is int and isinstance(value, bool):
        raise TypeError(f"{name} expects int, not bool")
    if not isinstance(value, expected):
        raise TypeError(f"{name} expects {expected.__name__}")
    return value
