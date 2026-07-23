"""Core-loop eval-case contract.

Reason vocabulary:
- ``empty-case-id``, ``bad-case-id-prefix``, ``bad-case-id-hex``
- ``bad-schema-version``, ``empty-feed-targets``, ``unknown-feed-target``
- ``provenance-event-mismatch``
- ``empty-command-kind``, ``unknown-command-kind``, ``empty-argv``
- ``empty-working-directory``, ``empty-source-event-id``, ``bad-captured-at``
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Final

from vetinari.workbench.evals import EvalKind

_CASE_ID_PREFIX: Final[str] = "eval-case-"
_SCHEMA_VERSION: Final[int] = 1
_CASE_ID_RE: Final[re.Pattern[str]] = re.compile(r"^eval-case-[0-9a-f]{32}$")
_VALID_REPLAY_COMMAND_KINDS: Final[frozenset[str]] = frozenset({
    "pytest",
    "cli",
    "playground_run",
    "route_simulation",
    "failure_replay",
})
_FEED_TARGETS: Final[frozenset[str]] = frozenset({
    "model_routing",
    "prompt_promotion",
    "redteam_fixtures",
    "benchmark_import",
    "failure_intelligence",
    "automation_approval_gates",
})

logger = logging.getLogger(__name__)


class EvalCaseRecordError(Exception):
    """Raised when a core-loop eval-case record is malformed."""

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.reason = reason

    def __str__(self) -> str:
        return f"{self.args[0]} (reason={self.reason})"


class ReplayCommandError(Exception):
    """Raised when a replay command cannot be trusted."""

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.reason = reason

    def __str__(self) -> str:
        return f"{self.args[0]} (reason={self.reason})"


class CoreLoopEventKind(str, Enum):
    """Intermediate trace/event sources promoted into regression evidence."""

    FAILED_TRACE = "failed_trace"
    USER_CORRECTION = "user_correction"
    WATCHER_HALT = "watcher_halt"
    ROUTE_DECISION = "route_decision"
    MEMORY_RECALL = "memory_recall"
    TOOL_ERROR = "tool_error"
    INSPECTOR_FINDING = "inspector_finding"


@dataclass(frozen=True, slots=True)
class ReplayCommand:
    """Serializable replay handle. It never executes a command."""

    command_kind: str
    argv: tuple[str, ...]
    working_directory: str
    env_overrides: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if not self.command_kind.strip():
            raise ReplayCommandError("command_kind must be non-empty", reason="empty-command-kind")
        if self.command_kind not in _VALID_REPLAY_COMMAND_KINDS:
            raise ReplayCommandError("command_kind is not supported", reason="unknown-command-kind")
        if not isinstance(self.argv, tuple) or not self.argv:
            raise ReplayCommandError("argv must be a non-empty tuple", reason="empty-argv")
        if any(not isinstance(value, str) or not value.strip() for value in self.argv):
            raise ReplayCommandError("argv must contain non-empty strings", reason="empty-argv")
        if not self.working_directory.strip():
            raise ReplayCommandError("working_directory must be non-empty", reason="empty-working-directory")
        for key, value in self.env_overrides:
            if not isinstance(key, str) or not key.strip() or not isinstance(value, str):
                raise ReplayCommandError("env_overrides must contain string pairs", reason="bad-env-overrides")

    def to_dict(self) -> dict[str, Any]:
        return {
            "command_kind": self.command_kind,
            "argv": list(self.argv),
            "working_directory": self.working_directory,
            "env_overrides": [[key, value] for key, value in self.env_overrides],
        }

    @classmethod
    def from_mapping(cls, payload: dict[str, Any]) -> ReplayCommand:
        return cls(
            command_kind=str(payload["command_kind"]),
            argv=tuple(str(value) for value in payload["argv"]),
            working_directory=str(payload["working_directory"]),
            env_overrides=tuple((str(key), str(value)) for key, value in payload.get("env_overrides", ())),
        )

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"ReplayCommand(command_kind={self.command_kind!r}, argv={self.argv!r}, working_directory={self.working_directory!r})"


@dataclass(frozen=True, slots=True)
class EvalCaseProvenance:
    """Where a core-loop eval case came from."""

    source_event_kind: CoreLoopEventKind
    source_event_id: str
    source_run_id: str | None
    source_asset_id: str | None
    source_asset_revision: str | None
    captured_at_utc: str

    def __post_init__(self) -> None:
        if not isinstance(self.source_event_kind, CoreLoopEventKind):
            raise EvalCaseRecordError("source_event_kind must be CoreLoopEventKind", reason="provenance-event-mismatch")
        if not self.source_event_id.strip():
            raise EvalCaseRecordError("source_event_id must be non-empty", reason="empty-source-event-id")
        if not _is_utc_iso(self.captured_at_utc):
            raise EvalCaseRecordError("captured_at_utc must be UTC ISO8601", reason="bad-captured-at")

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_event_kind": self.source_event_kind.value,
            "source_event_id": self.source_event_id,
            "source_run_id": self.source_run_id,
            "source_asset_id": self.source_asset_id,
            "source_asset_revision": self.source_asset_revision,
            "captured_at_utc": self.captured_at_utc,
        }

    @classmethod
    def from_mapping(cls, payload: dict[str, Any]) -> EvalCaseProvenance:
        return cls(
            source_event_kind=CoreLoopEventKind(str(payload["source_event_kind"])),
            source_event_id=str(payload["source_event_id"]),
            source_run_id=payload.get("source_run_id"),
            source_asset_id=payload.get("source_asset_id"),
            source_asset_revision=payload.get("source_asset_revision"),
            captured_at_utc=str(payload["captured_at_utc"]),
        )

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"EvalCaseProvenance(source_event_kind={self.source_event_kind!r}, source_event_id={self.source_event_id!r}, source_run_id={self.source_run_id!r})"


@dataclass(frozen=True, slots=True)
class EvalCaseRecord:
    """One persisted core-loop eval case."""

    case_id: str
    provenance: EvalCaseProvenance
    replay_command: ReplayCommand
    kind: EvalKind
    consumer_feed_targets: tuple[str, ...]
    schema_version: int = _SCHEMA_VERSION
    eval_result_ref: str | None = None
    redteam_fixture_ref: str | None = None
    benchmark_import_ref: str | None = None
    approval_gate_ref: str | None = None
    failure_intelligence_autopsy_ref: str | None = None
    route_decision_ref: str | None = None

    def __post_init__(self) -> None:
        if not self.case_id.strip():
            raise EvalCaseRecordError("case_id must be non-empty", reason="empty-case-id")
        if not self.case_id.startswith(_CASE_ID_PREFIX):
            raise EvalCaseRecordError("case_id must use eval-case prefix", reason="bad-case-id-prefix")
        if _CASE_ID_RE.fullmatch(self.case_id) is None:
            raise EvalCaseRecordError("case_id must contain 16 bytes of lowercase hex", reason="bad-case-id-hex")
        if self.schema_version != _SCHEMA_VERSION:
            raise EvalCaseRecordError("schema_version must be 1", reason="bad-schema-version")
        if not isinstance(self.provenance, EvalCaseProvenance):
            raise EvalCaseRecordError("provenance must be EvalCaseProvenance", reason="provenance-event-mismatch")
        if not isinstance(self.replay_command, ReplayCommand):
            raise EvalCaseRecordError("replay_command must be ReplayCommand", reason="provenance-event-mismatch")
        if not isinstance(self.kind, EvalKind):
            raise EvalCaseRecordError("kind must be EvalKind", reason="provenance-event-mismatch")
        if not isinstance(self.consumer_feed_targets, tuple) or not self.consumer_feed_targets:
            raise EvalCaseRecordError("consumer_feed_targets must be non-empty", reason="empty-feed-targets")
        if unknown := [target for target in self.consumer_feed_targets if target not in _FEED_TARGETS]:
            raise EvalCaseRecordError(f"unknown consumer feed target: {unknown[0]}", reason="unknown-feed-target")
        self._validate_event_consistency()

    def _validate_event_consistency(self) -> None:
        event = self.provenance.source_event_kind
        if event is CoreLoopEventKind.FAILED_TRACE and (not self.provenance.source_run_id or not self.eval_result_ref):
            raise EvalCaseRecordError("failed traces require run and eval refs", reason="provenance-event-mismatch")
        if event is CoreLoopEventKind.ROUTE_DECISION and not self.route_decision_ref:
            raise EvalCaseRecordError(
                "route decisions require a route_decision_ref", reason="provenance-event-mismatch"
            )
        if event is CoreLoopEventKind.INSPECTOR_FINDING and not self.failure_intelligence_autopsy_ref:
            raise EvalCaseRecordError("inspector findings require an autopsy ref", reason="provenance-event-mismatch")
        run_backed = {
            CoreLoopEventKind.USER_CORRECTION,
            CoreLoopEventKind.WATCHER_HALT,
            CoreLoopEventKind.MEMORY_RECALL,
            CoreLoopEventKind.TOOL_ERROR,
        }
        if event in run_backed and not self.provenance.source_run_id:
            raise EvalCaseRecordError("event kind requires a source run", reason="provenance-event-mismatch")

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "schema_version": self.schema_version,
            "provenance": self.provenance.to_dict(),
            "replay_command": self.replay_command.to_dict(),
            "kind": self.kind.value,
            "eval_result_ref": self.eval_result_ref,
            "redteam_fixture_ref": self.redteam_fixture_ref,
            "benchmark_import_ref": self.benchmark_import_ref,
            "approval_gate_ref": self.approval_gate_ref,
            "failure_intelligence_autopsy_ref": self.failure_intelligence_autopsy_ref,
            "route_decision_ref": self.route_decision_ref,
            "consumer_feed_targets": list(self.consumer_feed_targets),
        }

    @classmethod
    def from_mapping(cls, payload: dict[str, Any]) -> EvalCaseRecord:
        return cls(
            case_id=str(payload["case_id"]),
            schema_version=int(payload["schema_version"]),
            provenance=EvalCaseProvenance.from_mapping(dict(payload["provenance"])),
            replay_command=ReplayCommand.from_mapping(dict(payload["replay_command"])),
            kind=EvalKind(str(payload["kind"])),
            eval_result_ref=payload.get("eval_result_ref"),
            redteam_fixture_ref=payload.get("redteam_fixture_ref"),
            benchmark_import_ref=payload.get("benchmark_import_ref"),
            approval_gate_ref=payload.get("approval_gate_ref"),
            failure_intelligence_autopsy_ref=payload.get("failure_intelligence_autopsy_ref"),
            route_decision_ref=payload.get("route_decision_ref"),
            consumer_feed_targets=tuple(str(value) for value in payload["consumer_feed_targets"]),
        )

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"EvalCaseRecord(case_id={self.case_id!r}, provenance={self.provenance!r}, replay_command={self.replay_command!r})"


def _is_utc_iso(value: str) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = f"{normalized[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        logger.warning(
            "Rejected malformed UTC timestamp for eval-case contract.",
            extra={"value": value[:80]},
            exc_info=True,
        )
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() == timezone.utc.utcoffset(parsed)


__all__ = [
    "_CASE_ID_PREFIX",
    "_FEED_TARGETS",
    "_SCHEMA_VERSION",
    "_VALID_REPLAY_COMMAND_KINDS",
    "CoreLoopEventKind",
    "EvalCaseProvenance",
    "EvalCaseRecord",
    "EvalCaseRecordError",
    "ReplayCommand",
    "ReplayCommandError",
]
