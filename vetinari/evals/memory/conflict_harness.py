"""Deterministic Workbench memory recall and conflict eval harness.

The harness is intentionally storage-free. It evaluates explicit fixture
payloads through the active recall contract and emits Workbench follow-ups for
every failed expectation or detected conflict.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from vetinari.memory.recall_contract import (
    MemoryRecallPack,
    RecallProfile,
    RecallStatus,
    build_recall_pack,
    build_unavailable_pack,
)


class MemoryEvalError(ValueError):
    """Raised when a memory eval fixture is malformed."""


@dataclass(frozen=True, slots=True)
class WorkbenchFollowUp:
    """Automation-visible follow-up emitted for failed memory eval behavior."""

    follow_up_id: str
    case_id: str
    severity: str
    reason: str
    details: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe follow-up payload."""
        return {
            "follow_up_id": self.follow_up_id,
            "case_id": self.case_id,
            "severity": self.severity,
            "reason": self.reason,
            "details": list(self.details),
        }

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"WorkbenchFollowUp(follow_up_id={self.follow_up_id!r}, case_id={self.case_id!r}, severity={self.severity!r})"


@dataclass(frozen=True, slots=True)
class MemoryConflictFinding:
    """One conflict between an active memory and a stronger source."""

    memory_id: str
    conflict_type: str
    source_id: str
    source_type: str
    claim_key: str
    memory_value: str
    source_value: str
    severity: str = "high"

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe conflict payload."""
        return {
            "memory_id": self.memory_id,
            "conflict_type": self.conflict_type,
            "source_id": self.source_id,
            "source_type": self.source_type,
            "claim_key": self.claim_key,
            "memory_value": self.memory_value,
            "source_value": self.source_value,
            "severity": self.severity,
        }

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"MemoryConflictFinding(memory_id={self.memory_id!r}, conflict_type={self.conflict_type!r}, source_id={self.source_id!r})"


@dataclass(frozen=True, slots=True)
class MemoryEvalCase:
    """One recall/conflict regression case."""

    case_id: str
    query: str
    profile: RecallProfile
    raw_memories: tuple[Mapping[str, Any], ...]
    expected_memory_ids: tuple[str, ...] = ()
    forbidden_memory_ids: tuple[str, ...] = ()
    expected_status: RecallStatus | None = None
    expected_diagnostics: tuple[str, ...] = ()
    evidence_sources: tuple[Mapping[str, Any], ...] = ()
    newer_memories: tuple[Mapping[str, Any], ...] = ()
    public_export: bool = False
    embedding_available: bool = True
    degraded_fallback_memories: tuple[Mapping[str, Any], ...] = ()
    allow_degraded_embedding_fallback: bool = False
    should_pass: bool | None = None

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> MemoryEvalCase:
        """Parse a case fixture.

        Returns:
            MemoryEvalCase value produced by from_mapping().
        """
        profile_payload = _require_mapping(payload, "profile")
        expected_status = payload.get("expected_status")
        return cls(
            case_id=_require_text(payload, "case_id"),
            query=_require_text(payload, "query"),
            profile=RecallProfile.from_mapping(str(profile_payload.get("profile_id", "fixture")), profile_payload),
            raw_memories=_tuple_of_mappings(payload.get("raw_memories", ()), "raw_memories"),
            expected_memory_ids=_tuple_of_text(payload.get("expected_memory_ids", ()), "expected_memory_ids"),
            forbidden_memory_ids=_tuple_of_text(payload.get("forbidden_memory_ids", ()), "forbidden_memory_ids"),
            expected_status=RecallStatus(str(expected_status)) if expected_status is not None else None,
            expected_diagnostics=_tuple_of_text(payload.get("expected_diagnostics", ()), "expected_diagnostics"),
            evidence_sources=_tuple_of_mappings(payload.get("evidence_sources", ()), "evidence_sources"),
            newer_memories=_tuple_of_mappings(payload.get("newer_memories", ()), "newer_memories"),
            public_export=bool(payload.get("public_export", False)),
            embedding_available=bool(payload.get("embedding_available", True)),
            degraded_fallback_memories=_tuple_of_mappings(
                payload.get("degraded_fallback_memories", ()),
                "degraded_fallback_memories",
            ),
            allow_degraded_embedding_fallback=bool(payload.get("allow_degraded_embedding_fallback", False)),
            should_pass=_optional_bool(payload.get("should_pass")),
        )

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"MemoryEvalCase(case_id={self.case_id!r}, query={self.query!r}, profile={self.profile!r})"


@dataclass(frozen=True, slots=True)
class MemoryEvalResult:
    """Result for one memory regression case."""

    case_id: str
    passed: bool
    pack: MemoryRecallPack
    selected_memory_ids: tuple[str, ...]
    blocked_memory_ids: tuple[str, ...]
    expectation_failures: tuple[str, ...]
    conflicts: tuple[MemoryConflictFinding, ...]
    follow_ups: tuple[WorkbenchFollowUp, ...]
    degraded_fallback_used: bool = False
    expected_pass: bool | None = None

    @property
    def expectation_matched(self) -> bool:
        """Return whether this case matched an explicit per-case expectation."""
        return self.passed if self.expected_pass is None else self.passed is self.expected_pass

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe result payload."""
        return {
            "case_id": self.case_id,
            "passed": self.passed,
            "status": self.pack.status.value,
            "selected_memory_ids": list(self.selected_memory_ids),
            "blocked_memory_ids": list(self.blocked_memory_ids),
            "diagnostics": list(self.pack.diagnostics),
            "expectation_failures": list(self.expectation_failures),
            "expected_pass": self.expected_pass,
            "expectation_matched": self.expectation_matched,
            "conflicts": [conflict.to_dict() for conflict in self.conflicts],
            "follow_ups": [follow_up.to_dict() for follow_up in self.follow_ups],
            "degraded_fallback_used": self.degraded_fallback_used,
        }

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"MemoryEvalResult(case_id={self.case_id!r}, passed={self.passed!r}, pack={self.pack!r})"


@dataclass(frozen=True, slots=True)
class MemoryEvalSuiteResult:
    """Result for a fixture file or in-memory suite."""

    suite_id: str
    passed: bool
    expected_result: str
    case_results: tuple[MemoryEvalResult, ...]
    fixture_path: str = ""

    @property
    def actual_result(self) -> str:
        """Return pass/fail from case outcomes."""
        return "pass" if all(result.expectation_matched for result in self.case_results) else "fail"

    @property
    def expectation_matched(self) -> bool:
        """Return whether actual result matches the fixture expectation."""
        return self.actual_result == self.expected_result

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe suite result."""
        return {
            "suite_id": self.suite_id,
            "fixture_path": self.fixture_path,
            "passed": self.passed,
            "actual_result": self.actual_result,
            "expected_result": self.expected_result,
            "expectation_matched": self.expectation_matched,
            "case_results": [result.to_dict() for result in self.case_results],
        }

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"MemoryEvalSuiteResult(suite_id={self.suite_id!r}, passed={self.passed!r}, expected_result={self.expected_result!r})"


class MemoryConflictEvalHarness:
    """Run memory recall cases and detect conflicts in active selected items."""

    def run_case(self, case: MemoryEvalCase) -> MemoryEvalResult:
        """Run one case through the active recall contract.

        Returns:
            Outcome produced by run_case().
        """
        pack, degraded_used = self._build_pack(case)
        selected = tuple(item.memory_id for item in pack.eligible_items)
        blocked = tuple(item.memory_id for item in pack.items if not item.prompt_eligible)

        failures = list(self._expectation_failures(case, pack=pack, selected=selected, blocked=blocked))
        conflicts = self._detect_conflicts(case, pack)
        failures.extend(self._conflict_failures(conflicts))

        follow_ups = tuple(
            WorkbenchFollowUp(
                follow_up_id=f"{case.case_id}:{index}",
                case_id=case.case_id,
                severity="high" if failure.startswith("conflict:") else "medium",
                reason=failure,
                details=(f"status={pack.status.value}", *pack.diagnostics),
            )
            for index, failure in enumerate(failures, start=1)
        )
        return MemoryEvalResult(
            case_id=case.case_id,
            passed=not failures,
            pack=pack,
            selected_memory_ids=selected,
            blocked_memory_ids=blocked,
            expectation_failures=tuple(failures),
            conflicts=conflicts,
            follow_ups=follow_ups,
            degraded_fallback_used=degraded_used,
            expected_pass=case.should_pass,
        )

    @staticmethod
    def _build_pack(case: MemoryEvalCase) -> tuple[MemoryRecallPack, bool]:
        if case.embedding_available:
            return (
                build_recall_pack(
                    raw_memories=case.raw_memories,
                    agent_type=case.profile.agent_types[0],
                    task_type="memory_conflict_eval",
                    profile=case.profile,
                    query=case.query,
                ),
                False,
            )
        if case.allow_degraded_embedding_fallback and case.degraded_fallback_memories:
            return (
                build_recall_pack(
                    raw_memories=case.degraded_fallback_memories,
                    agent_type=case.profile.agent_types[0],
                    task_type="memory_conflict_eval_degraded_embedding_fallback",
                    profile=case.profile,
                    query=case.query,
                ),
                True,
            )
        return (
            build_unavailable_pack(
                agent_type=case.profile.agent_types[0],
                task_type="memory_conflict_eval",
                profile=case.profile,
                query=case.query,
                reason="embedding_unavailable",
            ),
            False,
        )

    @staticmethod
    def _expectation_failures(
        case: MemoryEvalCase,
        *,
        pack: MemoryRecallPack,
        selected: tuple[str, ...],
        blocked: tuple[str, ...],
    ) -> tuple[str, ...]:
        failures: list[str] = []
        if case.expected_status is not None and pack.status is not case.expected_status:
            failures.append(f"status:{pack.status.value}!={case.expected_status.value}")
        missing = sorted(set(case.expected_memory_ids) - set(selected))
        if missing:
            failures.append(f"missing_expected:{','.join(missing)}")
        forbidden_selected = sorted(set(case.forbidden_memory_ids) & set(selected))
        if forbidden_selected:
            failures.append(f"forbidden_selected:{','.join(forbidden_selected)}")
        missing_blocked = sorted(set(case.forbidden_memory_ids) - set(blocked) - set(selected))
        if missing_blocked:
            failures.append(f"forbidden_not_seen:{','.join(missing_blocked)}")
        missing_diagnostics = sorted(set(case.expected_diagnostics) - set(pack.diagnostics))
        if missing_diagnostics:
            failures.append(f"missing_diagnostics:{','.join(missing_diagnostics)}")
        if case.public_export:
            private_ids = _selected_boundary_ids(pack, forbidden_boundaries={"private", "mixed", "unknown"})
            if private_ids:
                failures.append(f"public_private_boundary:{','.join(private_ids)}")
        return tuple(failures)

    @staticmethod
    def _detect_conflicts(
        case: MemoryEvalCase,
        pack: MemoryRecallPack,
    ) -> tuple[MemoryConflictFinding, ...]:
        findings: list[MemoryConflictFinding] = []
        selected_items = pack.eligible_items
        for item in selected_items:
            claims = _claims_from_mapping(item.metadata)
            for source in case.evidence_sources:
                findings.extend(_compare_claims(item.memory_id, claims, source, "evidence_conflict"))
            for newer in case.newer_memories:
                newer_source = {
                    "source_id": newer.get("memory_id", newer.get("id", "newer-memory")),
                    "source_type": "newer_memory",
                    "assertions": _claims_from_mapping(_memory_metadata(newer)),
                }
                findings.extend(_compare_claims(item.memory_id, claims, newer_source, "newer_memory_conflict"))
        return tuple(findings)

    @staticmethod
    def _conflict_failures(conflicts: Sequence[MemoryConflictFinding]) -> tuple[str, ...]:
        return tuple(
            f"conflict:{conflict.conflict_type}:{conflict.memory_id}:{conflict.source_id}:{conflict.claim_key}"
            for conflict in conflicts
        )


def load_eval_suite(path: str | Path) -> tuple[str, str, tuple[MemoryEvalCase, ...]]:
    """Load a JSON eval suite from disk.

    Returns:
        Resolved eval suite value.

    Raises:
        Exception: Propagates validation or runtime failures from the underlying operation.
    """
    fixture_path = Path(path)
    try:
        payload = json.loads(fixture_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MemoryEvalError(f"unable to load fixture {fixture_path}: {exc}") from exc
    suite_id = _require_text(payload, "suite_id")
    expected_result = str(payload.get("expected_result", "pass"))
    if expected_result not in {"pass", "fail"}:
        raise MemoryEvalError(f"{fixture_path}: expected_result must be pass or fail")
    cases = tuple(MemoryEvalCase.from_mapping(case) for case in payload.get("cases", ()))
    if not cases:
        raise MemoryEvalError(f"{fixture_path}: cases must contain at least one case")
    return suite_id, expected_result, cases


def run_eval_suite(path: str | Path, harness: MemoryConflictEvalHarness | None = None) -> MemoryEvalSuiteResult:
    """Run a fixture suite and compare actual outcome with expected_result.

    Args:
        path: Filesystem path read or written by the operation.
        harness: Harness value consumed by run_eval_suite().

    Returns:
        Outcome produced by run_eval_suite().
    """
    harness = harness or MemoryConflictEvalHarness()
    suite_id, expected_result, cases = load_eval_suite(path)
    case_results = tuple(harness.run_case(case) for case in cases)
    actual_result = "pass" if all(result.expectation_matched for result in case_results) else "fail"
    return MemoryEvalSuiteResult(
        suite_id=suite_id,
        fixture_path=str(path),
        passed=actual_result == expected_result,
        expected_result=expected_result,
        case_results=case_results,
    )


def _compare_claims(
    memory_id: str,
    memory_claims: Mapping[str, Any],
    source: Mapping[str, Any],
    conflict_type: str,
) -> tuple[MemoryConflictFinding, ...]:
    source_claims = _claims_from_mapping(source.get("assertions", {}))
    source_id = str(source.get("source_id", "source"))
    source_type = str(source.get("source_type", "unknown"))
    findings: list[MemoryConflictFinding] = []
    for key, memory_value in memory_claims.items():
        if key in source_claims and str(memory_value) != str(source_claims[key]):
            findings.append(
                MemoryConflictFinding(
                    memory_id=memory_id,
                    conflict_type=conflict_type,
                    source_id=source_id,
                    source_type=source_type,
                    claim_key=str(key),
                    memory_value=str(memory_value),
                    source_value=str(source_claims[key]),
                )
            )
    return tuple(findings)


def _selected_boundary_ids(pack: MemoryRecallPack, *, forbidden_boundaries: set[str]) -> tuple[str, ...]:
    ids: list[str] = []
    for item in pack.eligible_items:
        boundary = str(item.metadata.get("boundary", "unknown"))
        if boundary in forbidden_boundaries:
            ids.append(item.memory_id)
    return tuple(ids)


def _claims_from_mapping(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping):
        return {}
    claims = value.get("claims", value)
    if not isinstance(claims, Mapping):
        return {}
    return {str(key): str(item) for key, item in claims.items()}


def _memory_metadata(value: Mapping[str, Any]) -> Mapping[str, Any]:
    metadata = value.get("metadata", {})
    return metadata if isinstance(metadata, Mapping) else {}


def _require_text(payload: Mapping[str, Any], field_name: str) -> str:
    value = payload.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise MemoryEvalError(f"{field_name} must be a non-empty string")
    return value


def _require_mapping(payload: Mapping[str, Any], field_name: str) -> Mapping[str, Any]:
    value = payload.get(field_name)
    if not isinstance(value, Mapping):
        raise MemoryEvalError(f"{field_name} must be a mapping")
    return value


def _tuple_of_text(value: Any, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, Iterable) or isinstance(value, (str, bytes)):
        raise MemoryEvalError(f"{field_name} must be a list")
    return tuple(str(item) for item in value)


def _tuple_of_mappings(value: Any, field_name: str) -> tuple[Mapping[str, Any], ...]:
    if not isinstance(value, Iterable) or isinstance(value, (str, bytes)):
        raise MemoryEvalError(f"{field_name} must be a list")
    rows: list[Mapping[str, Any]] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise MemoryEvalError(f"{field_name} entries must be mappings")
        rows.append(item)
    return tuple(rows)


def _optional_bool(value: Any) -> bool | None:
    if value is None:
        return None
    return bool(value)
