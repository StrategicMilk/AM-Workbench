"""Typed deterministic-evaluation suite and inference provenance contracts."""

from __future__ import annotations

import hashlib
import inspect
import json
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

InferenceFn = Callable[..., str]
CURRENT_EVIDENCE_SCHEMA_VERSION = 6
LEGACY_CASE_EVIDENCE_SCHEMA_VERSION = 2
LEGACY_SELF_ATTESTED_SCHEMA_VERSION = 3
LEGACY_LOCAL_RECEIPT_SCHEMA_VERSION = 4
LEGACY_ADAPTER_RECEIPT_SCHEMA_VERSION = 5
LEGACY_DETAILED_SCHEMA_VERSIONS = frozenset({
    LEGACY_CASE_EVIDENCE_SCHEMA_VERSION,
    LEGACY_SELF_ATTESTED_SCHEMA_VERSION,
    LEGACY_LOCAL_RECEIPT_SCHEMA_VERSION,
    LEGACY_ADAPTER_RECEIPT_SCHEMA_VERSION,
})
FLOAT_TOLERANCE = 1e-12


class EvalEvidenceOrigin(StrEnum):
    """Typed trust origin for one evaluation observation."""

    AM_ENGINE = "am_engine"
    ADAPTER_INTEGRITY = "adapter_integrity"
    CUSTOM = "custom"
    LEGACY_UNPROVEN = "legacy_unproven"


@dataclass(frozen=True, slots=True)
class EvalInferenceObservation:
    """Raw model output plus the provenance needed to establish engine trust."""

    output: str
    origin: EvalEvidenceOrigin
    engine_request_id: str | None = None
    engine_trace_id: str | None = None
    engine_instance_id: str | None = None
    engine_model_id: str | None = None
    engine_model_sha256: str | None = None
    engine_receipt_id: str | None = None
    engine_receipt: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.output, str) or not self.output.strip():
            raise ValueError("evaluation produced an empty observation")
        if not isinstance(self.origin, EvalEvidenceOrigin):
            raise ValueError("evaluation origin must be a typed EvalEvidenceOrigin")
        if self.origin in {EvalEvidenceOrigin.AM_ENGINE, EvalEvidenceOrigin.ADAPTER_INTEGRITY}:
            for name in (
                "engine_request_id",
                "engine_trace_id",
                "engine_instance_id",
                "engine_model_id",
                "engine_model_sha256",
                "engine_receipt_id",
            ):
                value = getattr(self, name)
                if not isinstance(value, str) or not value.strip():
                    raise ValueError(f"AM Engine evidence requires non-empty {name}")
            if not _is_sha256(self.engine_model_sha256):
                raise ValueError("AM Engine evidence requires a lowercase engine_model_sha256")
            if self.origin is EvalEvidenceOrigin.AM_ENGINE:
                if not isinstance(self.engine_receipt, Mapping) or not self.engine_receipt:
                    raise ValueError("AM Engine evidence requires the complete signed engine_receipt")
                object.__setattr__(self, "engine_receipt", _detached_json_mapping(self.engine_receipt))
            elif self.engine_receipt is not None:
                raise ValueError("adapter-integrity evidence cannot claim an engine-authored receipt")
        elif any(
            value is not None
            for value in (
                self.engine_request_id,
                self.engine_trace_id,
                self.engine_instance_id,
                self.engine_model_id,
                self.engine_model_sha256,
                self.engine_receipt_id,
                self.engine_receipt,
            )
        ):
            raise ValueError("non-engine evidence cannot claim engine request/model provenance")

    def __repr__(self) -> str:
        """Return a compact provenance representation without the full output."""
        return (
            f"EvalInferenceObservation(origin={self.origin.value!r}, "
            f"engine_request_id={self.engine_request_id!r}, engine_model_id={self.engine_model_id!r})"
        )


@dataclass(frozen=True, slots=True)
class EvalCaseProvenance:
    """Per-attempt AM Engine receipt correlation kept outside semantic rows."""

    case_id: str
    ordinal: int
    request_id: str
    trace_id: str
    engine_instance_id: str
    model_id: str
    model_sha256: str
    receipt_id: str
    engine_receipt: Mapping[str, Any]

    def __post_init__(self) -> None:
        for name in ("case_id", "request_id", "trace_id", "engine_instance_id", "model_id", "receipt_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        if isinstance(self.ordinal, bool) or not isinstance(self.ordinal, int) or self.ordinal < 0:
            raise ValueError("ordinal must be a non-negative integer")
        if not _is_sha256(self.model_sha256):
            raise ValueError("model_sha256 must be a 64-character lowercase SHA-256 digest")
        if not isinstance(self.engine_receipt, Mapping) or not self.engine_receipt:
            raise ValueError("engine_receipt must be a non-empty object")
        object.__setattr__(self, "engine_receipt", _detached_json_mapping(self.engine_receipt))

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> EvalCaseProvenance:
        """Decode strict per-attempt provenance."""
        return cls(
            case_id=_required_string(raw, "case_id"),
            ordinal=_required_int(raw, "ordinal"),
            request_id=_required_string(raw, "request_id"),
            trace_id=_required_string(raw, "trace_id"),
            engine_instance_id=_required_string(raw, "engine_instance_id"),
            model_id=_required_string(raw, "model_id"),
            model_sha256=_required_string(raw, "model_sha256"),
            receipt_id=_required_string(raw, "receipt_id"),
            engine_receipt=_required_mapping(raw, "engine_receipt"),
        )

    def to_dict(self) -> dict[str, Any]:
        """Return canonical provenance JSON."""
        return {
            "case_id": self.case_id,
            "ordinal": self.ordinal,
            "request_id": self.request_id,
            "trace_id": self.trace_id,
            "engine_instance_id": self.engine_instance_id,
            "model_id": self.model_id,
            "model_sha256": self.model_sha256,
            "receipt_id": self.receipt_id,
            "engine_receipt": _detached_json_mapping(self.engine_receipt),
        }

    def __repr__(self) -> str:
        """Return compact correlation identity."""
        return (
            f"EvalCaseProvenance(case_id={self.case_id!r}, ordinal={self.ordinal!r}, "
            f"request_id={self.request_id!r}, model_id={self.model_id!r})"
        )


@dataclass(frozen=True, slots=True)
class EvalCaseSpec:
    """Stable definition of one deterministic evaluation case."""

    case_id: str
    ordinal: int
    prompt: str
    expected: str
    seed: int
    threshold: float

    def __repr__(self) -> str:
        """Return a compact stable-identity representation."""
        return f"EvalCaseSpec(case_id={self.case_id!r}, ordinal={self.ordinal!r}, seed={self.seed!r})"

    def __post_init__(self) -> None:
        if not self.case_id.strip():
            raise ValueError("case_id must be non-empty")
        if isinstance(self.ordinal, bool) or not isinstance(self.ordinal, int) or self.ordinal < 0:
            raise ValueError("ordinal must be a non-negative integer")
        if not self.prompt.strip():
            raise ValueError("prompt must be non-empty")
        if not self.expected.strip():
            raise ValueError("expected must be non-empty")
        if isinstance(self.seed, bool) or not isinstance(self.seed, int) or self.seed < 0:
            raise ValueError("seed must be a non-negative integer")
        if not math.isfinite(self.threshold) or not 0.0 <= self.threshold <= 1.0:
            raise ValueError("threshold must be finite and in [0.0, 1.0]")


def _canonical_json_sha256(payload: Any) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and value == value.lower()
        and all(character in "0123456789abcdef" for character in value)
    )


def _case_spec_sha256(case: EvalCaseSpec) -> str:
    return _canonical_json_sha256({
        "case_id": case.case_id,
        "expected": case.expected,
        "ordinal": case.ordinal,
        "prompt": case.prompt,
        "seed": case.seed,
        "threshold": case.threshold,
    })


def _suite_revision_sha256(suite_id: str, cases: Sequence[EvalCaseSpec]) -> str:
    return _canonical_json_sha256({
        "cases": [
            {
                "case_spec_sha256": _case_spec_sha256(case),
                "case_id": case.case_id,
                "ordinal": case.ordinal,
            }
            for case in cases
        ],
        "suite_id": suite_id,
    })


def _validated_suite(cases: Sequence[EvalCaseSpec]) -> tuple[EvalCaseSpec, ...]:
    suite = tuple(cases)
    if not suite:
        raise ValueError("evaluation suite must contain at least one case")
    if [case.ordinal for case in suite] != list(range(len(suite))):
        raise ValueError("evaluation case ordinals must be contiguous and ordered from zero")
    case_ids = [case.case_id for case in suite]
    if len(case_ids) != len(set(case_ids)):
        raise ValueError("evaluation case IDs must be unique")
    return suite


_DEFAULT_SAMPLE_CASES = _validated_suite((
    EvalCaseSpec("default-001", 0, "What is 2+2?", "4", 1729, 1.0),
    EvalCaseSpec("default-002", 1, "Name the capital of France.", "paris", 2718, 1.0),
    EvalCaseSpec("default-003", 2, "Is Python a programming language?", "yes", 3141, 1.0),
))


def _same_float(left: float, right: float) -> bool:
    return math.isclose(left, right, rel_tol=0.0, abs_tol=FLOAT_TOLERANCE)


def _required_string(row: Mapping[str, Any], key: str) -> str:
    value = row.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be a non-empty string")
    return value


def _required_int(row: Mapping[str, Any], key: str) -> int:
    value = row.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{key} must be an integer")
    return value


def _required_mapping(row: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = row.get(key)
    if not isinstance(value, Mapping) or not value:
        raise ValueError(f"{key} must be a non-empty object")
    return value


def _detached_json_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        decoded = json.loads(encoded)
    except (TypeError, ValueError) as exc:
        raise ValueError("engine_receipt must contain only JSON-compatible values") from exc
    if not isinstance(decoded, dict) or not decoded:
        raise ValueError("engine_receipt must be a non-empty object")
    return decoded


def _required_float(row: Mapping[str, Any], key: str) -> float:
    value = row.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{key} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{key} must be finite")
    return result


@dataclass(frozen=True, slots=True)
class EvalCaseResult:
    """Immutable semantic result with construction-time score validation."""

    case_id: str
    ordinal: int
    prompt: str
    expected: str
    observed: str
    seed: int
    token_f1: float
    threshold: float
    passed: bool

    def __post_init__(self) -> None:
        """Reject caller-forged score or pass state at construction."""
        from vetinari.evaluation.arena import _contains_score

        spec = EvalCaseSpec(
            self.case_id,
            self.ordinal,
            self.prompt,
            self.expected,
            self.seed,
            self.threshold,
        )
        if not isinstance(self.observed, str) or not self.observed.strip():
            raise ValueError(f"case {spec.case_id} produced an empty observation")
        expected_score = _contains_score(self.observed, spec.expected.casefold())
        if isinstance(self.token_f1, bool) or not isinstance(self.token_f1, (int, float)):
            raise ValueError("token_f1 must be numeric")
        if not _same_float(float(self.token_f1), expected_score):
            raise ValueError(f"case {spec.case_id} token_f1 does not recompute")
        if not isinstance(self.passed, bool) or self.passed is not (expected_score >= spec.threshold):
            raise ValueError(f"case {spec.case_id} passed flag does not recompute")

    @classmethod
    def from_observation(
        cls,
        spec: EvalCaseSpec,
        observed: str,
    ) -> EvalCaseResult:
        """Build a deterministic semantic row from stable identity and raw output.

        Args:
            spec: Immutable case specification that supplies trusted identity.
            observed: Raw non-empty model output to score.

        Returns:
            Immutable result with recomputed token-F1 and pass decision.

        Raises:
            ValueError: If the observation is empty.
        """
        from vetinari.evaluation.arena import _contains_score

        if not isinstance(observed, str) or not observed.strip():
            raise ValueError(f"case {spec.case_id} produced an empty observation")
        token_f1 = _contains_score(observed, spec.expected.casefold())
        return cls(
            case_id=spec.case_id,
            ordinal=spec.ordinal,
            prompt=spec.prompt,
            expected=spec.expected,
            observed=observed,
            seed=spec.seed,
            token_f1=token_f1,
            threshold=spec.threshold,
            passed=token_f1 >= spec.threshold,
        )

    @classmethod
    def from_dict(
        cls,
        raw: Mapping[str, Any],
    ) -> EvalCaseResult:
        """Decode and recompute a persisted row.

        Args:
            raw: Persisted case-row mapping.

        Returns:
            Strictly validated immutable case result.

        Raises:
            ValueError: If identity, score, or pass state is invalid.
        """
        spec = EvalCaseSpec(
            case_id=_required_string(raw, "case_id"),
            ordinal=_required_int(raw, "ordinal"),
            prompt=_required_string(raw, "prompt"),
            expected=_required_string(raw, "expected"),
            seed=_required_int(raw, "seed"),
            threshold=_required_float(raw, "threshold"),
        )
        result = cls.from_observation(
            spec,
            _required_string(raw, "observed"),
        )
        stored_score = _required_float(raw, "token_f1")
        if not _same_float(stored_score, result.token_f1):
            raise ValueError(f"case {spec.case_id} token_f1 does not recompute")
        stored_passed = raw.get("passed")
        if not isinstance(stored_passed, bool) or stored_passed is not result.passed:
            raise ValueError(f"case {spec.case_id} passed flag does not recompute")
        return result

    def to_dict(self) -> dict[str, Any]:
        """Return the canonical persisted representation."""
        return {
            "case_id": self.case_id,
            "ordinal": self.ordinal,
            "prompt": self.prompt,
            "expected": self.expected,
            "observed": self.observed,
            "seed": self.seed,
            "token_f1": self.token_f1,
            "threshold": self.threshold,
            "passed": self.passed,
        }

    def __repr__(self) -> str:
        """Return a compact evidence-identity representation."""
        return (
            f"EvalCaseResult(case_id={self.case_id!r}, ordinal={self.ordinal!r}, "
            f"token_f1={self.token_f1!r}, passed={self.passed!r})"
        )


def _invoke_custom_inference(
    inference_fn: InferenceFn,
    model_id: str,
    prompt: str,
    eval_slot: int,
    seed: int,
) -> str:
    """Invoke versioned test callbacks without masking callback-body TypeErrors."""
    try:
        signature = inspect.signature(inference_fn)
    except (TypeError, ValueError) as exc:
        raise TypeError("inference_fn must expose a two- or four-argument signature") from exc
    four_args = (model_id, prompt, eval_slot, seed)
    accepts_four_arguments = True
    try:
        signature.bind(*four_args)
    except TypeError:
        accepts_four_arguments = False
        try:
            signature.bind(model_id, prompt)
        except TypeError as two_arg_error:
            raise TypeError(
                "inference_fn must accept (model_id, prompt) or include eval_slot and seed"
            ) from two_arg_error
    arguments = four_args if accepts_four_arguments else (model_id, prompt)
    return inference_fn(*arguments)
