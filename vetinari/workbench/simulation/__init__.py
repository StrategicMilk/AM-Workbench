"""Synthetic simulation factory for Workbench coverage growth.

The factory builds import-safe, schema-shaped simulation records for eval,
red-team, training, and automation workflows. It does not write durable state
and it never promotes synthetic evidence into eval or training authority unless
the caller provides an explicit trust classification.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from threading import Lock
from typing import Any, Final

try:
    from jsonschema import Draft202012Validator
except ImportError:
    Draft202012Validator = None  # type: ignore[assignment]

import vetinari.constants as _constants
from vetinari.workbench.simulation.sandbox import (
    CaseReplayResult,
    ChangeSurface,
    CounterfactualChange,
    CounterfactualSimulationReport,
    CounterfactualSimulationSandbox,
    HistoricalReplayCase,
    MetricDelta,
    MetricProjector,
    MetricVector,
    SimulationEvidence,
    SimulationImpact,
    SimulationSandboxError,
    medium_or_high_impact_requires_simulation,
    summarize_report_for_governance,
)

_SCHEMA_VERSION: Final[str] = "1.0.0"
_MAX_CONSTRAINTS: Final[int] = 64
_MAX_REGRESSION_CASES: Final[int] = 500
_SCHEMA_PATH_DEFAULT: Final[Path] = _constants.PROJECT_ROOT / "schemas" / "workbench_simulation.schema.json"
_VALIDATOR_CACHE_LOCK = Lock()
_VALIDATOR_CACHE: dict[Path, tuple[tuple[int, int, int], Any]] = {}


class SimulationFactoryError(Exception):
    """Raised when a simulation record would be ambiguous or untrusted."""

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.reason = reason

    def __str__(self) -> str:
        return f"{self.args[0]} (reason={self.reason})"


class SyntheticLabel(str, Enum):
    """Synthetic-vs-real provenance label for generated cases."""

    SYNTHETIC = "synthetic"
    REAL = "real"
    HYBRID = "hybrid"


class TrustClassification(str, Enum):
    """Authority classification for generated simulation records."""

    UNCLASSIFIED = "unclassified"
    SANDBOX_ONLY = "sandbox_only"
    EVAL_CANDIDATE = "eval_candidate"
    TRAINING_CANDIDATE = "training_candidate"
    PRODUCTION_TRUTH = "production_truth"


class SimulationModality(str, Enum):
    """Kinds of simulated operator or agent interactions."""

    TEXT = "text"
    AGENT_WORKFLOW = "agent_workflow"
    VOICE_CONVERSATION = "voice_conversation"
    VIDEO_CONVERSATION = "video_conversation"


@dataclass(frozen=True, slots=True)
class SourceConstraint:
    """One source constraint that generated scenarios must trace back to."""

    constraint_id: str
    source_ref: str
    summary: str
    weight: float = 1.0

    def __post_init__(self) -> None:
        _require_non_empty(self.constraint_id, "constraint_id")
        _require_non_empty(self.source_ref, "source_ref")
        _require_non_empty(self.summary, "summary")
        if self.weight <= 0:
            raise ValueError("weight must be positive")

    def to_payload(self) -> dict[str, Any]:
        return {
            "constraint_id": self.constraint_id,
            "source_ref": self.source_ref,
            "summary": self.summary,
            "weight": self.weight,
        }

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"SourceConstraint(constraint_id={self.constraint_id!r}, source_ref={self.source_ref!r}, summary={self.summary!r})"


@dataclass(frozen=True, slots=True)
class AgentWorkflowStep:
    """One simulated agent workflow step."""

    step_id: str
    actor: str
    action: str
    expected_signal: str

    def __post_init__(self) -> None:
        _require_non_empty(self.step_id, "step_id")
        _require_non_empty(self.actor, "actor")
        _require_non_empty(self.action, "action")
        _require_non_empty(self.expected_signal, "expected_signal")

    def to_payload(self) -> dict[str, str]:
        return {
            "step_id": self.step_id,
            "actor": self.actor,
            "action": self.action,
            "expected_signal": self.expected_signal,
        }

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"AgentWorkflowStep(step_id={self.step_id!r}, actor={self.actor!r}, action={self.action!r})"


@dataclass(frozen=True, slots=True)
class ConversationHook:
    """Voice or video simulation hook for later execution engines."""

    hook_id: str
    modality: SimulationModality
    transcript_prompt: str
    media_assertion: str

    def __post_init__(self) -> None:
        _require_non_empty(self.hook_id, "hook_id")
        if self.modality not in {SimulationModality.VOICE_CONVERSATION, SimulationModality.VIDEO_CONVERSATION}:
            raise ValueError("conversation hook modality must be voice_conversation or video_conversation")
        _require_non_empty(self.transcript_prompt, "transcript_prompt")
        _require_non_empty(self.media_assertion, "media_assertion")

    def to_payload(self) -> dict[str, str]:
        return {
            "hook_id": self.hook_id,
            "modality": self.modality.value,
            "transcript_prompt": self.transcript_prompt,
            "media_assertion": self.media_assertion,
        }

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"ConversationHook(hook_id={self.hook_id!r}, modality={self.modality!r})"


@dataclass(frozen=True, slots=True)
class SimulationScenarioRecord:
    """Schema-shaped generated scenario record."""

    scenario_id: str
    project_id: str
    title: str
    synthetic_label: SyntheticLabel
    trust_classification: TrustClassification
    source_constraints: tuple[SourceConstraint, ...]
    eval_weight: float
    adversarial_tags: tuple[str, ...]
    workflow_steps: tuple[AgentWorkflowStep, ...]
    conversation_hooks: tuple[ConversationHook, ...]
    reusable_regression_refs: tuple[str, ...]
    created_at_utc: str
    allow_eval_authority: bool = False
    allow_training_authority: bool = False
    notes: str = ""

    def __post_init__(self) -> None:
        _require_non_empty(self.scenario_id, "scenario_id")
        _require_non_empty(self.project_id, "project_id")
        _require_non_empty(self.title, "title")
        if not self.source_constraints:
            raise ValueError("source_constraints must be non-empty")
        if len(self.source_constraints) > _MAX_CONSTRAINTS:
            raise ValueError(f"source_constraints cannot exceed {_MAX_CONSTRAINTS}")
        if self.eval_weight <= 0:
            raise ValueError("eval_weight must be positive")
        if not self.adversarial_tags:
            raise ValueError("adversarial_tags must be non-empty")
        if not self.workflow_steps:
            raise ValueError("workflow_steps must be non-empty")
        if self.trust_classification is TrustClassification.UNCLASSIFIED and (
            self.allow_eval_authority or self.allow_training_authority
        ):
            raise ValueError("unclassified simulations cannot enter eval or training authority")
        if self.allow_eval_authority and self.trust_classification not in {
            TrustClassification.EVAL_CANDIDATE,
            TrustClassification.PRODUCTION_TRUTH,
        }:
            raise ValueError("eval authority requires eval_candidate or production_truth trust classification")
        if self.allow_training_authority and self.trust_classification not in {
            TrustClassification.TRAINING_CANDIDATE,
            TrustClassification.PRODUCTION_TRUTH,
        }:
            raise ValueError("training authority requires training_candidate or production_truth trust classification")
        if (
            self.synthetic_label is SyntheticLabel.SYNTHETIC
            and self.trust_classification is TrustClassification.PRODUCTION_TRUTH
        ):
            raise ValueError("synthetic scenarios cannot be classified as production truth")

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": _SCHEMA_VERSION,
            "scenario_id": self.scenario_id,
            "project_id": self.project_id,
            "title": self.title,
            "synthetic_label": self.synthetic_label.value,
            "trust_classification": self.trust_classification.value,
            "source_constraints": [constraint.to_payload() for constraint in self.source_constraints],
            "eval_weight": self.eval_weight,
            "adversarial_tags": list(self.adversarial_tags),
            "workflow_steps": [step.to_payload() for step in self.workflow_steps],
            "conversation_hooks": [hook.to_payload() for hook in self.conversation_hooks],
            "reusable_regression_refs": list(self.reusable_regression_refs),
            "created_at_utc": self.created_at_utc,
            "allow_eval_authority": self.allow_eval_authority,
            "allow_training_authority": self.allow_training_authority,
            "notes": self.notes,
        }

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"SimulationScenarioRecord(scenario_id={self.scenario_id!r}, project_id={self.project_id!r}, title={self.title!r})"


@dataclass(frozen=True, slots=True)
class RegressionLibrary:
    """Reusable regression-library bundle built from generated scenarios."""

    library_id: str
    project_id: str
    scenarios: tuple[SimulationScenarioRecord, ...]
    created_at_utc: str

    def __post_init__(self) -> None:
        _require_non_empty(self.library_id, "library_id")
        _require_non_empty(self.project_id, "project_id")
        if not self.scenarios:
            raise ValueError("scenarios must be non-empty")
        if len(self.scenarios) > _MAX_REGRESSION_CASES:
            raise ValueError(f"scenarios cannot exceed {_MAX_REGRESSION_CASES}")
        if any(scenario.project_id != self.project_id for scenario in self.scenarios):
            raise ValueError("all scenarios must belong to the regression library project")

    def to_payload(self) -> dict[str, Any]:
        return {
            "library_id": self.library_id,
            "project_id": self.project_id,
            "created_at_utc": self.created_at_utc,
            "scenario_ids": [scenario.scenario_id for scenario in self.scenarios],
            "case_count": len(self.scenarios),
            "total_eval_weight": sum(scenario.eval_weight for scenario in self.scenarios),
            "adversarial_tags": sorted({tag for scenario in self.scenarios for tag in scenario.adversarial_tags}),
        }

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"RegressionLibrary(library_id={self.library_id!r}, project_id={self.project_id!r}, scenarios={self.scenarios!r})"


class SyntheticSimulationFactory:
    """Create schema-valid simulation records without durable writes."""

    def __init__(self, *, schema_path: Path | None = None) -> None:
        self._schema_path = schema_path or (_repo_root() / _SCHEMA_PATH_DEFAULT)

    def create_scenario(
        self,
        *,
        project_id: str,
        title: str,
        source_constraints: tuple[SourceConstraint, ...],
        adversarial_tags: tuple[str, ...],
        workflow_steps: tuple[AgentWorkflowStep, ...],
        synthetic_label: SyntheticLabel = SyntheticLabel.SYNTHETIC,
        trust_classification: TrustClassification = TrustClassification.UNCLASSIFIED,
        eval_weight: float = 1.0,
        conversation_hooks: tuple[ConversationHook, ...] = (),
        reusable_regression_refs: tuple[str, ...] = (),
        allow_eval_authority: bool = False,
        allow_training_authority: bool = False,
        notes: str = "",
    ) -> SimulationScenarioRecord:
        """Build and validate one simulation scenario record.

        Returns:
            Newly constructed scenario value.
        """
        _require_non_empty(project_id, "project_id")
        _require_non_empty(title, "title")
        record = SimulationScenarioRecord(
            scenario_id=f"sim-{uuid.uuid4().hex[:16]}",
            project_id=project_id,
            title=title,
            synthetic_label=synthetic_label,
            trust_classification=trust_classification,
            source_constraints=source_constraints,
            eval_weight=eval_weight,
            adversarial_tags=_dedupe_non_empty(adversarial_tags, "adversarial_tags"),
            workflow_steps=workflow_steps,
            conversation_hooks=conversation_hooks,
            reusable_regression_refs=_dedupe_non_empty(reusable_regression_refs, "reusable_regression_refs"),
            created_at_utc=_utc_now(),
            allow_eval_authority=allow_eval_authority,
            allow_training_authority=allow_training_authority,
            notes=notes,
        )
        self.validate_record(record)
        return record

    def create_regression_library(
        self,
        *,
        project_id: str,
        scenarios: tuple[SimulationScenarioRecord, ...],
    ) -> RegressionLibrary:
        """Bundle generated cases into a reusable regression-library descriptor.

        Returns:
            Newly constructed regression library value.

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        library = RegressionLibrary(
            library_id=f"sim-lib-{uuid.uuid4().hex[:16]}",
            project_id=project_id,
            scenarios=scenarios,
            created_at_utc=_utc_now(),
        )
        if not any(scenario.reusable_regression_refs for scenario in scenarios):
            raise SimulationFactoryError(
                "at least one scenario must carry a reusable regression reference",
                reason="missing-regression-ref",
            )
        return library

    def validate_record(self, record: SimulationScenarioRecord) -> None:
        """Validate a generated record against the owned JSON schema.

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        payload = record.to_payload()
        validator = _load_schema_validator(self._schema_path)
        errors = sorted(validator.iter_errors(payload), key=lambda err: list(err.path))
        if errors:
            details = "; ".join(f"{'.'.join(str(p) for p in err.path) or '<root>'}: {err.message}" for err in errors)
            raise SimulationFactoryError(f"schema validation failed; errors: {details}", reason="schema-validation")
        encoded = json.dumps(payload, sort_keys=True).encode("utf-8")
        if not encoded:
            raise SimulationFactoryError("record payload encoded to empty bytes", reason="empty-payload")


def create_simulation_scenario(
    *,
    project_id: str,
    title: str,
    source_constraints: tuple[SourceConstraint, ...],
    adversarial_tags: tuple[str, ...],
    workflow_steps: tuple[AgentWorkflowStep, ...],
    synthetic_label: SyntheticLabel = SyntheticLabel.SYNTHETIC,
    trust_classification: TrustClassification = TrustClassification.UNCLASSIFIED,
    eval_weight: float = 1.0,
    conversation_hooks: tuple[ConversationHook, ...] = (),
    reusable_regression_refs: tuple[str, ...] = (),
    allow_eval_authority: bool = False,
    allow_training_authority: bool = False,
    notes: str = "",
) -> SimulationScenarioRecord:
    """Convenience wrapper for one-off scenario creation."""
    return SyntheticSimulationFactory().create_scenario(
        project_id=project_id,
        title=title,
        source_constraints=source_constraints,
        adversarial_tags=adversarial_tags,
        workflow_steps=workflow_steps,
        synthetic_label=synthetic_label,
        trust_classification=trust_classification,
        eval_weight=eval_weight,
        conversation_hooks=conversation_hooks,
        reusable_regression_refs=reusable_regression_refs,
        allow_eval_authority=allow_eval_authority,
        allow_training_authority=allow_training_authority,
        notes=notes,
    )


def _load_schema(schema_path: Path) -> dict[str, Any]:
    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SimulationFactoryError("schema file unavailable or invalid", reason="schema-unavailable") from exc
    if Draft202012Validator is None:
        raise SimulationFactoryError("jsonschema is required for schema validation", reason="jsonschema-missing")
    Draft202012Validator.check_schema(schema)
    return schema


def _load_schema_validator(schema_path: Path) -> Any:
    if Draft202012Validator is None:
        raise SimulationFactoryError("jsonschema is required for schema validation", reason="jsonschema-missing")
    path = schema_path.resolve()
    try:
        stat = path.stat()
    except OSError as exc:
        raise SimulationFactoryError("schema file unavailable or invalid", reason="schema-unavailable") from exc
    signature = (stat.st_mtime_ns, stat.st_size, stat.st_ino)
    with _VALIDATOR_CACHE_LOCK:
        cached = _VALIDATOR_CACHE.get(path)
        if cached is not None and cached[0] == signature:
            return cached[1]
        schema = _load_schema(path)
        validator = Draft202012Validator(schema)
        _VALIDATOR_CACHE[path] = (signature, validator)
        return validator


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _require_non_empty(value: str, field_name: str) -> None:
    if not value or not value.strip():
        raise ValueError(f"{field_name} must be non-empty")


def _dedupe_non_empty(values: tuple[str, ...], field_name: str) -> tuple[str, ...]:
    seen: list[str] = []
    for value in values:
        _require_non_empty(value, field_name)
        if value not in seen:
            seen.append(value)
    return tuple(seen)


__all__ = [
    "AgentWorkflowStep",
    "CaseReplayResult",
    "ChangeSurface",
    "ConversationHook",
    "CounterfactualChange",
    "CounterfactualSimulationReport",
    "CounterfactualSimulationSandbox",
    "HistoricalReplayCase",
    "MetricDelta",
    "MetricProjector",
    "MetricVector",
    "RegressionLibrary",
    "SimulationEvidence",
    "SimulationFactoryError",
    "SimulationImpact",
    "SimulationModality",
    "SimulationSandboxError",
    "SimulationScenarioRecord",
    "SourceConstraint",
    "SyntheticLabel",
    "SyntheticSimulationFactory",
    "TrustClassification",
    "create_simulation_scenario",
    "medium_or_high_impact_requires_simulation",
    "summarize_report_for_governance",
]
