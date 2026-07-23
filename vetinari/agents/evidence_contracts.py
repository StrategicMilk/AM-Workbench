"""Evidence and outcome-signal contracts for agent quality gates."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Literal

from vetinari.boundary_guards import require_nonempty
from vetinari.exceptions import InsufficientEvidenceError
from vetinari.types import ArtifactKind, EvidenceBasis, ShardKind


def _validate_score(value: float, field_name: str) -> None:
    """Reject unverifiable quality scores outside the closed 0.0-1.0 range."""
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(float(value)):
        raise ValueError(f"{field_name} must be a finite numeric score in [0.0, 1.0]")
    if not 0.0 <= float(value) <= 1.0:
        raise ValueError(f"{field_name} must be in [0.0, 1.0]")


def _basis_token(basis: EvidenceBasis | str) -> str:
    return str(getattr(basis, "name", basis)).upper()


@dataclass(frozen=True, slots=True)
class Provenance:
    """Origin metadata attached to every OutcomeSignal."""

    source: str
    timestamp_utc: str
    model_id: str = ""
    tool_name: str = ""
    tool_version: str = ""
    attested_by: str = ""

    def __repr__(self) -> str:
        return (
            f"Provenance(source={self.source!r}, timestamp_utc={self.timestamp_utc!r},"
            f" tool_name={self.tool_name!r}, model_id={self.model_id!r})"
        )


@dataclass(frozen=True, slots=True)
class ToolEvidence:
    """A single deterministic tool result backing an OutcomeSignal."""

    tool_name: str
    command: str
    exit_code: int
    stdout_snippet: str = ""
    stdout_hash: str = ""
    passed: bool = False

    def __repr__(self) -> str:
        return f"ToolEvidence(tool_name={self.tool_name!r}, exit_code={self.exit_code!r}, passed={self.passed!r})"


@dataclass(frozen=True, slots=True)
class LLMJudgment:
    """A model-generated judgment backing an OutcomeSignal."""

    model_id: str
    summary: str
    score: float = 0.0
    reasoning: str = ""

    def __post_init__(self) -> None:
        _validate_score(self.score, "LLMJudgment.score")

    def __repr__(self) -> str:
        return f"LLMJudgment(model_id={self.model_id!r}, score={self.score!r}, summary={self.summary[:60]!r})"


@dataclass(frozen=True, slots=True)
class AttestedArtifact:
    """A concrete artifact that a human attested to as claim substantiation."""

    kind: ArtifactKind
    attested_by: str
    attested_at_utc: str
    payload: dict[str, Any]

    def __repr__(self) -> str:
        return (
            f"AttestedArtifact(kind={self.kind.value!r},"
            f" attested_by={self.attested_by!r},"
            f" attested_at_utc={self.attested_at_utc!r})"
        )


@dataclass(frozen=True, slots=True)
class OutcomeSignal:
    """Evidence-backed verdict on whether an agent output meets its contract."""

    passed: bool = False
    score: float = 0.0
    basis: EvidenceBasis = EvidenceBasis.UNSUPPORTED
    tool_evidence: tuple[ToolEvidence, ...] = field(default_factory=tuple)
    llm_judgment: LLMJudgment | None = None
    attested_artifacts: tuple[AttestedArtifact, ...] = field(default_factory=tuple)
    provenance: Provenance | None = None
    issues: tuple[str, ...] = field(default_factory=tuple)
    suggestions: tuple[str, ...] = field(default_factory=tuple)
    use_case: Literal["INTENT_CONFIRMATION"] | None = None
    kind: ShardKind = ShardKind.STANDARD

    def __post_init__(self) -> None:
        _validate_score(self.score, "OutcomeSignal.score")
        evidence_basis_name = _basis_token(self.basis)
        if evidence_basis_name in {"TOOL_EVIDENCE", "TOOL_VERIFIED"} and (self.passed or not self.issues):
            require_nonempty(" ".join(str(item) for item in self.tool_evidence), field_name="tool_evidence")
        if evidence_basis_name == "LLM_JUDGED" and (self.passed or not self.issues):
            require_nonempty(
                str(self.llm_judgment) if self.llm_judgment is not None else "",
                field_name="llm_judgment",
            )
        if (
            self.basis is EvidenceBasis.HUMAN_ATTESTED
            and self.use_case != "INTENT_CONFIRMATION"
            and not self.attested_artifacts
        ):
            raise InsufficientEvidenceError(
                "HUMAN_ATTESTED OutcomeSignal requires at least one AttestedArtifact "
                "on non-intent-confirmation paths. Provide an AttestedArtifact (command "
                "invocation, commit SHA, signed review, ADR reference, or external "
                "receipt) or set use_case='INTENT_CONFIRMATION' for consent / override "
                "appeal paths.",
                basis=self.basis.value,
                use_case=self.use_case,
            )

    def __repr__(self) -> str:
        return (
            f"OutcomeSignal(passed={self.passed!r}, score={self.score!r},"
            f" basis={self.basis.value!r},"
            f" tool_evidence={len(self.tool_evidence)},"
            f" attested_artifacts={len(self.attested_artifacts)},"
            f" kind={self.kind.value!r})"
        )
