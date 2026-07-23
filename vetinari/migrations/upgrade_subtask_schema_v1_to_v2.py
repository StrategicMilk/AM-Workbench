"""Upgrade durable-execution subtask JSON from schema v1 to v2.

The migration is file-based because legacy Ponder subtask exports can live
outside the SQLite migration runner. It reads a JSON object or JSON array,
adds ``schema_version: 2`` to each subtask record, and preserves existing
fields.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def upgrade_subtask_record(record: dict[str, Any]) -> dict[str, Any]:
    """Return a v2 subtask record without mutating the caller's object.

    Returns:
        Schema-v2 subtask record.
    """
    upgraded = dict(record)
    upgraded["schema_version"] = 2
    upgraded.setdefault("status", "pending")
    upgraded.setdefault("evidence_refs", [])
    return upgraded


def upgrade_payload(payload: Any) -> Any:
    """Upgrade a JSON object or list of objects to subtask schema v2.

    Returns:
        Upgraded JSON-compatible payload.

    Raises:
        ValueError: If the input is not a JSON object or list of objects.
    """
    if isinstance(payload, dict):
        return upgrade_subtask_record(payload)
    if isinstance(payload, list) and all(isinstance(item, dict) for item in payload):
        return [upgrade_subtask_record(item) for item in payload]
    raise ValueError("subtask migration input must be a JSON object or list of objects")


def upgrade_file(path: Path) -> None:
    """Rewrite one JSON file with schema-v2 subtask records."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    upgraded = upgrade_payload(payload)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(upgraded, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp_path.replace(path)


def main(argv: list[str] | None = None) -> int:
    """Run the subtask schema migration CLI.

    Returns:
        Process exit code.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path, help="JSON file containing one subtask record or a list of records")
    args = parser.parse_args(argv)
    upgrade_file(args.path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
