"""Persistent recovery state for the first-run init wizard."""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

SCHEMA_VERSION = "vetinari.init-wizard-state.v1"
LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class InitWizardState:
    """Durable progress marker for an interrupted init wizard run."""

    schema_version: str
    step: str
    status: str
    detail: str
    updated_at: str

    def __repr__(self) -> str:
        """Return a compact debug representation with the current step."""
        return f"InitWizardState(step={self.step!r}, status={self.status!r}, updated_at={self.updated_at!r})"


def state_path_for(config_path: Path | None) -> Path:
    """Return the recovery-state path associated with a config path.

    Returns:
        Path to the init wizard recovery-state file.
    """
    config = config_path or Path.home() / ".vetinari" / "config.yaml"
    return config.parent / ".init-wizard-state.json"


def load_wizard_state(config_path: Path | None) -> InitWizardState | None:
    """Load existing wizard state, returning None for absent or malformed state.

    Returns:
        Parsed wizard state, or ``None`` when no valid recovery state exists.
    """
    path = state_path_for(config_path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        LOGGER.warning("init_wizard_state_unreadable", extra={"path": str(path), "error": str(exc)})
        return None
    if raw.get("schema_version") != SCHEMA_VERSION:
        return None
    try:
        return InitWizardState(
            schema_version=str(raw["schema_version"]),
            step=str(raw["step"]),
            status=str(raw["status"]),
            detail=str(raw.get("detail", "")),
            updated_at=str(raw["updated_at"]),
        )
    except KeyError as exc:
        LOGGER.warning("init_wizard_state_missing_field", extra={"path": str(path), "field": str(exc)})
        return None


def save_wizard_state(config_path: Path | None, *, step: str, status: str, detail: str = "") -> Path:
    """Atomically write wizard recovery state.

    Returns:
        Path to the written recovery-state file.
    """
    path = state_path_for(config_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    state = InitWizardState(
        schema_version=SCHEMA_VERSION,
        step=step,
        status=status,
        detail=detail,
        updated_at=datetime.now(UTC).isoformat(),
    )
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(asdict(state), indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)
    return path


def clear_wizard_state(config_path: Path | None) -> None:
    """Remove wizard recovery state after a clean completion."""
    path = state_path_for(config_path)
    try:
        path.unlink()
    except FileNotFoundError as exc:
        LOGGER.warning("init_wizard_state_already_absent", extra={"path": str(path), "error": str(exc)})
        return


__all__ = ["InitWizardState", "clear_wizard_state", "load_wizard_state", "save_wizard_state", "state_path_for"]
