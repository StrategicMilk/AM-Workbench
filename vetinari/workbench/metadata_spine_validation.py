"""Dependency validation helpers for the Workbench metadata spine."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from vetinari.workbench.metadata_spine_records import WorkbenchSpineCorrupt


class _SeenRecords:
    """Tracks records already replayed while rebuilding the SQLite index."""

    def __init__(self) -> None:
        self.assets: set[str] = set()
        self.asset_revisions: set[tuple[str, str]] = set()
        self.runs: set[str] = set()
        self.evals: set[str] = set()
        self.proposals: set[str] = set()
        self.promotions: set[str] = set()
        self.leases: set[str] = set()

    def add(self, kind: str, record_id: str, payload: dict[str, Any]) -> None:
        """Record a replayed append-log row for later dependency checks.

        Args:
            kind: Kind discriminator used to select the operation branch.
            record_id: Typed record consumed by the operation.
            payload: Payload data validated or transformed by the operation.
        """
        if kind == "asset":
            self.assets.add(record_id)
            self.asset_revisions.add((record_id, str(payload.get("revision", ""))))
        elif kind == "run":
            self.runs.add(record_id)
        elif kind == "eval":
            self.evals.add(record_id)
        elif kind == "proposal":
            self.proposals.add(record_id)
        elif kind == "promotion":
            self.promotions.add(record_id)
        elif kind == "lease":
            self.leases.add(record_id)

    def remove(self, kind: str, record_id: str) -> None:
        """Forget a deleted record while retaining tombstone knowledge in the log.

        Args:
            kind: Record family to remove from the active validation index.
            record_id: Stable record identifier to forget.
        """
        if kind == "asset":
            self.assets.discard(record_id)
            self.asset_revisions = {
                (asset_id, revision) for asset_id, revision in self.asset_revisions if asset_id != record_id
            }
        elif kind == "run":
            self.runs.discard(record_id)
        elif kind == "eval":
            self.evals.discard(record_id)
        elif kind == "proposal":
            self.proposals.discard(record_id)
        elif kind == "promotion":
            self.promotions.discard(record_id)
        elif kind == "lease":
            self.leases.discard(record_id)


class _WorkbenchSpineValidationMixin:
    """Runtime and replay dependency checks for spine records."""

    if TYPE_CHECKING:
        _require_conn: Any
        _write_lock: Any

    def _validate_dependencies(self, kind: str, payload: dict[str, Any]) -> None:
        if kind == "run":
            for asset_id, revision in payload.get("asset_revisions", ()):
                if not self._asset_revision_exists(asset_id, revision):
                    raise WorkbenchSpineCorrupt(f"orphan asset_id {asset_id!r} revision {revision!r}")
            lease_id = payload.get("lease_id", "")
            if lease_id and not self._exists("lease", lease_id):
                raise WorkbenchSpineCorrupt(f"orphan lease_id {lease_id!r}")
        elif kind == "trace":
            run_id = payload.get("run_id", "")
            if not self._exists("run", run_id):
                raise WorkbenchSpineCorrupt(f"orphan run_id {run_id!r}")
        elif kind == "eval":
            run_id = payload.get("run_id", "")
            if not self._exists("run", run_id):
                raise WorkbenchSpineCorrupt(f"orphan run_id {run_id!r}")
            if not self._asset_revision_exists(payload.get("asset_id", ""), payload.get("asset_revision", "")):
                raise WorkbenchSpineCorrupt(f"orphan asset_id {payload.get('asset_id', '')!r}")
        elif kind == "proposal":
            for asset_id, revision in payload.get("affected_revisions", ()):
                if not self._asset_revision_exists(asset_id, revision):
                    raise WorkbenchSpineCorrupt(f"orphan asset_id {asset_id!r} revision {revision!r}")
            for eval_payload in payload.get("pre_promotion_evals", ()):
                eval_id = eval_payload.get("eval_id", "")
                if not self._exists("eval", eval_id):
                    raise WorkbenchSpineCorrupt(f"orphan eval_id {eval_id!r}")
        elif kind == "lease":
            run_id = payload.get("requested_for_run_id", "")
            if run_id and not self._exists("run", run_id):
                raise WorkbenchSpineCorrupt(f"orphan run_id {run_id!r}")
        elif kind == "promotion":
            proposal_id = payload.get("proposal_id", "")
            if not self._exists("proposal", proposal_id):
                raise WorkbenchSpineCorrupt(f"orphan proposal_id {proposal_id!r}")
        elif kind == "delete":
            target_kind = payload.get("target_kind", "")
            target_record_id = payload.get("target_record_id", "")
            if not target_kind or not target_record_id:
                raise WorkbenchSpineCorrupt("delete tombstone missing target")
            if not self._exists(str(target_kind), str(target_record_id)):
                raise WorkbenchSpineCorrupt(f"delete target missing {target_kind!r} {target_record_id!r}")

    @staticmethod
    def _validate_dependencies_against_seen(
        kind: str,
        payload: dict[str, Any],
        seen: _SeenRecords,
    ) -> None:
        if kind == "run":
            for asset_id, revision in payload.get("asset_revisions", ()):
                if (asset_id, revision) not in seen.asset_revisions:
                    raise WorkbenchSpineCorrupt(f"orphan asset_id {asset_id!r} revision {revision!r}")
            lease_id = payload.get("lease_id", "")
            if lease_id and lease_id not in seen.leases:
                raise WorkbenchSpineCorrupt(f"orphan lease_id {lease_id!r}")
        elif kind == "trace" and payload.get("run_id", "") not in seen.runs:
            raise WorkbenchSpineCorrupt(f"orphan run_id {payload.get('run_id', '')!r}")
        elif kind == "eval":
            if payload.get("run_id", "") not in seen.runs:
                raise WorkbenchSpineCorrupt(f"orphan run_id {payload.get('run_id', '')!r}")
            asset_ref = (payload.get("asset_id", ""), payload.get("asset_revision", ""))
            if asset_ref not in seen.asset_revisions:
                raise WorkbenchSpineCorrupt(f"orphan asset_id {payload.get('asset_id', '')!r}")
        elif kind == "proposal":
            for asset_id, revision in payload.get("affected_revisions", ()):
                if (asset_id, revision) not in seen.asset_revisions:
                    raise WorkbenchSpineCorrupt(f"orphan asset_id {asset_id!r} revision {revision!r}")
            for eval_payload in payload.get("pre_promotion_evals", ()):
                eval_id = eval_payload.get("eval_id", "")
                if eval_id not in seen.evals:
                    raise WorkbenchSpineCorrupt(f"orphan eval_id {eval_id!r}")
        elif kind == "lease":
            run_id = payload.get("requested_for_run_id", "")
            if run_id and run_id not in seen.runs:
                raise WorkbenchSpineCorrupt(f"orphan run_id {run_id!r}")
        elif kind == "promotion" and payload.get("proposal_id", "") not in seen.proposals:
            raise WorkbenchSpineCorrupt(f"orphan proposal_id {payload.get('proposal_id', '')!r}")
        elif kind == "delete":
            target_kind = str(payload.get("target_kind", ""))
            target_record_id = str(payload.get("target_record_id", ""))
            if not target_kind or not target_record_id:
                raise WorkbenchSpineCorrupt("delete tombstone missing target")
            exists = {
                "asset": target_record_id in seen.assets,
                "run": target_record_id in seen.runs,
                "eval": target_record_id in seen.evals,
                "proposal": target_record_id in seen.proposals,
                "promotion": target_record_id in seen.promotions,
                "lease": target_record_id in seen.leases,
            }.get(target_kind, False)
            if not exists:
                raise WorkbenchSpineCorrupt(f"delete target missing {target_kind!r} {target_record_id!r}")

    def _exists(self, kind: str, record_id: str) -> bool:
        with self._write_lock:
            row = (
                self
                ._require_conn()
                .execute(
                    "SELECT 1 FROM records WHERE kind = ? AND record_id = ? LIMIT 1",
                    (kind, record_id),
                )
                .fetchone()
            )
        return row is not None

    def _asset_revision_exists(self, asset_id: str, revision: str) -> bool:
        with self._write_lock:
            row = (
                self
                ._require_conn()
                .execute(
                    "SELECT payload FROM records WHERE kind = 'asset' AND record_id = ? LIMIT 1",
                    (asset_id,),
                )
                .fetchone()
            )
        if row is None:
            return False
        payload = json.loads(row[0])
        return payload.get("revision") == revision
