"""Persistence helpers for prompt evolver variant state."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any

from vetinari.constants import VETINARI_STATE_DIR
from vetinari.guards import GateError

_SUBJECT_FIELD_NAMES = ("subject", "subject_id", "privacy_subject_id", "user_id")
_SUBJECT_MARKER_RE = re.compile(r"(?:^|\b)(?:subject|subject_id|privacy_subject_id|user_id)\s*[=: ]\s*(?P<id>[^\s,;]+)")


def prompt_variant_state_path(*, create: bool = False) -> Path:
    """Resolve the prompt variant state file path.

    Returns:
        Path to ``prompt_variants.json``.
    """
    state_dir_env = os.environ.get("VETINARI_STATE_DIR", "")
    state_dir = Path(state_dir_env) if state_dir_env else VETINARI_STATE_DIR
    if create:
        state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir / "prompt_variants.json"


def load_prompt_variants(variant_factory: Callable[..., Any], logger: Any) -> dict[str, list[Any]]:
    """Load prompt variants, failing closed on corrupt persisted state.

    Args:
        variant_factory: Callable that rehydrates one persisted variant row.
        logger: Logger-like object used for recoverable failure reporting.

    Returns:
        Prompt variants keyed by agent type.

    Raises:
        GateError: If persisted state is corrupt or unreadable.
    """
    try:
        path = prompt_variant_state_path()
        if not path.exists():
            return {}
        with path.open(encoding="utf-8") as handle:
            data = json.load(handle)
        return {agent_type: [variant_factory(**row) for row in variants] for agent_type, variants in data.items()}
    except Exception as exc:
        raise GateError("prompt_evolver_state", f"corrupt state file: {prompt_variant_state_path()}", exc) from exc


def save_prompt_variants(variants: dict[str, list[Any]], logger: Any) -> None:
    """Persist prompt variants, logging recoverable write failures.

    Args:
        variants: Prompt variants keyed by agent type.
        logger: Logger-like object used for recoverable failure reporting.
    """
    try:
        path = prompt_variant_state_path(create=True)
        data = {agent_type: [asdict(variant) for variant in rows] for agent_type, rows in variants.items()}
        tmp_path = path.with_name(f"{path.name}.tmp")
        with tmp_path.open("w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2)
        os.replace(tmp_path, path)
    except Exception as exc:
        logger.warning("Could not save prompt variants: %s", exc)


def load_prompt_variant_rows() -> dict[str, list[dict[str, Any]]]:
    """Load persisted prompt variants as raw JSON rows for privacy rights.

    Returns:
        Prompt variant JSON rows keyed by agent type.

    Raises:
        GateError: If persisted state is not a valid variant mapping.
    """
    path = prompt_variant_state_path()
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise GateError("prompt_evolver_state", f"corrupt state file: {path}", None)
    rows: dict[str, list[dict[str, Any]]] = {}
    for agent_type, variants in data.items():
        if not isinstance(variants, list):
            raise GateError("prompt_evolver_state", f"corrupt state file: {path}", None)
        rows[str(agent_type)] = [dict(row) for row in variants if isinstance(row, dict)]
    return rows


def export_prompt_variants_for_subject(subject: str) -> dict[str, Any]:
    """Export prompt variant rows explicitly bound to ``subject``.

    Returns:
        Export payload containing matching records.
    """
    marker = subject.strip()
    records = []
    for agent_type, variants in load_prompt_variant_rows().items():
        records.extend(
            {"agent_type": agent_type, "variant": row} for row in variants if _value_marks_subject(row, marker)
        )
    return {"records": records}


def delete_prompt_variants_for_subject(subject: str) -> int:
    """Delete prompt variant rows explicitly bound to ``subject``.

    Returns:
        Number of deleted prompt variant rows.
    """
    marker = subject.strip()
    if not marker:
        return 0
    rows = load_prompt_variant_rows()
    deleted = 0
    kept: dict[str, list[dict[str, Any]]] = {}
    for agent_type, variants in rows.items():
        kept_rows = [row for row in variants if not _value_marks_subject(row, marker)]
        deleted += len(variants) - len(kept_rows)
        kept[agent_type] = kept_rows
    if deleted:
        path = prompt_variant_state_path(create=True)
        tmp_path = path.with_name(f"{path.name}.tmp")
        with tmp_path.open("w", encoding="utf-8") as handle:
            json.dump(kept, handle, indent=2)
        os.replace(tmp_path, path)
    return deleted


def _value_marks_subject(value: Any, subject: str) -> bool:
    if not subject:
        return False
    if isinstance(value, dict):
        for key in _SUBJECT_FIELD_NAMES:
            if value.get(key) == subject:
                return True
        receipt = value.get("privacy_receipt") or value.get("_privacy_envelope")
        if isinstance(receipt, dict) and receipt.get("subject_id") == subject:
            return True
        return any(_value_marks_subject(item, subject) for item in value.values())
    if isinstance(value, list):
        return any(_value_marks_subject(item, subject) for item in value)
    if isinstance(value, str):
        return any(match.group("id") == subject for match in _SUBJECT_MARKER_RE.finditer(value))
    return False
