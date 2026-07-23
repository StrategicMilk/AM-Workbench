"""Fail-closed Workbench mode template catalog.

The catalog binds product-facing workbench modes to the typed spine
``ModeTemplate`` object and to Foreman planning primitives. Importing this
module performs no file I/O; callers explicitly load the YAML catalog when they
need mode contracts.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from vetinari.constants import PROJECT_ROOT
from vetinari.planning.plan_graph import PlanGraph
from vetinari.planning.spec_frame import SpecFrame
from vetinari.workbench.spine import ModeTemplate

DEFAULT_MODE_TEMPLATE_CONFIG_PATH = PROJECT_ROOT / "config" / "workbench_modes.yaml"
_MODE_TEMPLATE_CATALOG_LOCK = threading.Lock()
_MODE_TEMPLATE_CATALOG_CACHE: tuple[BoundModeTemplate, ...] | None = None


class ModeTemplateCatalogError(Exception):
    """Raised when the mode template catalog cannot be trusted."""


class TemplateStateRejected(ValueError):
    """Raised when runtime state is missing proof required by a template."""


@dataclass(frozen=True, slots=True)
class ModeToolPolicy:
    """Allowed tool surface and policy references for a mode."""

    allowed_tool_ids: tuple[str, ...]
    required_policy_refs: tuple[str, ...]
    requires_citations: bool
    max_external_calls: int

    def __post_init__(self) -> None:
        _require_non_empty_tuple(self.allowed_tool_ids, "allowed_tool_ids")
        _require_non_empty_tuple(self.required_policy_refs, "required_policy_refs")
        if not isinstance(self.requires_citations, bool):
            raise ModeTemplateCatalogError("requires_citations must be bool")
        if self.max_external_calls < 0:
            raise ModeTemplateCatalogError("max_external_calls must be non-negative")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"ModeToolPolicy(allowed_tool_ids={self.allowed_tool_ids!r}, required_policy_refs={self.required_policy_refs!r}, requires_citations={self.requires_citations!r})"


@dataclass(frozen=True, slots=True)
class MemoryPolicy:
    """Memory read/write contract for a mode."""

    ephemeral_keys: tuple[str, ...]
    persistent_keys: tuple[str, ...]
    retention_policy: str
    provenance_required: bool

    def __post_init__(self) -> None:
        if not self.ephemeral_keys and not self.persistent_keys:
            raise ModeTemplateCatalogError("memory policy must declare at least one key")
        _require_non_empty(self.retention_policy, "retention_policy")
        if self.provenance_required is not True:
            raise ModeTemplateCatalogError("memory policy must require provenance")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"MemoryPolicy(ephemeral_keys={self.ephemeral_keys!r}, persistent_keys={self.persistent_keys!r}, retention_policy={self.retention_policy!r})"


@dataclass(frozen=True, slots=True)
class OutputArtifactContract:
    """Artifacts a mode must produce before it can be promoted."""

    schema_ref: str
    required_artifacts: tuple[str, ...]
    export_paths: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_non_empty(self.schema_ref, "schema_ref")
        _require_non_empty_tuple(self.required_artifacts, "required_artifacts")
        _require_non_empty_tuple(self.export_paths, "export_paths")


@dataclass(frozen=True, slots=True)
class ReviewCriterion:
    """One review rubric row for a mode."""

    criterion_id: str
    description: str
    evidence_required: str

    def __post_init__(self) -> None:
        _require_non_empty(self.criterion_id, "criterion_id")
        _require_non_empty(self.description, "description")
        _require_non_empty(self.evidence_required, "evidence_required")


@dataclass(frozen=True, slots=True)
class TemplateTransition:
    """Allowed user-workflow state transition."""

    from_state: str
    to_state: str
    required_evidence: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_non_empty(self.from_state, "from_state")
        _require_non_empty(self.to_state, "to_state")
        _require_non_empty_tuple(self.required_evidence, "required_evidence")


@dataclass(frozen=True, slots=True)
class RecoveryStep:
    """User-visible recovery behavior for a failed mode run."""

    trigger: str
    action: str
    user_visible_status: str

    def __post_init__(self) -> None:
        _require_non_empty(self.trigger, "trigger")
        _require_non_empty(self.action, "action")
        _require_non_empty(self.user_visible_status, "user_visible_status")


@dataclass(frozen=True, slots=True)
class BoundModeTemplate:
    """Mode contract bound to spine and planning primitives."""

    template: ModeTemplate
    input_model: str
    tool_policy: ModeToolPolicy
    memory_policy: MemoryPolicy
    output_contract: OutputArtifactContract
    review_rubric: tuple[ReviewCriterion, ...]
    transitions: tuple[TemplateTransition, ...]
    failure_recovery: tuple[RecoveryStep, ...]
    provenance: tuple[tuple[str, str], ...]
    evidence_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_non_empty(self.input_model, "input_model")
        _require_non_empty_sequence(self.review_rubric, "review_rubric")
        _require_non_empty_sequence(self.transitions, "transitions")
        _require_non_empty_sequence(self.failure_recovery, "failure_recovery")
        if not self.provenance:
            raise ModeTemplateCatalogError("mode template provenance must be non-empty")
        if not self.evidence_refs:
            raise ModeTemplateCatalogError("mode template evidence_refs must be non-empty")

    @property
    def template_id(self) -> str:
        """Return the stable mode template id."""
        return self.template.template_id

    def to_spec_frame(self) -> SpecFrame:
        """Bind this template to the existing planning SpecFrame contract."""
        return bind_template_to_spec_frame(self)

    def to_plan_graph(self) -> PlanGraph:
        """Bind this template's transitions to the existing PlanGraph contract."""
        return bind_template_to_plan_graph(self)

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"BoundModeTemplate(template={self.template!r}, input_model={self.input_model!r}, tool_policy={self.tool_policy!r})"


class ModeTemplateRuntime:
    """Runtime validator for a selected mode template."""

    def __init__(self, template: BoundModeTemplate) -> None:
        self.template = template

    def require_ready_state(self, state: dict[str, Any]) -> None:
        """Reject state that lacks policy, provenance, evidence, or required artifacts.

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        if not isinstance(state, dict):
            raise TemplateStateRejected("template state must be a mapping")
        policy_ack = state.get("policy_acknowledged")
        if policy_ack is not True:
            raise TemplateStateRejected("mode template policy must be acknowledged")
        provenance = state.get("provenance")
        if not isinstance(provenance, dict) or not provenance:
            raise TemplateStateRejected("mode template provenance must be present")
        evidence_refs = _string_tuple(state.get("evidence_refs"))
        if not evidence_refs:
            raise TemplateStateRejected("mode template evidence_refs must be non-empty")
        artifacts = _string_tuple(state.get("artifacts"))
        missing = set(self.template.output_contract.required_artifacts) - set(artifacts)
        if missing:
            raise TemplateStateRejected(f"mode template artifacts missing: {sorted(missing)}")


def load_mode_template_catalog(
    path: Path | str = DEFAULT_MODE_TEMPLATE_CONFIG_PATH,
    *,
    use_cache: bool = True,
) -> tuple[BoundModeTemplate, ...]:
    """Return validated mode templates from the YAML catalog.

    Returns:
        Resolved mode template catalog value.
    """
    global _MODE_TEMPLATE_CATALOG_CACHE
    catalog_path = Path(path)
    if use_cache and catalog_path == DEFAULT_MODE_TEMPLATE_CONFIG_PATH and _MODE_TEMPLATE_CATALOG_CACHE is not None:
        return _MODE_TEMPLATE_CATALOG_CACHE
    if catalog_path == DEFAULT_MODE_TEMPLATE_CONFIG_PATH:
        with _MODE_TEMPLATE_CATALOG_LOCK:
            if use_cache and _MODE_TEMPLATE_CATALOG_CACHE is not None:
                return _MODE_TEMPLATE_CATALOG_CACHE
            catalog = _load_mode_template_catalog_uncached(catalog_path)
            if use_cache:
                _MODE_TEMPLATE_CATALOG_CACHE = catalog
            return catalog
    return _load_mode_template_catalog_uncached(catalog_path)


def list_mode_templates() -> tuple[BoundModeTemplate, ...]:
    """List all configured mode templates."""
    return load_mode_template_catalog()


def get_mode_template(template_id: str) -> BoundModeTemplate | None:
    """Return one mode template by id.

    Returns:
        Resolved mode template value.
    """
    for template in load_mode_template_catalog():
        if template.template_id == template_id:
            return template
    return None


def bind_template_to_spec_frame(template: BoundModeTemplate) -> SpecFrame:
    """Project a mode template into a planning SpecFrame."""
    return SpecFrame(
        goal=template.template.charter,
        in_scope=(
            f"input_model:{template.input_model}",
            *(f"tool:{tool_id}" for tool_id in template.tool_policy.allowed_tool_ids),
            *(f"artifact:{artifact}" for artifact in template.output_contract.required_artifacts),
        ),
        out_of_scope=(
            "unproven output promotion",
            "policy-bypassing tool execution",
            "memory writes without provenance",
        ),
        acceptance_criteria=tuple(
            f"{criterion.criterion_id}: {criterion.evidence_required}" for criterion in template.review_rubric
        ),
        anti_goals=tuple(step.user_visible_status for step in template.failure_recovery),
        frame_id=template.template_id,
    )


def bind_template_to_plan_graph(template: BoundModeTemplate) -> PlanGraph:
    """Build a transition graph for a mode template and validate it.

    Returns:
        PlanGraph value produced by bind_template_to_plan_graph().
    """
    graph = PlanGraph()
    for transition in template.transitions:
        graph.add_edge(transition.to_state, transition.from_state)
    graph.validate()
    return graph


def _load_mode_template_catalog_uncached(path: Path) -> tuple[BoundModeTemplate, ...]:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ModeTemplateCatalogError(f"mode template catalog unreadable: {path}") from exc
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise ModeTemplateCatalogError("mode template catalog schema_version must be 1")
    rows = raw.get("templates")
    if not isinstance(rows, list) or not rows:
        raise ModeTemplateCatalogError("mode template catalog must contain templates")
    templates = tuple(_parse_template_row(row) for row in rows)
    template_ids = [template.template_id for template in templates]
    if len(set(template_ids)) != len(template_ids):
        raise ModeTemplateCatalogError("mode template ids must be unique")
    return templates


def _parse_template_row(row: object) -> BoundModeTemplate:
    if not isinstance(row, dict):
        raise ModeTemplateCatalogError("mode template row must be a mapping")
    required = {
        "id",
        "name",
        "version",
        "charter",
        "input_model",
        "allowed_tools",
        "tool_policy",
        "memory_policy",
        "output_artifact_schema",
        "review_rubric",
        "transitions",
        "export_paths",
        "failure_recovery",
        "provenance",
        "evidence_refs",
    }
    missing = required - set(row)
    if missing:
        raise ModeTemplateCatalogError(f"mode template row missing keys: {sorted(missing)}")
    provenance = _string_pairs(row["provenance"])
    provenance_keys = {key for key, _value in provenance}
    if {"decision_ref", "source"} - provenance_keys:
        raise ModeTemplateCatalogError(f"mode template {row['id']!r} missing provenance decision_ref/source")
    output_contract = OutputArtifactContract(
        schema_ref=str(row["output_artifact_schema"]),
        required_artifacts=_string_tuple(row.get("required_artifacts", ())),
        export_paths=_string_tuple(row["export_paths"]),
    )
    template = ModeTemplate(
        template_id=str(row["id"]),
        name=str(row["name"]),
        version=str(row["version"]),
        charter=str(row["charter"]),
        allowed_tools=_string_tuple(row["allowed_tools"]),
        output_schema_ref=output_contract.schema_ref,
    )
    return BoundModeTemplate(
        template=template,
        input_model=str(row["input_model"]),
        tool_policy=_parse_tool_policy(row["tool_policy"], template.allowed_tools),
        memory_policy=_parse_memory_policy(row["memory_policy"]),
        output_contract=output_contract,
        review_rubric=tuple(_parse_review_criterion(item) for item in _list(row["review_rubric"], "review_rubric")),
        transitions=tuple(_parse_transition(item) for item in _list(row["transitions"], "transitions")),
        failure_recovery=tuple(
            _parse_recovery_step(item) for item in _list(row["failure_recovery"], "failure_recovery")
        ),
        provenance=provenance,
        evidence_refs=_string_tuple(row["evidence_refs"]),
    )


def _parse_tool_policy(raw: object, allowed_tools: tuple[str, ...]) -> ModeToolPolicy:
    if not isinstance(raw, dict):
        raise ModeTemplateCatalogError("tool_policy must be a mapping")
    policy_tools = _string_tuple(raw.get("allowed_tool_ids", ()))
    if set(policy_tools) != set(allowed_tools):
        raise ModeTemplateCatalogError("tool_policy.allowed_tool_ids must match allowed_tools")
    return ModeToolPolicy(
        allowed_tool_ids=policy_tools,
        required_policy_refs=_string_tuple(raw.get("required_policy_refs", ())),
        requires_citations=bool(raw.get("requires_citations", True)),
        max_external_calls=int(raw.get("max_external_calls", -1)),
    )


def _parse_memory_policy(raw: object) -> MemoryPolicy:
    if not isinstance(raw, dict):
        raise ModeTemplateCatalogError("memory_policy must be a mapping")
    return MemoryPolicy(
        ephemeral_keys=_string_tuple(raw.get("ephemeral_keys", ())),
        persistent_keys=_string_tuple(raw.get("persistent_keys", ())),
        retention_policy=str(raw.get("retention_policy", "")),
        provenance_required=bool(raw.get("provenance_required", False)),
    )


def _parse_review_criterion(raw: object) -> ReviewCriterion:
    if not isinstance(raw, dict):
        raise ModeTemplateCatalogError("review_rubric entries must be mappings")
    return ReviewCriterion(
        criterion_id=str(raw.get("id", "")),
        description=str(raw.get("description", "")),
        evidence_required=str(raw.get("evidence_required", "")),
    )


def _parse_transition(raw: object) -> TemplateTransition:
    if not isinstance(raw, dict):
        raise ModeTemplateCatalogError("transitions entries must be mappings")
    return TemplateTransition(
        from_state=str(raw.get("from", "")),
        to_state=str(raw.get("to", "")),
        required_evidence=_string_tuple(raw.get("required_evidence", ())),
    )


def _parse_recovery_step(raw: object) -> RecoveryStep:
    if not isinstance(raw, dict):
        raise ModeTemplateCatalogError("failure_recovery entries must be mappings")
    return RecoveryStep(
        trigger=str(raw.get("trigger", "")),
        action=str(raw.get("action", "")),
        user_visible_status=str(raw.get("user_visible_status", "")),
    )


def _require_non_empty(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ModeTemplateCatalogError(f"{field_name} must be non-empty")


def _require_non_empty_tuple(values: tuple[Any, ...], field_name: str) -> None:
    if not isinstance(values, tuple) or not values:
        raise ModeTemplateCatalogError(f"{field_name} must be a non-empty tuple")
    if not all(isinstance(value, str) and value.strip() for value in values):
        raise ModeTemplateCatalogError(f"{field_name} must contain non-empty strings")


def _require_non_empty_sequence(values: tuple[Any, ...], field_name: str) -> None:
    if not isinstance(values, tuple) or not values:
        raise ModeTemplateCatalogError(f"{field_name} must be a non-empty tuple")


def _list(raw: object, field_name: str) -> list[object]:
    if not isinstance(raw, list) or not raw:
        raise ModeTemplateCatalogError(f"{field_name} must be a non-empty list")
    return raw


def _string_tuple(raw: object) -> tuple[str, ...]:
    if raw is None:
        return ()
    if isinstance(raw, str):
        return (raw,) if raw.strip() else ()
    if isinstance(raw, (list, tuple)):
        return tuple(str(item) for item in raw if str(item).strip())
    return (str(raw),)


def _string_pairs(raw: object) -> tuple[tuple[str, str], ...]:
    if not isinstance(raw, dict) or not raw:
        raise ModeTemplateCatalogError("provenance must be a non-empty mapping")
    pairs = tuple(sorted((str(key), str(value)) for key, value in raw.items() if str(value).strip()))
    if not pairs:
        raise ModeTemplateCatalogError("provenance must contain non-empty values")
    return pairs


def _reset_mode_template_catalog_for_test() -> None:
    global _MODE_TEMPLATE_CATALOG_CACHE
    with _MODE_TEMPLATE_CATALOG_LOCK:
        _MODE_TEMPLATE_CATALOG_CACHE = None


__all__ = [
    "DEFAULT_MODE_TEMPLATE_CONFIG_PATH",
    "BoundModeTemplate",
    "MemoryPolicy",
    "ModeTemplateCatalogError",
    "ModeTemplateRuntime",
    "ModeToolPolicy",
    "OutputArtifactContract",
    "RecoveryStep",
    "ReviewCriterion",
    "TemplateStateRejected",
    "TemplateTransition",
    "bind_template_to_plan_graph",
    "bind_template_to_spec_frame",
    "get_mode_template",
    "list_mode_templates",
    "load_mode_template_catalog",
]
