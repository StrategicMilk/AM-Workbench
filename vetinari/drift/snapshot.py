"""Drift snapshot path helpers."""

from __future__ import annotations

from pathlib import Path

from vetinari.constants import VETINARI_STATE_DIR
from vetinari.security.fail_closed import confine_to_root


def get_snapshot_path() -> Path:
    """Return the canonical drift snapshot path.

    Returns:
        Snapshot path under the local Vetinari state directory.
    """
    return confine_to_root(VETINARI_STATE_DIR, Path("drift") / "snapshot.json")


__all__ = ["get_snapshot_path"]
