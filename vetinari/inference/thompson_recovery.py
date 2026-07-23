"""Recovery helpers for persisted Thompson-sampling state."""

from __future__ import annotations

import json
import logging
import os
import shutil
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


class ThompsonStateError(ValueError):
    """Raised when Thompson state cannot be trusted."""


@dataclass(frozen=True, slots=True)
class ThompsonArmState:
    """Beta-distribution state for one arm."""

    alpha: float = 1.0
    beta: float = 1.0

    def __post_init__(self) -> None:
        if self.alpha <= 0 or self.beta <= 0:
            raise ThompsonStateError("alpha and beta must be positive")


@dataclass(frozen=True, slots=True)
class ThompsonRecoveryResult:
    """Loaded or recovered Thompson state with operator-visible evidence."""

    state: dict[str, ThompsonArmState]
    recovered: bool
    blocked: bool
    evidence: tuple[str, ...]

    def __repr__(self) -> str:
        return (
            f"ThompsonRecoveryResult(recovered={self.recovered!r}, blocked={self.blocked!r}, arms={sorted(self.state)})"
        )


def _default_state(arms: tuple[str, ...]) -> dict[str, ThompsonArmState]:
    if not arms:
        raise ThompsonStateError("at least one arm is required")
    return {arm: ThompsonArmState() for arm in arms}


def save_thompson_state_atomic(path: str | Path, state: Mapping[str, ThompsonArmState]) -> Path:
    """Write Thompson state atomically.

    Args:
        path: Target JSON path.
        state: Arm state keyed by arm id.

    Returns:
        The target path that was written.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "updated_at_utc": datetime.now(timezone.utc).isoformat(),
        "arms": {name: asdict(value) for name, value in state.items()},
    }
    tmp = target.with_name(f".{target.name}.tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(payload, sort_keys=True, indent=2) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, target)
    return target


def load_thompson_state(path: str | Path, *, arms: tuple[str, ...]) -> ThompsonRecoveryResult:
    """Load Thompson state or recover to blocked priors on missing/corrupt state.

    Returns:
        A recovery result with usable prior state and operator evidence.

    Raises:
        ThompsonStateError: if the requested arm list is empty.
    """
    target = Path(path)
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or raw.get("schema_version") != 1:
            raise ThompsonStateError("unsupported Thompson state schema")
        arm_payload = raw.get("arms")
        if not isinstance(arm_payload, dict):
            raise ThompsonStateError("Thompson state missing arms")
        state = {
            arm: ThompsonArmState(
                alpha=float(arm_payload[arm]["alpha"]),
                beta=float(arm_payload[arm]["beta"]),
            )
            for arm in arms
        }
        return ThompsonRecoveryResult(state=state, recovered=False, blocked=False, evidence=(f"loaded:{target}",))
    except (FileNotFoundError, json.JSONDecodeError, KeyError, TypeError, ThompsonStateError) as exc:
        evidence = [f"recovered:{type(exc).__name__}:{exc}"]
        if target.exists():
            quarantine = target.with_suffix(target.suffix + ".corrupt")
            try:
                shutil.copy2(target, quarantine)
                evidence.append(f"quarantined:{quarantine}")
            except OSError as copy_exc:
                logger.warning("Could not quarantine corrupt Thompson state at %s", target, exc_info=True)
                evidence.append(f"quarantine_failed:{copy_exc}")
        return ThompsonRecoveryResult(
            state=_default_state(arms),
            recovered=True,
            blocked=True,
            evidence=tuple(evidence),
        )


__all__ = [
    "ThompsonArmState",
    "ThompsonRecoveryResult",
    "ThompsonStateError",
    "load_thompson_state",
    "save_thompson_state_atomic",
]
