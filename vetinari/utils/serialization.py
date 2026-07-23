"""Serialization utilities for dataclass-to-dict conversion.

Replaces hand-written ``to_dict()`` methods on dataclasses with a single
recursive converter that handles enums, datetimes, and nested dataclasses.
"""

from __future__ import annotations

import dataclasses
from datetime import datetime
from enum import Enum
from typing import Any


def dataclass_to_dict(obj: Any) -> dict[str, Any]:
    """Convert a dataclass instance to a JSON-serializable dictionary.

    Recursively processes all fields, converting enums to their ``.value``,
    datetimes to ISO-8601 strings, and nested dataclasses to dicts.  Lists
    and dicts are traversed element-wise so nested structures are handled
    correctly.

    Args:
        obj: A dataclass instance to serialize.

    Returns:
        A plain dictionary suitable for ``json.dumps()``.

    Raises:
        TypeError: If *obj* is not a dataclass instance.
        ValueError: If *obj* contains a cyclic dataclass or container graph.
    """
    if not dataclasses.is_dataclass(obj) or isinstance(obj, type):
        raise TypeError(f"Expected a dataclass instance, got {type(obj).__name__}")

    return _dataclass_to_dict(obj, set())


def _dataclass_to_dict(obj: Any, active_ids: set[int]) -> dict[str, Any]:
    obj_id = id(obj)
    if obj_id in active_ids:
        raise ValueError("Cannot serialize cyclic dataclass graph")
    active_ids.add(obj_id)
    result: dict[str, Any] = {}
    try:
        for f in dataclasses.fields(obj):
            result[f.name] = _convert_value(getattr(obj, f.name), active_ids)
        return result
    finally:
        active_ids.discard(obj_id)


def _convert_value(value: Any, active_ids: set[int]) -> Any:
    """Recursively convert a value to a JSON-serializable form."""
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return _dataclass_to_dict(value, active_ids)
    if isinstance(value, list):
        value_id = id(value)
        if value_id in active_ids:
            raise ValueError("Cannot serialize cyclic container graph")
        active_ids.add(value_id)
        try:
            return [_convert_value(v, active_ids) for v in value]
        finally:
            active_ids.discard(value_id)
    if isinstance(value, tuple):
        # JSON has no tuple type; convert to list so json.dumps() can serialise it.
        value_id = id(value)
        if value_id in active_ids:
            raise ValueError("Cannot serialize cyclic container graph")
        active_ids.add(value_id)
        try:
            return [_convert_value(v, active_ids) for v in value]
        finally:
            active_ids.discard(value_id)
    if isinstance(value, dict):
        value_id = id(value)
        if value_id in active_ids:
            raise ValueError("Cannot serialize cyclic container graph")
        active_ids.add(value_id)
        try:
            return {k: _convert_value(v, active_ids) for k, v in value.items()}
        finally:
            active_ids.discard(value_id)
    return value
