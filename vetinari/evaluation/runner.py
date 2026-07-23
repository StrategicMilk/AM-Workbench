"""Deterministic evaluation execution and versioned per-case evidence.

The current evidence schema persists immutable case rows and derives every
aggregate from those rows.  Historical aggregate-only JSONL records remain
visible for leaderboard history, but they are never eligible as case evidence.
"""

from __future__ import annotations

import json
import logging
import math
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from vetinari.boundary_guards import account_evidence_drop, assert_dependency_success
from vetinari.clock import Clock, utc_now_iso
from vetinari.config.inference_config import get_inference_config
from vetinari.constants import INFERENCE_STATUS_OK
from vetinari.engine.eval_receipts import _private_ledger_transaction, verify_eval_receipt_bindings
from vetinari.evaluation.evidence_types import (
    _DEFAULT_SAMPLE_CASES,
    CURRENT_EVIDENCE_SCHEMA_VERSION,
    LEGACY_DETAILED_SCHEMA_VERSIONS,
    EvalCaseProvenance,
    EvalCaseResult,
    EvalCaseSpec,
    EvalEvidenceOrigin,
    EvalInferenceObservation,
    _case_spec_sha256,
    _invoke_custom_inference,
    _required_float,
    _required_int,
    _required_string,
    _same_float,
    _suite_revision_sha256,
)
from vetinari.evaluation.evidence_types import (
    _validated_suite as _validate_suite_definition,
)
from vetinari.learning.atomic_writers import append_jsonl_atomic

__all__ = [
    "EvalCaseProvenance",
    "EvalCaseResult",
    "EvalCaseSpec",
    "EvalEvidenceOrigin",
    "EvalRunRecord",
    "leaderboard",
    "list_eval_runs",
    "run_eval",
]

logger = logging.getLogger(__name__)

_CURRENT_EVIDENCE_SCHEMA_VERSION = CURRENT_EVIDENCE_SCHEMA_VERSION
_LEGACY_DETAILED_SCHEMA_VERSIONS = LEGACY_DETAILED_SCHEMA_VERSIONS
_HISTORICAL_EVIDENCE_SCHEMA_VERSION = 1
_DEFAULT_EVAL_SLOT = 0
_DEFAULT_SUITE_REVISION_SHA256 = _suite_revision_sha256("default", _DEFAULT_SAMPLE_CASES)
_EVAL_RUNS_DIR_NAME = "eval-runs"
InferenceFn = Callable[..., str]
_validated_suite = _validate_suite_definition


@dataclass(slots=True)
class _EvalStoreIdentities:
    """Store-wide identities that must never appear in two persisted rows."""

    run_ids: set[str]
    request_ids: set[str]
    receipt_ids: set[str]
    attempt_keys: set[str]
    correlations: set[tuple[str, str, str, str, int]]

    @classmethod
    def empty(cls) -> _EvalStoreIdentities:
        return cls(set(), set(), set(), set(), set())

    def conflict(self, record: EvalRunRecord) -> str | None:
        if record.run_id in self.run_ids:
            return f"duplicate run_id {record.run_id!r}"
        for request_id, receipt_id, attempt_key, correlation in _v6_receipt_identities(record):
            if request_id in self.request_ids:
                return f"duplicate AM Engine request_id {request_id!r}"
            if receipt_id in self.receipt_ids:
                return f"duplicate AM Engine receipt_id {receipt_id!r}"
            if attempt_key in self.attempt_keys:
                return f"duplicate AM Engine attempt_key {attempt_key!r}"
            if correlation in self.correlations:
                return "duplicate AM Engine run/suite/case correlation"
        return None

    def add(self, record: EvalRunRecord) -> None:
        self.run_ids.add(record.run_id)
        for request_id, receipt_id, attempt_key, correlation in _v6_receipt_identities(record):
            self.request_ids.add(request_id)
            self.receipt_ids.add(receipt_id)
            self.attempt_keys.add(attempt_key)
            self.correlations.add(correlation)


def _eval_runs_path() -> Path:
    """Resolve the JSONL store lazily relative to the canonical output root."""
    from vetinari.constants import OUTPUTS_DIR

    runs_dir = OUTPUTS_DIR / _EVAL_RUNS_DIR_NAME
    runs_dir.mkdir(parents=True, exist_ok=True)
    return runs_dir / "runs.jsonl"


@dataclass(frozen=True, slots=True)
class EvalRunRecord:
    """One eval run with current row evidence or historical disposition."""

    run_id: str
    model_id: str
    suite_id: str
    started_at: str
    finished_at: str
    score: float | None = None
    error: str | None = None
    case_results: tuple[EvalCaseResult, ...] = ()
    case_provenance: tuple[EvalCaseProvenance, ...] = ()
    evidence_origin: EvalEvidenceOrigin = EvalEvidenceOrigin.CUSTOM
    evidence_schema_version: int | None = None
    total_cases: int | None = None
    passed_cases: int | None = None
    failed_cases: int | None = None

    def __post_init__(self) -> None:
        for key in ("run_id", "model_id", "suite_id", "started_at", "finished_at"):
            value = getattr(self, key)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{key} must be a non-empty string")
        if self.error is not None and (not isinstance(self.error, str) or not self.error.strip()):
            raise ValueError("error must be None or a non-empty string")

        rows = tuple(self.case_results)
        provenance = tuple(self.case_provenance)
        object.__setattr__(self, "case_results", rows)
        object.__setattr__(self, "case_provenance", provenance)
        if not isinstance(self.evidence_origin, EvalEvidenceOrigin):
            raise ValueError("evidence_origin must be a typed EvalEvidenceOrigin")
        if rows:
            if self.error is not None:
                raise ValueError("successful case evidence cannot carry an error")
            self._validate_current_rows(rows)
            version = self.evidence_schema_version or _CURRENT_EVIDENCE_SCHEMA_VERSION
            if version not in {*_LEGACY_DETAILED_SCHEMA_VERSIONS, _CURRENT_EVIDENCE_SCHEMA_VERSION}:
                raise ValueError("case results require a detailed evidence schema")
            if version in _LEGACY_DETAILED_SCHEMA_VERSIONS:
                if provenance:
                    raise ValueError("legacy detailed evidence cannot claim receipt provenance")
                origin = EvalEvidenceOrigin.LEGACY_UNPROVEN
            else:
                origin = self.evidence_origin
                if origin is EvalEvidenceOrigin.AM_ENGINE:
                    self._validate_provenance(rows, provenance)
                    if not self._receipts_match(rows, provenance):
                        raise ValueError("AM Engine receipt provenance does not verify against the pinned trust anchor")
                elif origin in {EvalEvidenceOrigin.ADAPTER_INTEGRITY, EvalEvidenceOrigin.CUSTOM}:
                    if provenance:
                        raise ValueError(f"{origin.value} evidence cannot claim AM Engine provenance")
                else:
                    raise ValueError("current evidence cannot claim legacy-unproven origin")
            total = len(rows)
            passed = sum(row.passed for row in rows)
            failed = total - passed
            score = sum(row.token_f1 for row in rows) / total
            self._verify_derived("score", self.score, score)
            self._verify_derived("total_cases", self.total_cases, total)
            self._verify_derived("passed_cases", self.passed_cases, passed)
            self._verify_derived("failed_cases", self.failed_cases, failed)
        else:
            version = self.evidence_schema_version or _HISTORICAL_EVIDENCE_SCHEMA_VERSION
            if version == _CURRENT_EVIDENCE_SCHEMA_VERSION and self.error is None:
                raise ValueError("current successful evidence requires case_results")
            if version not in {
                _HISTORICAL_EVIDENCE_SCHEMA_VERSION,
                *_LEGACY_DETAILED_SCHEMA_VERSIONS,
                _CURRENT_EVIDENCE_SCHEMA_VERSION,
            }:
                raise ValueError(f"unsupported evidence_schema_version {version!r}")
            score = 0.0 if self.score is None else float(self.score)
            if not math.isfinite(score) or not 0.0 <= score <= 1.0:
                raise ValueError("score must be finite and in [0.0, 1.0]")
            total = passed = failed = 0
            if version == _CURRENT_EVIDENCE_SCHEMA_VERSION:
                self._verify_derived("total_cases", self.total_cases, total)
                self._verify_derived("passed_cases", self.passed_cases, passed)
                self._verify_derived("failed_cases", self.failed_cases, failed)
                origin = self.evidence_origin
            else:
                origin = EvalEvidenceOrigin.LEGACY_UNPROVEN
            if provenance:
                raise ValueError("records without case results cannot carry case provenance")

        object.__setattr__(self, "evidence_schema_version", version)
        object.__setattr__(self, "evidence_origin", origin)
        object.__setattr__(self, "score", score)
        object.__setattr__(self, "total_cases", total)
        object.__setattr__(self, "passed_cases", passed)
        object.__setattr__(self, "failed_cases", failed)

    @staticmethod
    def _verify_derived(name: str, supplied: float | int | None, expected: float | int) -> None:
        if supplied is None:
            return
        if isinstance(supplied, bool) or not isinstance(supplied, (int, float)):
            raise ValueError(f"{name} must be numeric")
        matches = _same_float(float(supplied), float(expected)) if isinstance(expected, float) else supplied == expected
        if not matches:
            raise ValueError(f"{name} does not match derived case evidence")

    def _validate_current_rows(self, rows: tuple[EvalCaseResult, ...]) -> None:
        if self.suite_id != "default":
            raise ValueError(f"current case evidence does not support suite {self.suite_id!r}")
        if len(rows) != len(_DEFAULT_SAMPLE_CASES):
            raise ValueError("current case evidence is missing or has extra rows")
        if len({row.case_id for row in rows}) != len(rows):
            raise ValueError("current case evidence contains duplicate case IDs")
        for row, spec in zip(rows, _DEFAULT_SAMPLE_CASES, strict=True):
            expected_identity = (
                spec.case_id,
                spec.ordinal,
                spec.prompt,
                spec.expected,
                spec.seed,
                spec.threshold,
            )
            actual_identity = (
                row.case_id,
                row.ordinal,
                row.prompt,
                row.expected,
                row.seed,
                row.threshold,
            )
            if actual_identity != expected_identity:
                raise ValueError(f"case evidence does not match stable suite row {spec.case_id}")

    def _validate_provenance(
        self,
        rows: tuple[EvalCaseResult, ...],
        provenance: tuple[EvalCaseProvenance, ...],
    ) -> None:
        if len(provenance) != len(rows):
            raise ValueError("AM Engine evidence requires one provenance row per case")
        if len({row.request_id for row in provenance}) != len(provenance):
            raise ValueError("AM Engine case provenance requires unique request IDs")
        if len({row.trace_id for row in provenance}) != len(provenance):
            raise ValueError("AM Engine case provenance requires unique trace IDs")
        if len({row.receipt_id for row in provenance}) != len(provenance):
            raise ValueError("AM Engine case provenance requires unique receipt IDs")
        if len({row.engine_instance_id for row in provenance}) != 1:
            raise ValueError("AM Engine case provenance must use one verified engine instance")
        if len({row.model_sha256 for row in provenance}) != 1:
            raise ValueError("AM Engine case provenance must use one model artifact digest")
        for result, proof in zip(rows, provenance, strict=True):
            if (proof.case_id, proof.ordinal) != (result.case_id, result.ordinal):
                raise ValueError("case provenance does not align with semantic result order")
            if proof.model_id != self.model_id:
                raise ValueError("case provenance model does not match run envelope model")

    def _receipts_match(
        self,
        rows: tuple[EvalCaseResult, ...],
        provenance: tuple[EvalCaseProvenance, ...],
    ) -> bool:
        bindings = [
            {
                "receipt_id": proof.receipt_id,
                "engine_receipt": proof.engine_receipt,
                "request_id": proof.request_id,
                "trace_id": proof.trace_id,
                "engine_instance_id": proof.engine_instance_id,
                "model_id": proof.model_id,
                "model_sha256": proof.model_sha256,
                "run_id": self.run_id,
                "suite_id": self.suite_id,
                "suite_revision_sha256": _DEFAULT_SUITE_REVISION_SHA256,
                "case_id": result.case_id,
                "ordinal": result.ordinal,
                "seed": result.seed,
                "eval_slot": _DEFAULT_EVAL_SLOT,
                "case_spec_sha256": _case_spec_sha256(_DEFAULT_SAMPLE_CASES[result.ordinal]),
                "messages": ({"role": "user", "content": result.prompt},),
                "output": result.observed,
            }
            for result, proof in zip(rows, provenance, strict=True)
        ]
        return verify_eval_receipt_bindings(bindings)

    @property
    def case_evidence_complete(self) -> bool:
        """Whether this record contains validated current-schema case rows."""
        return (
            self.evidence_schema_version == _CURRENT_EVIDENCE_SCHEMA_VERSION
            and bool(self.case_results)
            and self.error is None
            and self.evidence_origin is EvalEvidenceOrigin.AM_ENGINE
            and self._receipts_match(self.case_results, self.case_provenance)
        )

    def require_case_evidence(self) -> tuple[EvalCaseResult, ...]:
        """Return validated rows or fail closed for historical/failed evidence.

        Returns:
            Complete validated current-schema case rows.

        Raises:
            ValueError: If the record is historical, failed, or incomplete.
        """
        if self.evidence_origin is EvalEvidenceOrigin.ADAPTER_INTEGRITY:
            raise ValueError(
                "blocked_missing_engine_trust_anchor: local adapter-integrity receipts are not maturity evidence"
            )
        if not self.case_evidence_complete:
            raise ValueError(f"eval run {self.run_id} does not contain complete case evidence")
        return self.case_results

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> EvalRunRecord:
        """Decode historical rows or strictly validate current-schema evidence.

        Args:
            raw: Persisted run-record mapping.

        Returns:
            Historical display record or validated current evidence record.

        Raises:
            ValueError: If schema, envelope, rows, or aggregates are invalid.
        """
        version_raw = raw.get("evidence_schema_version", _HISTORICAL_EVIDENCE_SCHEMA_VERSION)
        if isinstance(version_raw, bool) or not isinstance(version_raw, int):
            raise ValueError("evidence_schema_version must be an integer")
        error = raw.get("error")
        if error is not None and not isinstance(error, str):
            raise ValueError("error must be a string or null")
        common = {
            "run_id": _required_string(raw, "run_id"),
            "model_id": _required_string(raw, "model_id"),
            "suite_id": _required_string(raw, "suite_id"),
            "started_at": _required_string(raw, "started_at"),
            "finished_at": _required_string(raw, "finished_at"),
            "error": error,
            "evidence_schema_version": version_raw,
        }
        if version_raw == _HISTORICAL_EVIDENCE_SCHEMA_VERSION:
            if raw.get("case_results"):
                raise ValueError("historical evidence cannot contain case_results")
            return cls(**common, score=_required_float(raw, "score"))
        if version_raw not in {*_LEGACY_DETAILED_SCHEMA_VERSIONS, _CURRENT_EVIDENCE_SCHEMA_VERSION}:
            raise ValueError(f"unsupported evidence_schema_version {version_raw!r}")
        rows_raw = raw.get("case_results")
        if not isinstance(rows_raw, list):
            raise ValueError("current evidence case_results must be a list")
        rows = tuple(
            EvalCaseResult.from_dict(item) if isinstance(item, Mapping) else _raise_invalid_case_row()
            for item in rows_raw
        )
        if version_raw in _LEGACY_DETAILED_SCHEMA_VERSIONS:
            return cls(
                **common,
                score=_required_float(raw, "score"),
                case_results=rows,
                evidence_origin=EvalEvidenceOrigin.LEGACY_UNPROVEN,
                total_cases=_required_int(raw, "total_cases"),
                passed_cases=_required_int(raw, "passed_cases"),
                failed_cases=_required_int(raw, "failed_cases"),
            )
        origin_raw = raw.get("evidence_origin")
        if not isinstance(origin_raw, str):
            raise ValueError("current evidence_origin must be a string")
        try:
            origin = EvalEvidenceOrigin(origin_raw)
        except ValueError as exc:
            raise ValueError(f"unsupported evidence_origin {origin_raw!r}") from exc
        provenance_raw = raw.get("case_provenance")
        if not isinstance(provenance_raw, list):
            raise ValueError("current evidence case_provenance must be a list")
        provenance = tuple(
            EvalCaseProvenance.from_dict(item) if isinstance(item, Mapping) else _raise_invalid_provenance_row()
            for item in provenance_raw
        )
        return cls(
            **common,
            score=_required_float(raw, "score"),
            case_results=rows,
            case_provenance=provenance,
            evidence_origin=origin,
            total_cases=_required_int(raw, "total_cases"),
            passed_cases=_required_int(raw, "passed_cases"),
            failed_cases=_required_int(raw, "failed_cases"),
        )

    def to_dict(self) -> dict[str, Any]:
        """Return the canonical versioned JSONL representation.

        Returns:
            JSON-compatible run record with current rows only when validated.
        """
        row: dict[str, Any] = {
            "run_id": self.run_id,
            "model_id": self.model_id,
            "suite_id": self.suite_id,
            "score": self.score,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "error": self.error,
        }
        if self.evidence_schema_version in {*_LEGACY_DETAILED_SCHEMA_VERSIONS, _CURRENT_EVIDENCE_SCHEMA_VERSION}:
            row.update({
                "evidence_schema_version": self.evidence_schema_version,
                "case_results": [case.to_dict() for case in self.case_results],
                "total_cases": self.total_cases,
                "passed_cases": self.passed_cases,
                "failed_cases": self.failed_cases,
            })
            if self.evidence_schema_version == _CURRENT_EVIDENCE_SCHEMA_VERSION:
                row.update({
                    "evidence_origin": self.evidence_origin.value,
                    "case_provenance": [proof.to_dict() for proof in self.case_provenance],
                })
        return row

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return (
            f"EvalRunRecord(run_id={self.run_id!r}, model_id={self.model_id!r}, "
            f"suite_id={self.suite_id!r}, score={self.score!r})"
        )


def _raise_invalid_case_row() -> EvalCaseResult:
    raise ValueError("case_results entries must be objects")


def _raise_invalid_provenance_row() -> EvalCaseProvenance:
    raise ValueError("case_provenance entries must be objects")


def _v6_receipt_identities(
    record: EvalRunRecord,
) -> tuple[tuple[str, str, str, tuple[str, str, str, str, int]], ...]:
    if (
        record.evidence_schema_version != _CURRENT_EVIDENCE_SCHEMA_VERSION
        or record.evidence_origin is not EvalEvidenceOrigin.AM_ENGINE
    ):
        return ()
    identities: list[tuple[str, str, str, tuple[str, str, str, str, int]]] = []
    for proof in record.case_provenance:
        raw_claims = proof.engine_receipt.get("claims")
        if not isinstance(raw_claims, Mapping):
            raise ValueError("AM Engine receipt provenance has no claims object")
        installation_id = _required_string(raw_claims, "installation_id")
        attempt_key = _required_string(raw_claims, "attempt_key")
        identities.append((
            proof.request_id,
            proof.receipt_id,
            attempt_key,
            (installation_id, record.run_id, record.suite_id, proof.case_id, proof.ordinal),
        ))
    return tuple(identities)


def run_eval(
    model_id: str,
    suite_id: str = "default",
    *,
    clock: Clock | None = None,
    inference_fn: InferenceFn | None = None,
) -> EvalRunRecord:
    """Run the deterministic suite, persist canonical evidence, and return it.

    Args:
        model_id: Non-empty model identifier evaluated by the suite.
        suite_id: Suite identifier; only ``default`` is currently defined.
        clock: Optional deterministic timestamp provider.
        inference_fn: Optional controlled inference callable for tests.

    Returns:
        Persisted current-schema success or failure record.

    Raises:
        ValueError: If the model or suite identifier is invalid.
        RuntimeError: If canonical persistence cannot be confirmed.
    """
    if not model_id or not model_id.strip():
        raise ValueError("model_id must be a non-empty string")
    if suite_id != "default":
        raise ValueError(f"unknown suite_id {suite_id!r}; supported: 'default'")

    started_at = utc_now_iso(clock)
    run_id = str(uuid.uuid4())
    try:
        evaluation = _evaluate_model_against_default_suite(model_id, run_id=run_id, inference_fn=inference_fn)
        finished_at = utc_now_iso(clock)
        record = EvalRunRecord(
            run_id=run_id,
            model_id=model_id,
            suite_id=suite_id,
            started_at=started_at,
            finished_at=finished_at,
            case_results=evaluation.case_results,
            case_provenance=evaluation.case_provenance,
            evidence_origin=evaluation.evidence_origin,
            evidence_schema_version=_CURRENT_EVIDENCE_SCHEMA_VERSION,
        )
    except Exception as exc:
        logger.warning("Eval run %s for model %s failed; recording error: %s", run_id, model_id, exc)
        error_detail = str(exc).strip() or f"{type(exc).__name__}: evaluation failed without detail"
        record = EvalRunRecord(
            run_id=run_id,
            model_id=model_id,
            suite_id=suite_id,
            started_at=started_at,
            finished_at=utc_now_iso(clock),
            score=0.0,
            error=error_detail,
            evidence_origin=(
                EvalEvidenceOrigin.CUSTOM if inference_fn is not None else EvalEvidenceOrigin.ADAPTER_INTEGRITY
            ),
            evidence_schema_version=_CURRENT_EVIDENCE_SCHEMA_VERSION,
            total_cases=0,
            passed_cases=0,
            failed_cases=0,
        )

    failed_steps = [] if _append_run_record(record) else ["persistence"]
    try:
        assert_dependency_success("persistence", failed_steps)
    except RuntimeError as exc:
        logger.error("eval_runner: persistence not confirmed; exiting non-zero")
        raise RuntimeError("eval_runner: persistence not confirmed") from exc
    logger.info(
        "Eval run %s complete: model=%s suite=%s score=%.3f error=%s",
        run_id,
        model_id,
        suite_id,
        record.score,
        record.error,
    )
    return record


def list_eval_runs(model_id: str | None = None, suite_id: str | None = None) -> list[EvalRunRecord]:
    """Read valid historical and current eval runs, newest first.

    Args:
        model_id: Optional exact model filter.
        suite_id: Optional exact suite filter.

    Returns:
        Valid decoded records, excluding corrupt or tampered rows.

    Raises:
        OSError: Never propagated; unreadable state fails closed to no records.
    """
    path = _eval_runs_path()
    if not path.exists():
        return []
    records: list[EvalRunRecord] = []
    identities = _EvalStoreIdentities.empty()
    try:
        with path.open(encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    raw = json.loads(line)
                    if not isinstance(raw, Mapping):
                        raise ValueError("eval run row must be an object")
                    record = EvalRunRecord.from_dict(raw)
                    conflict = identities.conflict(record)
                    if conflict is not None:
                        raise ValueError(f"store-wide evaluation identity conflict: {conflict}")
                    identities.add(record)
                    records.append(record)
                except (json.JSONDecodeError, TypeError, ValueError) as exc:
                    account_evidence_drop(
                        {"path": str(path), "line_number": line_number, "error_type": type(exc).__name__},
                        "eval_runner",
                        logger=logger,
                    )
                    logger.warning("Skipping invalid eval run record in %s at line %s: %s", path, line_number, exc)
    except OSError as exc:
        account_evidence_drop(
            {"path": str(path), "error_type": type(exc).__name__},
            "eval_runner",
            logger=logger,
        )
        logger.warning("Could not read eval run store %s; returning no trusted rows: %s", path, exc)
        return []

    if model_id is not None:
        records = [record for record in records if record.model_id == model_id]
    if suite_id is not None:
        records = [record for record in records if record.suite_id == suite_id]
    records.sort(key=lambda record: record.started_at, reverse=True)
    return records


def leaderboard(suite_id: str = "default", top_n: int = 10) -> list[dict[str, Any]]:
    """Rank successful trusted/history runs without scoring failed attempts.

    Args:
        suite_id: Exact suite identifier to rank.
        top_n: Maximum number of ranked models.

    Returns:
        Ranked dictionaries containing best score and valid run count.

    Raises:
        ValueError: If ``top_n`` is less than one.
    """
    if top_n < 1:
        raise ValueError(f"top_n must be >= 1, got {top_n!r}")
    attempts: dict[str, dict[str, int]] = {}
    best: dict[str, dict[str, Any]] = {}
    for run in list_eval_runs(suite_id=suite_id):
        counts = attempts.setdefault(run.model_id, {"attempt_count": 0, "failure_count": 0, "untrusted_count": 0})
        counts["attempt_count"] += 1
        if run.error is not None:
            counts["failure_count"] += 1
            continue
        rank_eligible = run.case_evidence_complete
        if not rank_eligible:
            counts["untrusted_count"] += 1
            continue
        entry = best.setdefault(
            run.model_id,
            {
                "model_id": run.model_id,
                "best_score": run.score,
                "best_run_id": run.run_id,
                "best_run_started_at": run.started_at,
                "run_count": 0,
            },
        )
        if run.score > entry["best_score"]:
            entry.update({
                "best_score": run.score,
                "best_run_id": run.run_id,
                "best_run_started_at": run.started_at,
            })
        entry["run_count"] += 1
    for model_id, entry in best.items():
        entry.update(attempts[model_id])
    ranked = sorted(best.values(), key=lambda entry: entry["best_score"], reverse=True)
    return [
        {
            "rank": rank,
            "model_id": entry["model_id"],
            "best_score": entry["best_score"],
            "run_count": entry["run_count"],
            "attempt_count": entry["attempt_count"],
            "failure_count": entry["failure_count"],
            "untrusted_count": entry["untrusted_count"],
            "best_run_id": entry["best_run_id"],
            "best_run_started_at": entry["best_run_started_at"],
        }
        for rank, entry in enumerate(ranked[:top_n], start=1)
    ]


@dataclass(frozen=True, slots=True)
class _EvalSuiteEvaluation:
    """Semantic case results and per-attempt trust evidence for one run."""

    case_results: tuple[EvalCaseResult, ...]
    case_provenance: tuple[EvalCaseProvenance, ...]
    evidence_origin: EvalEvidenceOrigin


def _evaluate_model_against_default_suite(
    model_id: str,
    *,
    run_id: str,
    inference_fn: InferenceFn | None = None,
) -> _EvalSuiteEvaluation:
    """Evaluate every default case sequentially in its declared ordinal order."""
    results: list[EvalCaseResult] = []
    provenance: list[EvalCaseProvenance] = []
    origin = EvalEvidenceOrigin.CUSTOM
    for case in _DEFAULT_SAMPLE_CASES:
        if inference_fn is None:
            observation = _infer_with_adapter_manager(model_id, case, run_id, _DEFAULT_EVAL_SLOT)
        else:
            output = _invoke_custom_inference(inference_fn, model_id, case.prompt, _DEFAULT_EVAL_SLOT, case.seed)
            observation = EvalInferenceObservation(output=output, origin=EvalEvidenceOrigin.CUSTOM)
        if results and observation.origin is not origin:
            raise RuntimeError("evaluation cases returned mixed evidence origins")
        origin = observation.origin
        results.append(EvalCaseResult.from_observation(case, observation.output))
        if observation.origin is EvalEvidenceOrigin.AM_ENGINE:
            provenance.append(
                EvalCaseProvenance(
                    case_id=case.case_id,
                    ordinal=case.ordinal,
                    request_id=observation.engine_request_id or "",
                    trace_id=observation.engine_trace_id or "",
                    engine_instance_id=observation.engine_instance_id or "",
                    model_id=observation.engine_model_id or "",
                    model_sha256=observation.engine_model_sha256 or "",
                    receipt_id=observation.engine_receipt_id or "",
                    engine_receipt=observation.engine_receipt or {},
                )
            )
    return _EvalSuiteEvaluation(tuple(results), tuple(provenance), origin)


def _score_model_against_default_suite(
    model_id: str,
    *,
    inference_fn: InferenceFn | None = None,
) -> tuple[EvalCaseResult, ...]:
    """Compatibility helper returning deterministic semantic results only."""
    return _evaluate_model_against_default_suite(
        model_id,
        run_id=f"unpersisted-{uuid.uuid4()}",
        inference_fn=inference_fn,
    ).case_results


def _infer_with_adapter_manager(
    model_id: str,
    case: EvalCaseSpec,
    run_id: str,
    eval_slot: int,
) -> EvalInferenceObservation:
    """Run one case directly through the registered AM Engine EVAL path."""
    from vetinari.adapter_manager import get_adapter_manager
    from vetinari.adapters.base import InferenceRequest
    from vetinari.types import PriorityClass

    profile = get_inference_config().get_profile("eval")
    response = get_adapter_manager().infer(
        InferenceRequest(
            model_id=model_id,
            prompt=case.prompt,
            max_tokens=profile.max_tokens,
            temperature=profile.temperature,
            task_type="eval",
            priority_class=PriorityClass.EVAL,
            eval_slot=eval_slot,
            seed=case.seed,
            metadata={
                "eval_context": {
                    "schema_version": 1,
                    "run_id": run_id,
                    "suite_id": "default",
                    "suite_revision_sha256": _DEFAULT_SUITE_REVISION_SHA256,
                    "case_id": case.case_id,
                    "ordinal": case.ordinal,
                    "case_spec_sha256": _case_spec_sha256(case),
                },
            },
        ),
        provider_name="am_engine",
        fallback_on_error=False,
        use_cascade=False,
    )
    if response.status != INFERENCE_STATUS_OK:
        raise RuntimeError(response.error or f"inference failed for {model_id}")
    if not response.output.strip():
        raise RuntimeError(f"inference returned empty output for {model_id}")
    metadata = response.metadata if isinstance(response.metadata, Mapping) else {}
    request_id = metadata.get("request_id")
    if not isinstance(request_id, str) or not request_id.strip():
        raise RuntimeError("AM Engine inference response omitted request_id provenance")
    trace_id = metadata.get("trace_id")
    if not isinstance(trace_id, str) or not trace_id.strip():
        raise RuntimeError("AM Engine inference response omitted trace_id provenance")
    engine_instance_id = metadata.get("engine_instance_id")
    if not isinstance(engine_instance_id, str) or not engine_instance_id.strip():
        raise RuntimeError("AM Engine inference response omitted verified engine_instance_id provenance")
    engine_model_id = metadata.get("engine_model_id")
    if not isinstance(engine_model_id, str) or not engine_model_id.strip():
        raise RuntimeError("AM Engine inference response omitted model_id provenance")
    if response.model_id != model_id or engine_model_id != model_id:
        raise RuntimeError("AM Engine inference response model does not match run envelope model")
    engine_model_sha256 = metadata.get("engine_model_sha256")
    if not isinstance(engine_model_sha256, str) or len(engine_model_sha256) != 64:
        raise RuntimeError("AM Engine inference response omitted model artifact digest provenance")
    receipt_id = metadata.get("eval_receipt_id")
    if not isinstance(receipt_id, str) or not receipt_id.strip():
        raise RuntimeError("AM Engine inference response omitted durable receipt provenance")
    raw_receipt = metadata.get("engine_receipt")
    if not isinstance(raw_receipt, Mapping) or not raw_receipt:
        raise RuntimeError("AM Engine inference response omitted complete receipt provenance")
    if metadata.get("eval_evidence_origin") != EvalEvidenceOrigin.AM_ENGINE.value:
        raise RuntimeError("AM Engine inference response receipt was not classified by the pinned-anchor verifier")
    return EvalInferenceObservation(
        output=response.output,
        origin=EvalEvidenceOrigin.AM_ENGINE,
        engine_request_id=request_id,
        engine_trace_id=trace_id,
        engine_instance_id=engine_instance_id,
        engine_model_id=engine_model_id,
        engine_model_sha256=engine_model_sha256,
        engine_receipt_id=receipt_id,
        engine_receipt=raw_receipt,
    )


def _append_run_record(record: EvalRunRecord) -> bool:
    """Atomically append one revalidated canonical record, idempotently by content."""
    path = _eval_runs_path()
    try:
        canonical_record = EvalRunRecord.from_dict(record.to_dict())
        canonical_payload = canonical_record.to_dict()
        with _private_ledger_transaction(path):
            existing_lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
            identities = _EvalStoreIdentities.empty()
            identical_run_exists = False
            for existing_line in existing_lines:
                if not existing_line.strip():
                    continue
                try:
                    existing_row = json.loads(existing_line)
                except json.JSONDecodeError as exc:
                    logger.error("Refusing append because eval ledger contains malformed JSON: %s", exc)
                    return False
                if not isinstance(existing_row, Mapping):
                    logger.error("Refusing append because eval ledger contains a non-object row")
                    return False
                try:
                    existing_record = EvalRunRecord.from_dict(existing_row)
                    existing_canonical = existing_record.to_dict()
                except (TypeError, ValueError) as exc:
                    logger.error("Refusing append because eval ledger contains invalid evidence: %s", exc)
                    return False
                conflict = identities.conflict(existing_record)
                if conflict is not None:
                    logger.error("Refusing append because eval ledger contains %s", conflict)
                    return False
                identities.add(existing_record)
                if existing_row.get("run_id") == record.run_id:
                    if existing_canonical != canonical_payload:
                        logger.error("Eval run %s conflicts with a different persisted payload", record.run_id)
                        return False
                    identical_run_exists = True
            if identical_run_exists:
                logger.debug("Eval run %s already persisted with identical payload; skipping", record.run_id)
                return True
            conflict = identities.conflict(canonical_record)
            if conflict is not None:
                logger.error("Refusing append because new eval row has %s", conflict)
                return False
            append_jsonl_atomic(path, canonical_payload)
        return True
    except (OSError, TypeError, ValueError) as exc:
        account_evidence_drop(
            {"run_id": record.run_id, "path": str(path), "error_type": type(exc).__name__},
            "eval_runner",
            logger=logger,
        )
        logger.warning("Could not persist eval run %s to %s: %s", record.run_id, path, exc)
        return False
