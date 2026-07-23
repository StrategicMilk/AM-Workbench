"""Validation and JSON helpers for the Workbench model registry."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from vetinari.api.responses import json_safe

_json_safe = json_safe


def _require_non_empty(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be non-empty")


def _require_string_tuple(values: tuple[str, ...], field_name: str) -> None:
    if not isinstance(values, tuple) or not values:
        raise ValueError(f"{field_name} must be a non-empty tuple")
    if not all(isinstance(value, str) and value.strip() for value in values):
        raise ValueError(f"{field_name} must contain non-empty strings")


def _require_string_dict(values: dict[str, str], field_name: str) -> None:
    if not isinstance(values, dict) or not values:
        raise ValueError(f"{field_name} must be a non-empty dict[str, str]")
    if not all(isinstance(key, str) and key.strip() and isinstance(value, str) for key, value in values.items()):
        raise ValueError(f"{field_name} must contain string keys and values")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _tuple(payload: dict[str, Any], key: str) -> tuple[str, ...]:
    return tuple(str(value) for value in payload.get(key, ()))
