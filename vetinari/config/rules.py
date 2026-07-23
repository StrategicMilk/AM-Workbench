"""Rule configuration loader."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

RULES_PATH = Path(__file__).with_name("rules.yaml")


def load_rules(path: str | Path | None = None) -> dict[str, Any]:
    """Load rule configuration.

    Args:
        path: Optional YAML rule path.

    Returns:
        Parsed rule mapping.

    Raises:
        ValueError: If the rule config root is not a mapping.
    """
    rules_path = Path(path) if path is not None else RULES_PATH
    if not rules_path.exists():
        return {}
    loaded = yaml.safe_load(rules_path.read_text(encoding="utf-8")) or {}
    if not isinstance(loaded, dict):
        raise ValueError("rules config root must be a mapping")
    return loaded


__all__ = ["RULES_PATH", "load_rules"]
