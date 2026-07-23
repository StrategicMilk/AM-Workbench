"""Helpers for deferring config reads until runtime use."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any, TypeVar

import yaml

T = TypeVar("T")


def env_int(name: str, default: int, *, environ: Mapping[str, str] | None = None) -> int:
    """Read an integer environment variable at call time.

    Args:
        name: Environment variable name.
        default: Value returned when the variable is unset or empty.
        environ: Optional environment mapping for tests.

    Returns:
        Parsed integer value.

    Raises:
        ValueError: If the configured value is not an integer.
    """
    raw = (os.environ if environ is None else environ).get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc


def env_float(name: str, default: float, *, environ: Mapping[str, str] | None = None) -> float:
    """Read a floating point environment variable at call time.

    Args:
        name: Environment variable name.
        default: Value returned when the variable is unset or empty.
        environ: Optional environment mapping for tests.

    Returns:
        Parsed floating point value.

    Raises:
        ValueError: If the configured value is not a float.
    """
    raw = (os.environ if environ is None else environ).get(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a float") from exc


def read_yaml_mapping(path: Path) -> dict[str, Any]:
    """Read a YAML mapping from disk and fail closed for malformed roots.

    Args:
        path: YAML file path.

    Returns:
        Parsed mapping.

    Raises:
        TypeError: If the YAML root is not a mapping.
        OSError: If the file cannot be read.
        yaml.YAMLError: If the file is invalid YAML.
    """
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise TypeError(f"{path} must contain a YAML mapping")
    return raw
