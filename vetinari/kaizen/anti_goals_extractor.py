"""Extract recurring Inspector failure modes into the anti-goals pool.

The kaizen weekly loop uses this module to turn recent Inspector outcome
records into shard-generation anti-goals. Recurring typed failure modes become
candidate reminders that Foreman can pin into future shard frontmatter.
"""

from __future__ import annotations

import json
import logging
import os
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

import yaml

logger = logging.getLogger(__name__)


_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_ANTI_GOALS_POOL_PATH = _PROJECT_ROOT / "outputs" / "kaizen" / "anti_goals_pool.yaml"


def _ensure_path_under_project(path: Path) -> Path:
    """Q-L1 path-confinement guard for write_pool destinations.

    Resolves ``path`` to an absolute path under the project root or under
    the system temp dir (so pytest fixtures using ``tmp_path`` keep working).
    Relative paths are anchored to the project root before resolution.
    Absolute paths that resolve outside both allowlist roots are rejected.
    """
    import tempfile

    resolved = path if path.is_absolute() else (_PROJECT_ROOT / path)
    resolved = resolved.resolve()
    project_root = _PROJECT_ROOT.resolve()
    temp_roots = {
        Path(tempfile.gettempdir()).resolve(),
        *(Path(value).resolve() for value in (os.environ.get("TMP"), os.environ.get("TEMP")) if value),
    }
    if os.name == "nt":
        local_appdata = os.environ.get("LOCALAPPDATA")
        if local_appdata:
            temp_roots.add((Path(local_appdata) / "Temp").resolve())
    for permitted_root in (project_root, *sorted(temp_roots, key=str)):
        try:
            resolved.relative_to(permitted_root)
            return resolved
        except ValueError as exc:
            logger.debug(
                "Resolved anti-goals output %s is outside permitted root %s: %s", resolved, permitted_root, exc
            )
            continue
    raise ValueError(
        f"write_pool(output_path=...) must resolve under the project root or system tempdir; refusing path: {resolved}"
    )


def _parse_occurred_at(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = f"{normalized[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        logger.debug("Ignoring outcome with unparsable occurred_at=%r: %s", value, exc)
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _mode_counts(
    outcomes_path: str | Path,
    *,
    days: int,
    as_of: datetime,
) -> Counter[str]:
    path = Path(outcomes_path)
    if not path.exists():
        return Counter()

    window_start = as_of - timedelta(days=days)
    counts: Counter[str] = Counter()
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                record: dict[str, Any] = json.loads(line)
            except json.JSONDecodeError as exc:
                logger.warning("Skipping malformed outcome JSONL record in %s: %s", path, exc)
                continue
            mode = record.get("typed_failure_mode")
            if not isinstance(mode, str) or not mode.strip():
                continue
            occurred_at = _parse_occurred_at(record.get("occurred_at"))
            if occurred_at is None:
                continue
            if window_start <= occurred_at <= as_of:
                counts[mode.strip()] += 1
    return counts


def extract_pool(
    outcomes_path: str | Path,
    days: int = 7,
    min_occurrences: int = 2,
    *,
    as_of: datetime | None = None,
    include_counts: bool = False,
) -> list[str] | list[dict[str, int | str]]:
    """Extract recurring failure modes from Inspector outcome records.

    Args:
        outcomes_path: Path to the JSONL outcomes file. Missing files return an
            empty list.
        days: Rolling window in days.
        min_occurrences: Minimum occurrences required for a mode to enter the
            pool.
        as_of: End of the rolling window. Defaults to the current UTC time.
        include_counts: When true, include recurrence counts for kaizen metrics.

    Returns:
        A sorted list of failure-mode strings, or sorted mode/count records when
        include_counts is true.
    """
    reference_time = (as_of or datetime.now(timezone.utc)).astimezone(timezone.utc)
    counts = _mode_counts(outcomes_path, days=days, as_of=reference_time)
    included = sorted(mode for mode, count in counts.items() if count >= min_occurrences)
    if include_counts:
        return [{"mode": mode, "count": counts[mode]} for mode in included]
    return included


def write_pool(
    outcomes_path: str | Path,
    output_path: str | Path = DEFAULT_ANTI_GOALS_POOL_PATH,
    days: int = 7,
    min_occurrences: int = 2,
    *,
    as_of: datetime | None = None,
) -> None:
    """Write the anti-goals pool YAML using an atomic single-writer replace.

    Args:
        outcomes_path: Path to the JSONL outcomes file.
        output_path: Destination YAML path.
        days: Rolling window in days.
        min_occurrences: Minimum occurrences required for inclusion.
        as_of: End of the rolling window. Defaults to the current UTC time.
    """
    reference_time = (as_of or datetime.now(timezone.utc)).astimezone(timezone.utc)
    failure_modes = extract_pool(
        outcomes_path,
        days=days,
        min_occurrences=min_occurrences,
        as_of=reference_time,
    )
    destination = _ensure_path_under_project(Path(output_path))
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": reference_time.isoformat().replace("+00:00", "Z"),
        "failure_modes": failure_modes,
    }

    with NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=str(destination.parent),
        delete=False,
        prefix=f".{destination.name}.",
        suffix=".tmp",
    ) as handle:
        temp_path = Path(handle.name)
        yaml.safe_dump(payload, handle, sort_keys=False)
    os.replace(temp_path, destination)
