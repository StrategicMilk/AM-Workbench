"""Governed knowledge backfeed inbox for AM Workbench.

Backfeed proposals are candidates, not authority. The service writes an
append-only audit log and only exposes approved, non-superseded records to
trusted context, export, or eval consumers.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from vetinari.workbench.knowledge.backfeed_records import (
    CONSUMER_KINDS,
    SCHEMA_VERSION,
    BackfeedChange,
    BackfeedProposal,
    BackfeedScope,
    BackfeedSource,
    BackfeedStatus,
    BackfeedWriteResult,
    KnowledgeBackfeedError,
    _decision_payload,
    _decision_payload_from_event,
    _proposal_payload,
    _require_mapping,
    _require_text,
    _trusted_record_from_proposal,
    _trusted_record_id,
)


class KnowledgeBackfeedService:
    """Append-only governance boundary for knowledge backfeed state."""

    def __init__(self, state_path: str | Path, *, max_state_age_seconds: float | None = None) -> None:
        self._state_path = Path(state_path)
        self._max_state_age_seconds = max_state_age_seconds
        self._lock = threading.RLock()

    def append_proposal(self, proposal: BackfeedProposal) -> BackfeedWriteResult:
        """Append a proposed change without making it trusted context.

        Returns:
            BackfeedWriteResult value produced by append_proposal().
        """
        with self._lock:
            events = self._read_events()
            self._check_proposal_identity(events, proposal)
            if any(_proposal_payload(event).get("proposal_id") == proposal.proposal_id for event in events):
                return BackfeedWriteResult(BackfeedStatus.PROPOSED, changed=False, proposal_id=proposal.proposal_id)
            self._append_event("proposal", proposal.to_payload())
        return BackfeedWriteResult(BackfeedStatus.PROPOSED, changed=True, proposal_id=proposal.proposal_id)

    def approve_proposal(self, proposal_id: str, *, decided_by: str, rationale: str) -> BackfeedWriteResult:
        """Approve one proposal and append the matching trusted record.

        Returns:
            BackfeedWriteResult value produced by approve_proposal().

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        _require_text(decided_by, "decided_by")
        _require_text(rationale, "rationale")
        with self._lock:
            events = self._read_events()
            proposal = self._require_proposal(events, proposal_id)
            existing_status = self._terminal_status(events, proposal_id)
            if existing_status is BackfeedStatus.APPROVED:
                record_id = _trusted_record_id(proposal_id)
                if self._trusted_record_exists(events, proposal_id):
                    return BackfeedWriteResult(
                        existing_status, changed=False, proposal_id=proposal_id, record_id=record_id
                    )
                trusted_record = _trusted_record_from_proposal(proposal, decided_by=decided_by)
                self._append_event("trusted_record", trusted_record)
                return BackfeedWriteResult(existing_status, changed=True, proposal_id=proposal_id, record_id=record_id)
            if existing_status is not None:
                raise KnowledgeBackfeedError("proposal_already_decided", proposal_id=proposal_id)
            trusted_record = _trusted_record_from_proposal(proposal, decided_by=decided_by)
            decision = _decision_payload(
                proposal_id,
                decision=BackfeedStatus.APPROVED,
                decided_by=decided_by,
                rationale=rationale,
                trusted_record_id=str(trusted_record["record_id"]),
            )
            self._append_events(("decision", decision), ("trusted_record", trusted_record))
        return BackfeedWriteResult(
            BackfeedStatus.APPROVED,
            changed=True,
            proposal_id=proposal_id,
            record_id=str(trusted_record["record_id"]),
        )

    def reject_proposal(self, proposal_id: str, *, decided_by: str, rationale: str) -> BackfeedWriteResult:
        """Reject one proposal and preserve the audit trail without trusted context.

        Returns:
            BackfeedWriteResult value produced by reject_proposal().

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        _require_text(decided_by, "decided_by")
        _require_text(rationale, "rationale")
        with self._lock:
            events = self._read_events()
            self._require_proposal(events, proposal_id)
            existing_status = self._terminal_status(events, proposal_id)
            if existing_status is BackfeedStatus.REJECTED:
                return BackfeedWriteResult(existing_status, changed=False, proposal_id=proposal_id)
            if existing_status is not None:
                raise KnowledgeBackfeedError("proposal_already_decided", proposal_id=proposal_id)
            self._append_event(
                "decision",
                _decision_payload(
                    proposal_id,
                    decision=BackfeedStatus.REJECTED,
                    decided_by=decided_by,
                    rationale=rationale,
                ),
            )
        return BackfeedWriteResult(BackfeedStatus.REJECTED, changed=True, proposal_id=proposal_id)

    def supersede_proposal(
        self,
        proposal_id: str,
        *,
        superseded_by: str,
        decided_by: str,
        rationale: str,
    ) -> BackfeedWriteResult:
        """Supersede an approved record with an auditable decision.

        Returns:
            BackfeedWriteResult value produced by supersede_proposal().

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        _require_text(superseded_by, "superseded_by")
        _require_text(decided_by, "decided_by")
        _require_text(rationale, "rationale")
        with self._lock:
            events = self._read_events()
            self._require_proposal(events, proposal_id)
            if self._terminal_status(events, superseded_by) is not BackfeedStatus.APPROVED:
                raise KnowledgeBackfeedError("superseding_proposal_not_approved", proposal_id=superseded_by)
            existing_status = self._terminal_status(events, proposal_id)
            if existing_status is BackfeedStatus.SUPERSEDED:
                return BackfeedWriteResult(existing_status, changed=False, proposal_id=proposal_id)
            if existing_status is not BackfeedStatus.APPROVED:
                raise KnowledgeBackfeedError("proposal_must_be_approved_before_supersede", proposal_id=proposal_id)
            self._append_event(
                "decision",
                _decision_payload(
                    proposal_id,
                    decision=BackfeedStatus.SUPERSEDED,
                    decided_by=decided_by,
                    rationale=rationale,
                    superseded_by=superseded_by,
                ),
            )
        return BackfeedWriteResult(BackfeedStatus.SUPERSEDED, changed=True, proposal_id=proposal_id)

    def audit_events(self) -> tuple[dict[str, Any], ...]:
        """Return every proposal, decision, and trusted-record event."""
        return tuple(self._read_events())

    def trusted_context_records(self) -> tuple[dict[str, Any], ...]:
        """Return only approved active records that may alter trusted context."""
        return self.consumer_records("trusted_context")

    def export_records(self) -> tuple[dict[str, Any], ...]:
        """Return approved active records visible to export consumers."""
        return self.consumer_records("export")

    def eval_records(self) -> tuple[dict[str, Any], ...]:
        """Return approved active records visible to eval consumers."""
        return self.consumer_records("eval")

    def consumer_records(self, consumer: str) -> tuple[dict[str, Any], ...]:
        """Return approved active records allowed for a specific consumer.

        Returns:
            tuple[dict[str, Any], ...] value produced by consumer_records().

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        if consumer not in CONSUMER_KINDS:
            raise KnowledgeBackfeedError("consumer_unknown")
        events = self._read_events()
        superseded = {
            _decision_payload_from_event(event)["proposal_id"]
            for event in events
            if event.get("kind") == "decision"
            and _decision_payload_from_event(event).get("decision") == BackfeedStatus.SUPERSEDED.value
        }
        records = []
        for event in events:
            if event.get("kind") != "trusted_record":
                continue
            record = dict(_require_mapping(event, "payload"))
            if record["proposal_id"] in superseded:
                continue
            allowed = set(record["scope"]["allowed_consumers"])
            if consumer in allowed:
                records.append(record)
        return tuple(records)

    def to_payload(self) -> dict[str, Any]:
        """Return a schema-shaped state snapshot for diagnostics and export.

        Returns:
            dict[str, Any] value produced by to_payload().
        """
        events = self._read_events()
        return {
            "schema_version": SCHEMA_VERSION,
            "proposals": [_proposal_payload(event) for event in events if event.get("kind") == "proposal"],
            "decisions": [_decision_payload_from_event(event) for event in events if event.get("kind") == "decision"],
            "trusted_records": [dict(event["payload"]) for event in events if event.get("kind") == "trusted_record"],
        }

    def _read_events(self) -> list[dict[str, Any]]:
        self._assert_state_available()
        if not self._state_path.exists():
            return []
        events: list[dict[str, Any]] = []
        try:
            with self._state_path.open("r", encoding="utf-8") as handle:
                for line_number, line in enumerate(handle, start=1):
                    if not line.strip():
                        continue
                    event = json.loads(line)
                    if not isinstance(event, dict) or event.get("schema_version") != SCHEMA_VERSION:
                        raise KnowledgeBackfeedError(f"state_corrupt_line_{line_number}")
                    if event.get("kind") not in {"proposal", "decision", "trusted_record"}:
                        raise KnowledgeBackfeedError(f"state_corrupt_line_{line_number}")
                    if not isinstance(event.get("payload"), dict):
                        raise KnowledgeBackfeedError(f"state_corrupt_line_{line_number}")
                    events.append(event)
        except json.JSONDecodeError as exc:
            raise KnowledgeBackfeedError("state_corrupt") from exc
        except OSError as exc:
            raise KnowledgeBackfeedError("state_unavailable") from exc
        return events

    def _assert_state_available(self) -> None:
        if self._state_path.exists() and self._state_path.is_dir():
            raise KnowledgeBackfeedError("state_unavailable")
        if self._max_state_age_seconds is None or not self._state_path.exists():
            return
        age_seconds = time.time() - self._state_path.stat().st_mtime
        if age_seconds > self._max_state_age_seconds:
            raise KnowledgeBackfeedError("state_stale")

    def _append_event(self, kind: str, payload: Mapping[str, Any]) -> None:
        self._append_events((kind, payload))

    def _append_events(self, *events: tuple[str, Mapping[str, Any]]) -> None:
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            serialized = "".join(
                json.dumps(
                    {"schema_version": SCHEMA_VERSION, "kind": kind, "payload": dict(payload)},
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
                for kind, payload in events
            )
            if len(events) == 1:
                with self._state_path.open("a", encoding="utf-8") as handle:
                    handle.write(serialized)
                return
            existing = self._state_path.read_text(encoding="utf-8") if self._state_path.exists() else ""
            temp_path = self._state_path.with_name(f".{self._state_path.name}.tmp")
            temp_path.write_text(existing + serialized, encoding="utf-8")
            temp_path.replace(self._state_path)
        except OSError as exc:
            raise KnowledgeBackfeedError("state_unavailable") from exc

    @staticmethod
    def _check_proposal_identity(events: Iterable[Mapping[str, Any]], proposal: BackfeedProposal) -> None:
        for event in events:
            if event.get("kind") != "proposal":
                continue
            existing = _proposal_payload(event)
            if existing.get("proposal_id") == proposal.proposal_id and existing != proposal.to_payload():
                raise KnowledgeBackfeedError("duplicate_proposal_id", proposal_id=proposal.proposal_id)
            if existing.get("run_id") == proposal.run_id and existing.get("proposal_id") != proposal.proposal_id:
                raise KnowledgeBackfeedError("duplicate_run_id", proposal_id=proposal.proposal_id)

    @staticmethod
    def _require_proposal(events: Iterable[Mapping[str, Any]], proposal_id: str) -> BackfeedProposal:
        for event in events:
            if event.get("kind") == "proposal":
                payload = _proposal_payload(event)
                if payload.get("proposal_id") == proposal_id:
                    return BackfeedProposal.from_payload(payload)
        raise KnowledgeBackfeedError("proposal_not_found", proposal_id=proposal_id)

    @staticmethod
    def _terminal_status(events: Iterable[Mapping[str, Any]], proposal_id: str) -> BackfeedStatus | None:
        status: BackfeedStatus | None = None
        for event in events:
            if event.get("kind") != "decision":
                continue
            payload = _decision_payload_from_event(event)
            if payload.get("proposal_id") == proposal_id:
                status = BackfeedStatus(payload["decision"])
        return status

    @staticmethod
    def _trusted_record_exists(events: Iterable[Mapping[str, Any]], proposal_id: str) -> bool:
        for event in events:
            if event.get("kind") != "trusted_record":
                continue
            payload = _require_mapping(event, "payload")
            if payload.get("proposal_id") == proposal_id and payload.get("record_id") == _trusted_record_id(
                proposal_id
            ):
                return True
        return False


__all__ = [
    "BackfeedChange",
    "BackfeedProposal",
    "BackfeedScope",
    "BackfeedSource",
    "BackfeedStatus",
    "BackfeedWriteResult",
    "KnowledgeBackfeedError",
    "KnowledgeBackfeedService",
]
