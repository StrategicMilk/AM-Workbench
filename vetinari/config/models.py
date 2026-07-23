"""Model configuration compatibility surface."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

CATALOG_YAML_PATH = Path(__file__).with_name("models.yaml")


def load_models_config(path: str | Path | None = None) -> dict[str, Any]:
    """Load the model catalog YAML.

    Args:
        path: Optional YAML path. Defaults to the packaged models catalog.

    Returns:
        Parsed model configuration mapping.

    Raises:
        FileNotFoundError: If the model config path does not exist.
        ValueError: If the model config root is not a mapping.
    """
    config_path = Path(path) if path is not None else CATALOG_YAML_PATH
    if not config_path.exists():
        raise FileNotFoundError(f"models config not found at {config_path}")
    loaded = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    if not isinstance(loaded, dict):
        raise ValueError("models config root must be a mapping")
    return loaded


__all__ = ["CATALOG_YAML_PATH", "load_models_config"]
