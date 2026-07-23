"""Sandbox policy loading helpers."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from vetinari.config.sandbox_schema import SandboxPolicyConfig


@dataclass(frozen=True, slots=True)
class SandboxPolicy:
    """Parsed sandbox policy result."""

    valid: bool
    payload: dict[str, Any]
    config: SandboxPolicyConfig


def load_sandbox_policy(path: Path) -> SandboxPolicy:
    """Load and validate a sandbox policy file.

    Args:
        path: YAML or JSON policy path.

    Returns:
        Parsed policy wrapper.

    Raises:
        TypeError: If the policy root is not a mapping.
    """
    raw = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        payload = json.loads(raw)
    else:
        try:
            import yaml
        except ImportError as exc:
            raise RuntimeError("pyyaml is required to load sandbox policies") from exc
        payload = yaml.safe_load(raw)
    if not isinstance(payload, dict):
        raise TypeError("sandbox policy root must be a mapping")
    config = SandboxPolicyConfig.model_validate(payload)
    return SandboxPolicy(valid=bool(payload), payload=config.model_dump(mode="json"), config=config)
