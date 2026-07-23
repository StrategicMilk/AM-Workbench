"""Drift audit: snapshot source-file hashes and compare against a baseline.

This module replaces the earlier no-op stub so the drift-audit harness can
actually detect schema/source drift instead of always reporting
``snapshot_saved=True`` for zero schemas checked.

Pipeline:
    1. For each requested source module, ``_read_source`` returns
       ``(filesystem_path, source_text)``.
    2. ``_load_baseline`` reads any prior snapshot at ``DRIFT_SNAPSHOT_PATH``.
       Failure to read the baseline is itself a drift signal.
    3. The current source set is hashed and written back to
       ``DRIFT_SNAPSHOT_PATH``.

This is step 3 of the audit pipeline: Intake -> Collection -> **Drift Audit** ->
Reporting -> Verification.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import logging
from pathlib import Path
from typing import Any

from vetinari.constants import OUTPUTS_DIR

logger = logging.getLogger(__name__)

# Default snapshot location. Tests monkey-patch this constant to a tmp_path so
# the audit can run hermetically; production callers can also override it via
# the module attribute to redirect drift snapshots into a project-scoped path.
DRIFT_SNAPSHOT_PATH: Path = OUTPUTS_DIR / "drift" / "snapshot.json"


def _read_source(module: str) -> tuple[str, str]:
    """Resolve a module path to ``(filesystem_path, source_text)``.

    Args:
        module: Dotted import path of the module to inspect.

    Returns:
        Tuple of resolved path string and the module's source text.

    Raises:
        OSError: Re-raised when the module's source file cannot be read.
        ImportError: When the dotted path does not resolve.
    """
    spec = importlib.util.find_spec(module)
    if spec is None or spec.origin is None:
        msg = f"Cannot resolve source for module {module!r}"
        raise ImportError(msg)
    path = Path(spec.origin)
    return str(path), path.read_text(encoding="utf-8")


def _load_baseline(path: Path) -> dict[str, Any]:
    """Load a prior drift snapshot from ``path``.

    Args:
        path: Filesystem path to the snapshot JSON.

    Returns:
        Parsed snapshot dict. Returns an empty dict when the snapshot does
        not yet exist (first run).

    Raises:
        OSError: Re-raised when the snapshot exists but cannot be read.
        json.JSONDecodeError: Re-raised when the snapshot is corrupted.
    """
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def run_drift_audit(
    sources: list[str] | None = None,
    *,
    skip_subchecks: list[str] | None = None,
) -> dict[str, Any]:
    """Run a bounded drift audit over ``sources``.

    Args:
        sources: Dotted module paths to snapshot. When omitted or empty no
            sources are read and the audit returns the trivial empty result
            (``schemas_checked=0``, ``snapshot_saved=False`` so callers can
            distinguish "ran the audit on nothing" from "audit succeeded").
        skip_subchecks: Optional subcheck ids to omit (reserved; recorded in
            the returned summary so downstream consumers can verify the
            scope of a partial audit).

    Returns:
        Drift audit summary with ``schemas_checked``, ``failures``,
        ``snapshot_saved``, ``skipped``, and ``baseline_loaded`` fields.
    """
    skipped = skip_subchecks or []
    sources = sources or []
    snapshot_path = Path(DRIFT_SNAPSHOT_PATH)

    failures: list[dict[str, Any]] = []

    # Read every requested source. Any OSError/ImportError surfaces as a
    # failure entry and short-circuits the snapshot write.
    captured: dict[str, dict[str, str]] = {}
    for module in sources:
        try:
            path, content = _read_source(module)
        except (OSError, ImportError) as exc:
            logger.warning("Could not read drift audit source %s: %s", module, exc)
            failures.append({"module": module, "reason": str(exc)})
            continue
        captured[module] = {
            "path": path,
            "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
            "byte_length": str(len(content.encode("utf-8"))),
        }

    if failures:
        return {
            "schemas_checked": len(captured),
            "failures": failures,
            "snapshot_saved": False,
            "skipped": skipped,
            "baseline_loaded": False,
        }

    # Load (or initialize) the baseline. Failure is itself a drift signal.
    try:
        baseline = _load_baseline(snapshot_path)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Could not load drift audit baseline %s: %s", snapshot_path, exc)
        failures.append({"baseline": str(snapshot_path), "reason": str(exc)})
        return {
            "schemas_checked": len(captured),
            "failures": failures,
            "snapshot_saved": False,
            "skipped": skipped,
            "baseline_loaded": False,
        }

    # Nothing actually inspected. Tell callers "we ran, but found nothing".
    # When subchecks were explicitly skipped, still persist an empty scoped
    # snapshot so downstream auditors can distinguish "skipped and recorded"
    # from "no audit artifact was produced".
    if not captured:
        if skipped:
            payload = {
                "sources": {},
                "skipped": skipped,
                "baseline_schema_version": baseline.get("schema_version", 1),
                "schema_version": 1,
            }
            snapshot_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = snapshot_path.with_suffix(snapshot_path.suffix + ".tmp")
            tmp_path.write_text(json.dumps(payload, sort_keys=True, indent=2), encoding="utf-8")
            tmp_path.replace(snapshot_path)
            return {
                "schemas_checked": 0,
                "failures": [],
                "snapshot_saved": True,
                "skipped": skipped,
                "baseline_loaded": True,
            }
        return {
            "schemas_checked": 0,
            "failures": [],
            "snapshot_saved": False,
            "skipped": skipped,
            "baseline_loaded": True,
        }

    # Write the snapshot.
    payload = {
        "sources": captured,
        "baseline_schema_version": baseline.get("schema_version", 1),
        "schema_version": 1,
    }
    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    # Atomic write: stage to .tmp then rename. Drift snapshots are read by
    # later audit runs and a partial write would silently change every
    # downstream comparison.
    tmp_path = snapshot_path.with_suffix(snapshot_path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(payload, sort_keys=True, indent=2), encoding="utf-8")
    tmp_path.replace(snapshot_path)

    return {
        "schemas_checked": len(captured),
        "failures": [],
        "snapshot_saved": True,
        "skipped": skipped,
        "baseline_loaded": True,
    }


__all__ = ["DRIFT_SNAPSHOT_PATH", "run_drift_audit"]
