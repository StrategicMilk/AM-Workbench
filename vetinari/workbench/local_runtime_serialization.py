"""Serialization helpers for local-runtime onboarding receipts."""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from enum import Enum
from typing import Any


def json_safe(value: Any) -> Any:
    """Return a JSON-safe representation of onboarding dataclasses and enums.

    Returns:
        A recursively JSON-safe value.
    """
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value) and not isinstance(value, type):
        return {key: json_safe(item) for key, item in asdict(value).items()}
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    return value
