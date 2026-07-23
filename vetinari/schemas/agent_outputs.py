"""Typed Output Schemas (C6).

==========================
Pydantic BaseModel schemas for every agent mode output. Used for
validation after _infer_json() calls with auto-repair on failure.

Each schema defines the expected structure of agent mode outputs,
enabling structured validation and IDE autocompletion.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from vetinari.errors import FailClosedError

logger = logging.getLogger(__name__)


if TYPE_CHECKING:
    from pydantic import BaseModel, Field
else:
    try:
        from pydantic import BaseModel, Field
    except ImportError:
        # Fallback: use dataclasses to mimic Pydantic when not installed

        class _BaseMeta(type):
            """Metaclass that adds model_validate and model_json_schema."""

            def __new__(mcs, name, bases, namespace):
                return super().__new__(mcs, name, bases, namespace)

        class BaseModel(metaclass=_BaseMeta):
            """Minimal Pydantic-like base when pydantic is not installed."""

            def __init__(self, **kwargs):
                """Initialize model fields from keyword arguments.

                Args:
                    **kwargs: Field name-value pairs to set on the model instance.
                """
                for k, v in kwargs.items():
                    setattr(self, k, v)

            @classmethod
            def model_validate(cls, data: dict) -> BaseModel:
                """Validate and construct a model instance from a raw dictionary.

                Args:
                    data: Dictionary of field values to validate.

                Returns:
                    A new model instance populated with the validated data.
                """
                return cls(**data)

            @classmethod
            def model_json_schema(cls) -> dict:
                """Return a minimal JSON Schema representation of the model.

                Returns:
                    Dictionary with a basic JSON Schema object type definition.
                """
                return {"type": "object", "properties": {}}

            def model_dump(self) -> dict:
                """Serialize the model instance to a plain dictionary, excluding private fields.

                Returns:
                    Dictionary of all public field name-value pairs.
                """
                return {k: v for k, v in self.__dict__.items() if not k.startswith("_")}

        def Field(default=None, **kwargs) -> Any:
            """Fallback Field function that returns the default value when Pydantic is unavailable.

            Args:
                default: The default value to return.
                **kwargs: Ignored keyword arguments for Pydantic API compatibility.

            Returns:
                The provided default value unchanged.
            """
            return kwargs["default_factory"]() if "default_factory" in kwargs else default


# ── Planner Agent Schemas ─────────────────────────────────────────────


class PlanOutput(BaseModel):
    """Output schema for ForemanAgent plan mode."""

    plan: list[dict[str, Any]] = Field(default_factory=list)
    summary: str = Field(default="")
    estimated_steps: int = Field(default=0)
    complexity: str = Field(default="medium")
    dependencies: list[str] = Field(default_factory=list)


class ClarifyOutput(BaseModel):
    """Output schema for ForemanAgent clarify mode."""

    questions: list[str] = Field(default_factory=list)
    ambiguities: list[dict[str, str]] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.5)


class SummariseOutput(BaseModel):
    """Output schema for ForemanAgent summarise mode."""

    summary: str = Field(default="")
    key_points: list[str] = Field(default_factory=list)
    decisions_made: list[str] = Field(default_factory=list)
    open_items: list[str] = Field(default_factory=list)


class ConsolidateOutput(BaseModel):
    """Output schema for ForemanAgent consolidate mode."""

    consolidated: str = Field(default="")
    entries_processed: int = Field(default=0)
    themes: list[str] = Field(default_factory=list)


# ── Researcher Agent Schemas ─────────────────────────────────────────


class CodeDiscoveryOutput(BaseModel):
    """Output schema for ResearcherAgent code_discovery mode."""

    files: list[dict[str, Any]] = Field(default_factory=list)
    patterns: list[str] = Field(default_factory=list)
    architecture: str = Field(default="")
    dependencies: list[str] = Field(default_factory=list)


class DomainResearchOutput(BaseModel):
    """Output schema for ResearcherAgent domain_research mode."""

    findings: list[dict[str, str]] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)
    summary: str = Field(default="")
    confidence: float = Field(default=0.5)
    recommendations: list[str] = Field(default_factory=list)


class APILookupOutput(BaseModel):
    """Output schema for ResearcherAgent api_lookup mode."""

    endpoints: list[dict[str, Any]] = Field(default_factory=list)
    documentation: str = Field(default="")
    examples: list[str] = Field(default_factory=list)
    version: str = Field(default="")


# ── Oracle Agent Schemas ──────────────────────────────────────────────


class ArchitectureOutput(BaseModel):
    """Output schema for OracleAgent architecture mode."""

    analysis: str = Field(default="")
    recommendations: list[dict[str, Any]] = Field(default_factory=list)
    patterns: list[str] = Field(default_factory=list)
    risks: list[dict[str, str]] = Field(default_factory=list)
    score: float = Field(default=0.5)


class RiskAssessmentOutput(BaseModel):
    """Output schema for OracleAgent risk_assessment mode."""

    risks: list[dict[str, Any]] = Field(default_factory=list)
    overall_risk: str = Field(default="medium")
    mitigations: list[dict[str, str]] = Field(default_factory=list)
    risk_matrix: dict[str, Any] = Field(default_factory=dict)


class ContrarianReviewOutput(BaseModel):
    """Output schema for OracleAgent contrarian_review mode."""

    challenges: list[dict[str, str]] = Field(default_factory=list)
    blind_spots: list[str] = Field(default_factory=list)
    alternative_approaches: list[dict[str, str]] = Field(default_factory=list)
    verdict: str = Field(default="")


# ── Worker Agent Schemas ──────────────────────────────────────────────


class BuildOutput(BaseModel):
    """Output schema for WorkerAgent build mode."""

    scaffold_code: str = Field(default="")
    tests: list[dict[str, str]] = Field(default_factory=list)
    artifacts: list[dict[str, str]] = Field(default_factory=list)
    implementation_notes: list[str] = Field(default_factory=list)
    summary: str = Field(default="")


class ImageGenerationOutput(BaseModel):
    """Output schema for WorkerAgent image_generation mode."""

    images: list[dict[str, Any]] = Field(default_factory=list)
    spec: dict[str, Any] = Field(default_factory=dict)
    sd_available: bool = Field(default=False)
    count: int = Field(default=0)


# ── Inspector Agent Schemas ───────────────────────────────────────────


class CodeReviewOutput(BaseModel):
    """Output schema for InspectorAgent code_review mode."""

    score: float = Field(default=0.5)
    summary: str = Field(default="")
    issues: list[dict[str, Any]] = Field(default_factory=list)
    strengths: list[str] = Field(default_factory=list)
    recommendations: list[str] = Field(default_factory=list)


class SecurityAuditOutput(BaseModel):
    """Output schema for InspectorAgent security_audit mode."""

    findings: list[dict[str, Any]] = Field(default_factory=list)
    summary: str = Field(default="")
    overall_risk: str = Field(default="medium")
    score: float = Field(default=0.5)
    heuristic_count: int = Field(default=0)


class TestGenerationOutput(BaseModel):
    """Output schema for InspectorAgent test_generation mode."""

    tests: str = Field(default="")
    test_count: int = Field(default=0)
    coverage_estimate: float = Field(default=0.0)
    fixtures: list[str] = Field(default_factory=list)
    edge_cases_covered: list[str] = Field(default_factory=list)


class SimplificationOutput(BaseModel):
    """Output schema for InspectorAgent simplification mode."""

    score: float = Field(default=0.5)
    complexity_issues: list[dict[str, str]] = Field(default_factory=list)
    overall_recommendations: list[str] = Field(default_factory=list)
    estimated_line_reduction: int = Field(default=0)


# ── Operations Agent Schemas ──────────────────────────────────────────


class DocumentationOutput(BaseModel):
    """Output schema for OperationsAgent documentation mode."""

    content: str = Field(default="")
    type: str = Field(default="api_reference")
    sections: list[dict[str, str]] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class CostAnalysisOutput(BaseModel):
    """Output schema for OperationsAgent cost_analysis mode."""

    comparisons: list[dict[str, Any]] = Field(default_factory=list)
    recommendation: str = Field(default="")
    estimated_tokens: int = Field(default=0)
    analysis: str = Field(default="")
    recommendations: list[dict[str, Any]] = Field(default_factory=list)


class ExperimentOutput(BaseModel):
    """Output schema for OperationsAgent experiment mode."""

    experiment: dict[str, Any] = Field(default_factory=dict)
    metrics: list[dict[str, Any]] = Field(default_factory=list)
    variants: list[dict[str, str]] = Field(default_factory=list)
    sample_size: int = Field(default=0)
    duration_days: int = Field(default=7)


class ErrorRecoveryOutput(BaseModel):
    """Output schema for OperationsAgent error_recovery mode."""

    root_cause: str = Field(default="")
    category: str = Field(default="")
    severity: str = Field(default="medium")
    recovery_strategy: dict[str, Any] = Field(default_factory=dict)
    matched_patterns: list[dict[str, Any]] = Field(default_factory=list)
    prevention: list[str] = Field(default_factory=list)


class SynthesisOutput(BaseModel):
    """Output schema for OperationsAgent synthesis mode."""

    synthesis: str = Field(default="")
    sources_used: list[str] = Field(default_factory=list)
    conflicts_resolved: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.5)


class MonitorOutput(BaseModel):
    """Output schema for OperationsAgent monitor mode."""

    status: str = Field(default="healthy")
    issues: list[str] = Field(default_factory=list)
    recommendations: list[str] = Field(default_factory=list)
    metrics_summary: dict[str, Any] = Field(default_factory=dict)


# ── Schema Registry ──────────────────────────────────────────────────

OUTPUT_SCHEMAS: dict[str, type[BaseModel]] = {
    # Planner
    "plan": PlanOutput,
    "clarify": ClarifyOutput,
    "summarise": SummariseOutput,
    "consolidate": ConsolidateOutput,
    # Researcher
    "code_discovery": CodeDiscoveryOutput,
    "domain_research": DomainResearchOutput,
    "api_lookup": APILookupOutput,
    # Oracle
    "architecture": ArchitectureOutput,
    "risk_assessment": RiskAssessmentOutput,
    "contrarian_review": ContrarianReviewOutput,
    # Builder
    "build": BuildOutput,
    "image_generation": ImageGenerationOutput,
    # Quality
    "code_review": CodeReviewOutput,
    "security_audit": SecurityAuditOutput,
    "test_generation": TestGenerationOutput,
    "simplification": SimplificationOutput,
    # Operations
    "documentation": DocumentationOutput,
    "cost_analysis": CostAnalysisOutput,
    "experiment": ExperimentOutput,
    "error_recovery": ErrorRecoveryOutput,
    "synthesis": SynthesisOutput,
    "monitor": MonitorOutput,
}


def _get_schema_class(mode: str) -> type[BaseModel] | None:
    """Return the registered output schema class for ``mode``."""
    return OUTPUT_SCHEMAS.get(mode)


def _schema_field_names(schema_cls: type[BaseModel]) -> set[str]:
    model_fields = getattr(schema_cls, "model_fields", None)
    if isinstance(model_fields, dict):
        return set(model_fields)
    annotations = getattr(schema_cls, "__annotations__", {})
    return set(annotations) if isinstance(annotations, dict) else set()


def validate_output(mode: str, data: Any) -> BaseModel | None:
    """Validate agent output against the schema for the given mode.

    Attempts strict Pydantic validation first; falls back to a partial
    construction using only the fields that are present in the schema.

    Args:
        mode: Agent mode key used to look up the schema (e.g. ``"plan"``, ``"build"``).
        data: Raw output to validate; must be a dict to pass validation.

    Returns:
        A validated schema instance if the mode has a registered schema and ``data``
        is a dict that can be coerced to it, or None if no schema exists, ``data``
        is not a dict, or all validation attempts fail.
    """
    schema_cls = OUTPUT_SCHEMAS.get(mode)
    if schema_cls is None:
        # Unknown mode — log and return None so callers treat it as unvalidatable,
        # not as a passing validation.
        if mode:
            logger.warning(
                "validate_output called with unknown mode '%s' — no schema registered, skipping validation",
                mode,
            )
        return None

    if not isinstance(data, dict):
        logger.warning(
            "validate_output for mode '%s' received non-dict input (%s) — validation failed",
            mode,
            type(data).__name__,
        )
        return None

    field_names = _schema_field_names(schema_cls)
    if field_names and not any(key in field_names for key in data):
        logger.warning(
            "validate_output for mode '%s' received no recognized schema fields; validation failed",
            mode,
        )
        return None

    try:
        return schema_cls.model_validate(data)
    except Exception:
        # Auto-repair: try to construct with available fields
        try:
            repair_data = {k: v for k, v in data.items() if k in field_names} if field_names else data
            return schema_cls(**repair_data)
        except Exception:
            logger.warning(
                "Auto-repair of %s output schema failed — could not construct from available fields, returning None",
                schema_cls.__name__,
            )
            return None


def get_schema_for_mode(mode: str) -> type[BaseModel] | None:
    """Look up the Pydantic output schema class registered for a given agent mode.

    Args:
        mode: The agent mode string (e.g. ``"code_review"``, ``"planning"``).

    Returns:
        The schema class, or ``None`` if the mode has no registered schema.
    """
    return OUTPUT_SCHEMAS.get(mode)


def validate_output_or_raise(mode: str, data: Any) -> BaseModel:
    """Validate agent output and raise a structured error on invalid state.

    Args:
        mode: Agent mode key used to select the expected output schema.
        data: Raw agent output payload to validate.

    Returns:
        The validated Pydantic model instance for ``mode``.

    Raises:
        FailClosedError: Raised when ``mode`` has no schema or ``data`` cannot
            be validated into that schema.
    """
    result = validate_output(mode, data)
    if result is None:
        raise FailClosedError(
            "agent_output.schema_validation",
            f"could not validate output for mode '{mode}'",
            recovery="register the mode schema and provide a dictionary payload",
        )
    return result
