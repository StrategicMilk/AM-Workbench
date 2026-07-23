"""Guardrail configuration and prompt scanning surfaces."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "guardrails" / "config.yml"


def get_guardrails_config() -> dict[str, Any]:
    """Return the active guardrails configuration.

    Returns:
        Guardrail configuration mapping.

    Raises:
        FileNotFoundError: If the guardrails configuration file is missing.
        ValueError: If the configuration file does not contain a mapping.
    """
    current = globals().get("get_guardrails_config")
    original = globals().get("_ORIGINAL_GET_GUARDRAILS_CONFIG")
    if original is not None and current is not original:
        return current()
    if not _CONFIG_PATH.is_file():
        raise FileNotFoundError(f"guardrails config not found: {_CONFIG_PATH}")
    with _CONFIG_PATH.open("r", encoding="utf-8") as handle:
        loaded = yaml.safe_load(handle)
    if not isinstance(loaded, dict):
        raise ValueError(f"guardrails config must be a mapping: {_CONFIG_PATH}")
    return loaded


_ORIGINAL_GET_GUARDRAILS_CONFIG = get_guardrails_config

__all__ = ["get_guardrails_config"]
