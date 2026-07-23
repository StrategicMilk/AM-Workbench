"""Fail-closed contract enforcement helpers for Vetinari.

This module is the single authority for contract enforcement and must never
catch-all or return partial state.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

__all__ = [
    "ConfigContractViolation",
    "EnumContractViolation",
    "assert_enum_field",
    "fail_closed_config_load",
]


class ConfigContractViolation(RuntimeError):
    """Raised when a config contract cannot be loaded or trusted."""

    def __init__(self, *, path: Path | str, reason: str) -> None:
        self.path = Path(path)
        self.reason = reason
        super().__init__(f"{reason}: {self.path}")


class EnumContractViolation(TypeError):
    """Raised when an enum-like config field is outside its allowed values."""

    def __init__(self, *, field_name: str, bad_value: str, allowed: frozenset[str]) -> None:
        self.field_name = field_name
        self.bad_value = bad_value
        self.allowed = allowed
        super().__init__(f"Field {field_name!r} received invalid value {bad_value!r}; allowed: {sorted(allowed)}")


def _yaml_load(path: Path) -> Any:
    with path.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def fail_closed_config_load(path: Path | str, *, loader: Callable[[Path], Any] = _yaml_load) -> Any:
    """Load YAML-like config and reject missing, unreadable, or empty payloads.

    Returns:
        Parsed non-empty config payload.

    Raises:
        ConfigContractViolation: If the config is missing, unreadable, or empty.
    """
    p = Path(path)
    if not p.exists():
        raise ConfigContractViolation(path=p, reason="Config file missing")

    try:
        payload = loader(p)
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigContractViolation(path=p, reason=f"Config file unreadable: {exc}") from exc

    if payload is None or (isinstance(payload, dict) and not payload):
        raise ConfigContractViolation(path=p, reason="Config file empty")

    return payload


def assert_enum_field(field_name: str, value: str, allowed: Iterable[str]) -> None:
    """Validate one string field against an explicit allowed-value set.

    Args:
        field_name: Name of the field being checked.
        value: Candidate field value.
        allowed: Iterable of accepted values.

    Raises:
        EnumContractViolation: If ``value`` is not in ``allowed``.
    """
    allowed_set = frozenset(allowed)
    if value not in allowed_set:
        raise EnumContractViolation(field_name=field_name, bad_value=value, allowed=allowed_set)
    return None
