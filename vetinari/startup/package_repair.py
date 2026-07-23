"""Package provenance-aware startup repair helpers."""

from __future__ import annotations

import importlib.metadata
import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RepairResult:
    """Result of a startup package repair attempt."""

    success: bool
    provenance: str
    reason: str = ""


def detect_provenance(package_root: Path | None = None) -> str:
    """Classify whether this process is running from source, an install, or both.

    Args:
        package_root: Optional repository/package root override.

    Returns:
        One of ``editable``, ``installed``, ``ambiguous``, or ``unknown``.
    """
    root = (package_root or Path(__file__).resolve().parents[2]).resolve()
    source_markers = (root / "pyproject.toml").exists() and (root / "vetinari").is_dir()
    try:
        distribution = importlib.metadata.distribution("vetinari")
    except importlib.metadata.PackageNotFoundError:
        distribution = None

    if source_markers and distribution is not None:
        dist_root = Path(str(distribution.locate_file(""))).resolve()
        try:
            dist_root.relative_to(root)
            return "editable"
        except ValueError:
            logger.warning("Ambiguous vetinari package provenance: source root and distribution root differ")
            return "ambiguous"
    if source_markers:
        return "editable"
    if distribution is not None:
        return "installed"
    return "unknown"


def attempt_repair(
    *,
    repair_action: Callable[[str], object] | None = None,
    package_root: Path | None = None,
) -> RepairResult:
    """Attempt a bounded startup repair only after package provenance is known.

    Args:
        repair_action: Optional mutation callback receiving the proven package provenance.
        package_root: Optional repository/package root override.

    Returns:
        Repair attempt result with provenance and optional failure reason.
    """
    provenance = detect_provenance(package_root)
    if provenance in {"ambiguous", "unknown"}:
        return RepairResult(False, provenance, "package provenance is not safe for mutation")
    if repair_action is not None:
        repair_action(provenance)
    return RepairResult(True, provenance)


__all__ = ["RepairResult", "attempt_repair", "detect_provenance"]
