"""Update manifest parsing and validation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from vetinari.workbench.update_safety.contracts import SCHEMA_VERSION, UpdateManifest, UpdateSafetyError


def parse_update_manifest(path_or_payload: str | Path | dict[str, Any]) -> UpdateManifest:
    """Parse an update manifest from JSON file path or mapping.

    Returns:
        Parsed update manifest value.

    Raises:
        Exception: Propagates validation or runtime failures from the underlying operation.
    """
    if isinstance(path_or_payload, dict):
        payload = path_or_payload
    else:
        path = Path(path_or_payload)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise UpdateSafetyError(f"manifest_unreadable:{type(exc).__name__}") from exc
        if not isinstance(payload, dict):
            raise UpdateSafetyError("manifest root must be an object")
    manifest = UpdateManifest.from_dict(payload)
    if manifest.schema_version != SCHEMA_VERSION:
        raise UpdateSafetyError(f"manifest schema_version must be {SCHEMA_VERSION}")
    return manifest


__all__ = ["parse_update_manifest"]
