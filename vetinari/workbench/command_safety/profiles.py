"""Command-safety profile loading."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from vetinari.workbench.command_safety.contracts import (
    SCHEMA_VERSION,
    CommandSafetyError,
    CommandSafetyProfile,
    CommandSurface,
)

DEFAULT_CONFIG_PATH = Path("config") / "workbench" / "command_safety.yaml"


def load_command_safety_profiles(path: Path | str | None = None) -> dict[str, CommandSafetyProfile]:
    """Execute the load command safety profiles operation.

    Returns:
        Resolved command safety profiles value.

    Raises:
        Exception: Propagates validation or runtime failures from the underlying operation.
    """
    config_path = Path(path) if path is not None else DEFAULT_CONFIG_PATH
    if not config_path.exists():
        raise CommandSafetyError(f"command-safety policy not found: {config_path}")
    try:
        doc = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise CommandSafetyError(f"command-safety policy unreadable: {exc}") from exc
    return prepare_command_safety_profiles(doc)


def prepare_command_safety_profiles(doc: Any) -> dict[str, CommandSafetyProfile]:
    """Execute the prepare command safety profiles operation.

    Returns:
        dict[str, CommandSafetyProfile] value produced by prepare_command_safety_profiles().

    Raises:
        Exception: Propagates validation or runtime failures from the underlying operation.
    """
    if not isinstance(doc, dict):
        raise CommandSafetyError("command-safety policy root must be a mapping")
    if str(doc.get("schema_version", "")) != SCHEMA_VERSION:
        raise CommandSafetyError(f"command-safety schema_version must be {SCHEMA_VERSION}")
    rows = doc.get("profiles")
    if not isinstance(rows, list) or not rows:
        raise CommandSafetyError("command-safety policy must contain profiles")
    profiles: dict[str, CommandSafetyProfile] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise CommandSafetyError("each command-safety profile must be a mapping")
        profile = CommandSafetyProfile(
            profile_id=str(row.get("id", "")),
            surfaces=tuple(CommandSurface(str(surface)) for surface in row.get("surfaces", ())),
            safe_prefixes=tuple(str(prefix) for prefix in row.get("safe_prefixes", ())),
            approval_prefixes=tuple(str(prefix) for prefix in row.get("approval_prefixes", ())),
            blocked_patterns=tuple(str(pattern) for pattern in row.get("blocked_patterns", ())),
            allowed_cwd_roots=tuple(str(root) for root in row.get("allowed_cwd_roots", ())),
            require_tool_pin=bool(row.get("require_tool_pin", True)),
            allow_without_human_approval=bool(row.get("allow_without_human_approval", False)),
        )
        if profile.profile_id in profiles:
            raise CommandSafetyError(f"duplicate command-safety profile: {profile.profile_id}")
        if not profile.surfaces:
            raise CommandSafetyError(f"profile {profile.profile_id} must declare surfaces")
        profiles[profile.profile_id] = profile
    return profiles
