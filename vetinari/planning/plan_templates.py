"""Plan domain and agent subtask template data.

Contains the hardcoded template dictionaries that seed plan generation
when LLM-based candidate generation is unavailable or for fallback
candidate construction.

Extracted from ``plan_mode.py`` to keep each module under the 550-line
ceiling (ADR file-size rule). ``PlanModeEngine`` imports these at
construction time via ``_load_domain_templates()`` and
``_load_agent_templates()``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from vetinari.boundary_guards import require_nonempty
from vetinari.planning.plan_types import (
    DefinitionOfDone,
    DefinitionOfReady,
    TaskDomain,
)

# -- Domain templates --
# Each entry maps a TaskDomain to a list of subtask template dicts.
# The dicts are consumed by PlanModeEngine._create_subtasks_from_candidate().
DOMAIN_TEMPLATES: dict[TaskDomain, list[dict]] = {
    TaskDomain.CODING: [
        {
            "description": "Define API surface and data models",
            "domain": TaskDomain.CODING,
            "definition_of_done": DefinitionOfDone(
                criteria=["API spec written", "Data models defined", "Interfaces documented"],
            ),
            "definition_of_ready": DefinitionOfReady(prerequisites=["Requirements understood"]),
        },
        {
            "description": "Implement core functionality",
            "domain": TaskDomain.CODING,
            "definition_of_done": DefinitionOfDone(
                criteria=["Core logic implemented", "Code compiles", "Basic tests pass"],
            ),
            "definition_of_ready": DefinitionOfReady(prerequisites=["API surface defined"]),
        },
        {
            "description": "Write unit tests",
            "domain": TaskDomain.CODING,
            "definition_of_done": DefinitionOfDone(
                criteria=["Unit tests written", "Coverage > 80%", "All tests pass"],
            ),
            "definition_of_ready": DefinitionOfReady(prerequisites=["Core functionality implemented"]),
        },
        {
            "description": "Integrate with existing components",
            "domain": TaskDomain.CODING,
            "definition_of_done": DefinitionOfDone(
                criteria=["Integration points wired", "Integration tests pass"],
            ),
            "definition_of_ready": DefinitionOfReady(prerequisites=["Unit tests passing"]),
        },
        {
            "description": "Refactor for clarity and maintainability",
            "domain": TaskDomain.CODING,
            "definition_of_done": DefinitionOfDone(
                criteria=["Code reviewed", "Linting passes", "No critical tech debt"],
            ),
            "definition_of_ready": DefinitionOfReady(prerequisites=["Integration complete"]),
        },
    ],
    TaskDomain.DATA_PROCESSING: [
        {
            "description": "Define data schema and sources",
            "domain": TaskDomain.DATA_PROCESSING,
            "definition_of_done": DefinitionOfDone(criteria=["Schema documented", "Source systems identified"]),
            "definition_of_ready": DefinitionOfReady(prerequisites=[]),
        },
        {
            "description": "Build data ingestion pipeline",
            "domain": TaskDomain.DATA_PROCESSING,
            "definition_of_done": DefinitionOfDone(criteria=["Ingestion working", "Data validated at source"]),
            "definition_of_ready": DefinitionOfReady(prerequisites=["Schema defined"]),
        },
        {
            "description": "Implement transformation logic",
            "domain": TaskDomain.DATA_PROCESSING,
            "definition_of_done": DefinitionOfDone(criteria=["Transformations applied", "Output validated"]),
            "definition_of_ready": DefinitionOfReady(prerequisites=["Ingestion working"]),
        },
        {
            "description": "Implement data quality checks",
            "domain": TaskDomain.DATA_PROCESSING,
            "definition_of_done": DefinitionOfDone(
                criteria=["Quality checks implemented", "Alerts configured"],
            ),
            "definition_of_ready": DefinitionOfReady(prerequisites=["Transformations complete"]),
        },
        {
            "description": "Create deployment and scheduling",
            "domain": TaskDomain.DATA_PROCESSING,
            "definition_of_done": DefinitionOfDone(criteria=["Pipeline scheduled", "Monitoring active"]),
            "definition_of_ready": DefinitionOfReady(prerequisites=["Quality checks passing"]),
        },
    ],
    TaskDomain.INFRA: [
        {
            "description": "Define metrics and observability requirements",
            "domain": TaskDomain.INFRA,
            "definition_of_done": DefinitionOfDone(criteria=["Metrics catalog created", "SLOs defined"]),
            "definition_of_ready": DefinitionOfReady(prerequisites=[]),
        },
        {
            "description": "Implement health checks and readiness probes",
            "domain": TaskDomain.INFRA,
            "definition_of_done": DefinitionOfDone(
                criteria=["Health endpoints implemented", "Probes configured"],
            ),
            "definition_of_ready": DefinitionOfReady(prerequisites=["Metrics defined"]),
        },
        {
            "description": "Create monitoring dashboards",
            "domain": TaskDomain.INFRA,
            "definition_of_done": DefinitionOfDone(criteria=["Dashboards created", "Key metrics visible"]),
            "definition_of_ready": DefinitionOfReady(prerequisites=["Health checks working"]),
        },
        {
            "description": "Configure alerting rules",
            "domain": TaskDomain.INFRA,
            "definition_of_done": DefinitionOfDone(criteria=["Alert rules configured", "On-call defined"]),
            "definition_of_ready": DefinitionOfReady(prerequisites=["Dashboards complete"]),
        },
        {
            "description": "Document runbooks",
            "domain": TaskDomain.INFRA,
            "definition_of_done": DefinitionOfDone(criteria=["Runbooks written", "Team trained"]),
            "definition_of_ready": DefinitionOfReady(prerequisites=["Alert rules configured"]),
        },
    ],
    TaskDomain.DOCS: [
        {
            "description": "Outline documentation structure",
            "domain": TaskDomain.DOCS,
            "definition_of_done": DefinitionOfDone(criteria=["TOC created", "Audience defined"]),
            "definition_of_ready": DefinitionOfReady(prerequisites=[]),
        },
        {
            "description": "Draft main sections",
            "domain": TaskDomain.DOCS,
            "definition_of_done": DefinitionOfDone(
                criteria=["Sections drafted", "Technical accuracy verified"],
            ),
            "definition_of_ready": DefinitionOfReady(prerequisites=["Structure outlined"]),
        },
        {
            "description": "Add usage examples and code snippets",
            "domain": TaskDomain.DOCS,
            "definition_of_done": DefinitionOfDone(criteria=["Examples added", "Tested for accuracy"]),
            "definition_of_ready": DefinitionOfReady(prerequisites=["Main sections drafted"]),
        },
        {
            "description": "Review and get feedback",
            "domain": TaskDomain.DOCS,
            "definition_of_done": DefinitionOfDone(criteria=["Peer review complete", "Feedback addressed"]),
            "definition_of_ready": DefinitionOfReady(prerequisites=["Examples added"]),
        },
        {
            "description": "Finalize and publish",
            "domain": TaskDomain.DOCS,
            "definition_of_done": DefinitionOfDone(criteria=["Published", "Indexed", "Searchable"]),
            "definition_of_ready": DefinitionOfReady(prerequisites=["Review complete"]),
        },
    ],
    TaskDomain.AI_EXPERIMENTS: [
        {
            "description": "Define experiment metrics and success criteria",
            "domain": TaskDomain.AI_EXPERIMENTS,
            "definition_of_done": DefinitionOfDone(criteria=["Metrics defined", "Baseline established"]),
            "definition_of_ready": DefinitionOfReady(prerequisites=[]),
        },
        {
            "description": "Design experiment configuration",
            "domain": TaskDomain.AI_EXPERIMENTS,
            "definition_of_done": DefinitionOfDone(criteria=["Config documented", "Controls defined"]),
            "definition_of_ready": DefinitionOfReady(prerequisites=["Metrics defined"]),
        },
        {
            "description": "Run experiments",
            "domain": TaskDomain.AI_EXPERIMENTS,
            "definition_of_done": DefinitionOfDone(criteria=["Experiments executed", "Data collected"]),
            "definition_of_ready": DefinitionOfReady(prerequisites=["Config ready"]),
        },
        {
            "description": "Analyze results",
            "domain": TaskDomain.AI_EXPERIMENTS,
            "definition_of_done": DefinitionOfDone(
                criteria=["Results analyzed", "Statistical significance checked"],
            ),
            "definition_of_ready": DefinitionOfReady(prerequisites=["Data collected"]),
        },
        {
            "description": "Document insights and recommendations",
            "domain": TaskDomain.AI_EXPERIMENTS,
            "definition_of_done": DefinitionOfDone(criteria=["Insights documented", "Recommendations clear"]),
            "definition_of_ready": DefinitionOfReady(prerequisites=["Analysis complete"]),
        },
    ],
    TaskDomain.RESEARCH: [
        {
            "description": "Gather sources and literature",
            "domain": TaskDomain.RESEARCH,
            "definition_of_done": DefinitionOfDone(
                criteria=["Sources collected", "Relevant papers identified"],
            ),
            "definition_of_ready": DefinitionOfReady(prerequisites=[]),
        },
        {
            "description": "Summarize findings",
            "domain": TaskDomain.RESEARCH,
            "definition_of_done": DefinitionOfDone(criteria=["Summary written", "Key insights extracted"]),
            "definition_of_ready": DefinitionOfReady(prerequisites=["Sources gathered"]),
        },
        {
            "description": "Compare approaches",
            "domain": TaskDomain.RESEARCH,
            "definition_of_done": DefinitionOfDone(
                criteria=["Comparison matrix created", "Tradeoffs identified"],
            ),
            "definition_of_ready": DefinitionOfReady(prerequisites=["Summaries complete"]),
        },
        {
            "description": "Propose recommendations",
            "domain": TaskDomain.RESEARCH,
            "definition_of_done": DefinitionOfDone(criteria=["Recommendations clear", "Action items defined"]),
            "definition_of_ready": DefinitionOfReady(prerequisites=["Comparison done"]),
        },
        {
            "description": "Validate against goals",
            "domain": TaskDomain.RESEARCH,
            "definition_of_done": DefinitionOfDone(criteria=["Validation complete", "Final report ready"]),
            "definition_of_ready": DefinitionOfReady(prerequisites=["Recommendations proposed"]),
        },
    ],
    TaskDomain.GENERAL: [
        {
            "description": "Understand requirements and define scope",
            "domain": TaskDomain.GENERAL,
            "definition_of_done": DefinitionOfDone(
                criteria=["Requirements understood", "Scope defined", "Success criteria clear"],
            ),
            "definition_of_ready": DefinitionOfReady(prerequisites=[]),
        },
        {
            "description": "Plan approach and identify resources",
            "domain": TaskDomain.GENERAL,
            "definition_of_done": DefinitionOfDone(criteria=["Approach documented", "Resources identified"]),
            "definition_of_ready": DefinitionOfReady(prerequisites=["Scope defined"]),
        },
        {
            "description": "Execute the core work",
            "domain": TaskDomain.GENERAL,
            "definition_of_done": DefinitionOfDone(criteria=["Core deliverables produced", "Quality checked"]),
            "definition_of_ready": DefinitionOfReady(prerequisites=["Approach planned"]),
        },
        {
            "description": "Review and finalise deliverables",
            "domain": TaskDomain.GENERAL,
            "definition_of_done": DefinitionOfDone(
                criteria=["Deliverables reviewed", "Feedback incorporated", "Final output ready"],
            ),
            "definition_of_ready": DefinitionOfReady(prerequisites=["Core work complete"]),
        },
    ],
}

# -- Agent templates --
# Maps agent role names to their default subtask list.
# Consumed by PlanModeEngine._load_agent_templates().
AGENT_TEMPLATES: dict[str, list[dict]] = {
    "planner": [
        {"description": "Clarify objective and success criteria", "agent": "planner"},
        {"description": "Identify constraints and requirements", "agent": "planner"},
        {"description": "Draft initial plan skeleton", "agent": "planner"},
        {"description": "Enumerate dependencies and blockers", "agent": "planner"},
        {"description": "Propose alternative plan variants", "agent": "planner"},
        {"description": "Assess risks per plan variant", "agent": "planner"},
        {"description": "Provide final plan with justification", "agent": "planner"},
    ],
    "decomposer": [
        {"description": "Break down high-level features into modules", "agent": "decomposer"},
        {"description": "Decompose modules into interfaces and contracts", "agent": "decomposer"},
        {"description": "Split complex tasks by data flow", "agent": "decomposer"},
        {"description": "Identify edge cases and error paths", "agent": "decomposer"},
        {"description": "Group related subtasks for efficiency", "agent": "decomposer"},
    ],
    "breaker": [
        {"description": "Break API surface into individual endpoints", "agent": "breaker"},
        {"description": "Split data processing into ETL steps", "agent": "breaker"},
        {"description": "Decompose deployment into config steps", "agent": "breaker"},
        {"description": "Further break complex subtasks", "agent": "breaker"},
        {"description": "Ensure atomicity of each subtask", "agent": "breaker"},
    ],
    "assigner": [
        {"description": "Map tasks to local vs cloud models", "agent": "assigner"},
        {"description": "Select optimal model per task type", "agent": "assigner"},
        {"description": "Balance load across models", "agent": "assigner"},
        {"description": "Record assignment rationale", "agent": "assigner"},
        {"description": "Validate model capabilities match requirements", "agent": "assigner"},
    ],
    "executor": [
        {"description": "Install dependencies", "agent": "executor"},
        {"description": "Run core execution step", "agent": "executor"},
        {"description": "Collect and validate results", "agent": "executor"},
        {"description": "Handle errors and retries", "agent": "executor"},
        {"description": "Report execution status", "agent": "executor"},
    ],
    "explainer": [
        {"description": "Cite capability match for model selection", "agent": "explainer"},
        {"description": "Cite context fit rationale", "agent": "explainer"},
        {"description": "Document policy compliance notes", "agent": "explainer"},
        {"description": "Explain trade-offs considered", "agent": "explainer"},
        {"description": "Summarize decision justification", "agent": "explainer"},
    ],
    "memory": [
        {"description": "Log plan outcome to memory store", "agent": "memory", "runtime_agent": "FOREMAN"},
        {"description": "Record model performance metrics", "agent": "memory", "runtime_agent": "FOREMAN"},
        {"description": "Archive plan rationale for future reference", "agent": "memory", "runtime_agent": "FOREMAN"},
        {"description": "Update success rates based on outcome", "agent": "memory", "runtime_agent": "FOREMAN"},
        {"description": "Prune old plans per retention policy", "agent": "memory", "runtime_agent": "FOREMAN"},
    ],
}


def _validate_template_catalog(catalog: Mapping[object, Sequence[Mapping[str, object]]]) -> None:
    """Fail closed if static planning templates contain empty prompt text."""
    for template_rows in catalog.values():
        for row in template_rows:
            template_str = str(row.get("description", ""))
            require_nonempty(template_str, field_name="prompt_template")


_validate_template_catalog(DOMAIN_TEMPLATES)
_validate_template_catalog(AGENT_TEMPLATES)
