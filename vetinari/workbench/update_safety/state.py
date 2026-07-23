"""Skipped-version state with atomic replace semantics."""

from __future__ import annotations

import json
import os
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from vetinari.workbench.spine_consumers import record_asset_written
from vetinari.workbench.update_safety.contracts import SkippedVersionRecord, UpdateSafetyError

SCHEMA_VERSION = "1.0"
DEFAULT_SKIPPED_STATE_PATH = Path("outputs") / "workbench" / "update-safety" / "skipped_versions.json"


@dataclass(frozen=True, slots=True)
class SkippedVersionState:
    """JSON-safe skipped-version state."""

    schema_version: str
    revision: int
    records: tuple[SkippedVersionRecord, ...]

    def versions(self) -> tuple[str, ...]:
        return tuple(record.version for record in self.records)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "revision": self.revision,
            "records": [record.to_dict() for record in self.records],
        }


class SkippedVersionStore:
    """Atomic skipped-version state store."""

    def __init__(self, path: str | Path = DEFAULT_SKIPPED_STATE_PATH) -> None:
        self.path = Path(path)

    def load(self) -> SkippedVersionState:
        """Execute the load operation.

        Returns:
            SkippedVersionState value produced by load().

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        if not self.path.exists():
            return SkippedVersionState(schema_version=SCHEMA_VERSION, revision=0, records=())
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise UpdateSafetyError(f"skipped_state_unreadable:{type(exc).__name__}") from exc
        if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION:
            raise UpdateSafetyError("skipped_state_schema_mismatch")
        records_raw = payload.get("records")
        if not isinstance(records_raw, list):
            raise UpdateSafetyError("skipped_state_records_invalid")
        records = tuple(
            SkippedVersionRecord(
                version=str(row["version"]),
                channel=str(row["channel"]),
                skipped_at_utc=str(row["skipped_at_utc"]),
                approval_decision_id=str(row["approval_decision_id"]),
                reason=str(row.get("reason", "")),
            )
            for row in records_raw
            if isinstance(row, dict)
        )
        return SkippedVersionState(
            schema_version=SCHEMA_VERSION,
            revision=int(payload.get("revision", -1)),
            records=records,
        )

    def record_skip(
        self,
        *,
        version: str,
        channel: str,
        approval_decision_id: str,
        reason: str = "",
        expected_revision: int | None = None,
    ) -> SkippedVersionState:
        """Execute the record skip operation.

        Returns:
            Outcome produced by record_skip().

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        state = self.load()
        if expected_revision is not None and state.revision != expected_revision:
            raise UpdateSafetyError("skipped_state_revision_conflict")
        clean_version = str(version).strip()
        clean_decision = str(approval_decision_id).strip()
        if not clean_version or not clean_decision:
            raise UpdateSafetyError("skipped_state_missing_required_fields")
        records = [record for record in state.records if record.version != clean_version]
        records.append(
            SkippedVersionRecord(
                version=clean_version,
                channel=str(channel),
                skipped_at_utc=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                approval_decision_id=clean_decision,
                reason=str(reason),
            )
        )
        new_state = SkippedVersionState(
            schema_version=SCHEMA_VERSION,
            revision=state.revision + 1,
            records=tuple(records),
        )
        self._atomic_write(new_state)
        return new_state

    def _atomic_write(self, state: SkippedVersionState) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(state.to_dict(), handle, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            with suppress(OSError):
                os.fsync(handle.fileno())
        tmp.replace(self.path)
        # spine_consumers invokes get_spine() and absorbs observability failures.
        record_asset_written(
            asset_id="update-safety-skipped-state",
            kind="tool",
            project_id="default",
            path=str(self.path),
            redact_fields=["path"],
        )


__all__ = ["DEFAULT_SKIPPED_STATE_PATH", "SkippedVersionState", "SkippedVersionStore"]
