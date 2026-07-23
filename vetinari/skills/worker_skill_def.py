"""Worker skill specification for the Vetinari factory pipeline.

Contains the ``_WORKER_SKILL`` constant - the full ``SkillSpec`` definition
for the Worker agent covering research, architecture, build, and operations
across 24 modes in 4 mode groups.

Split from ``skill_definitions.py`` because the Worker spec alone exceeds
400 lines of declarative data.
"""

from __future__ import annotations

import logging
from types import MappingProxyType

from vetinari.constants import MAX_TOKENS_WORKER
from vetinari.skills.skill_spec import (
    SkillConstraint,
    SkillGuideline,
    SkillSpec,
    SkillStandard,
)
from vetinari.types import AgentType

logger = logging.getLogger(__name__)


_WORKER_SKILL_BASE = MappingProxyType({
    "skill_id": "worker",
    "name": "Worker",
    "version": "2.0.0",
    "description": (
        "All-purpose execution agent covering research, architecture, build, "
        "and operations across 24 modes in 4 mode groups"
    ),
    "agent_type": AgentType.WORKER.value,
    "modes": [
        # Research modes (8)
        "code_discovery",
        "domain_research",
        "api_lookup",
        "lateral_thinking",
        "ui_design",
        "database",
        "devops",
        "git_workflow",
        # Architecture modes (5)
        "architecture",
        "risk_assessment",
        "ontological_analysis",
        "contrarian_review",
        "suggest",
        # Build modes (2)
        "build",
        "image_generation",
        # Operations modes (9)
        "documentation",
        "creative_writing",
        "cost_analysis",
        "experiment",
        "error_recovery",
        "synthesis",
        "improvement",
        "monitor",
        "devops_ops",
    ],
    "capabilities": [
        # Research capabilities
        "code_discovery",
        "api_research",
        "domain_research",
        "lateral_thinking",
        "dependency_analysis",
        "git_archaeology",
        "database_analysis",
        "infrastructure_research",
        # Architecture capabilities
        "architecture_review",
        "risk_assessment",
        "contrarian_analysis",
        "ontological_mapping",
        "api_contract_validation",
        # Build capabilities
        "feature_implementation",
        "refactoring",
        "test_writing",
        "bug_diagnosis",
        "error_handling_hardening",
        "code_generation",
        "image_generation",
        # Operations capabilities
        "documentation_generation",
        "cost_analysis",
        "error_recovery",
        "continuous_improvement",
        "experiment_runner",
        "monitoring",
        "synthesis",
        "creative_writing",
    ],
    "input_schema": {
        "type": "object",
        "required": ["task"],
        "properties": {
            "task": {
                "type": "string",
                "minLength": 1,
                "description": "Task description to execute",
            },
            "mode": {
                "type": "string",
                "description": "Execution mode (auto-resolved if omitted)",
            },
            "context": {
                "type": "object",
                "description": "Task context (files, dependencies, prior results)",
            },
            "files": {
                "type": "array",
                "items": {"type": "string"},
                "description": "File paths relevant to the task",
            },
            "thinking_mode": {
                "type": "string",
                "enum": ["none", "low", "medium", "high", "xhigh", "max"],
                "description": "Thinking budget tier",
            },
        },
    },
    "output_schema": {
        "type": "object",
        "required": ["success", "output"],
        "properties": {
            "success": {"type": "boolean"},
            "output": {"description": "Task-specific output (string, object, or array)"},
            "files_changed": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Files modified during execution (build modes)",
            },
            "errors": {
                "type": "array",
                "items": {"type": "string"},
            },
            "warnings": {
                "type": "array",
                "items": {"type": "string"},
            },
            "metadata": {
                "type": "object",
                "description": "Mode-specific metadata (ADRs, research citations, cost data)",
            },
            "provenance": {
                "type": "array",
                "items": {"type": "object"},
                "description": "Chain of reasoning / evidence trail",
            },
        },
    },
    "max_tokens": MAX_TOKENS_WORKER,
    "timeout_seconds": 600,
    "max_cost_usd": 5.00,
    "requires_tools": [
        "Read",
        "Write",
        "Edit",
        "Glob",
        "Grep",
        "Bash",
        "WebSearch",
        "WebFetch",
        "ModelInference",
    ],
    "min_verification_score": 0.5,
    "tags": [
        "research",
        "architecture",
        "build",
        "operations",
        "code",
        "documentation",
        "analysis",
    ],
    "standards": [
        # -- Research standards --
        SkillStandard(
            id="STD-WRK-001",
            category="research",
            rule="Research modes MUST cite sources - file paths, URLs, or commit SHAs",
            severity="error",
            check_hint="output contains at least one source reference",
        ),
        SkillStandard(
            id="STD-WRK-002",
            category="research",
            rule="Code discovery MUST use progressive zoom: directory -> file -> function -> line",
            severity="warning",
        ),
        # -- Architecture standards --
        SkillStandard(
            id="STD-WRK-003",
            category="architecture",
            rule="Architecture mode MUST produce or reference an ADR for every design decision",
            severity="error",
            check_hint="output.metadata contains 'adr_id' or 'adr_reference'",
        ),
        SkillStandard(
            id="STD-WRK-004",
            category="architecture",
            rule="Architecture modes are READ-ONLY - MUST NOT modify production files",
            severity="error",
            check_hint="files_changed is empty for architecture modes",
        ),
        SkillStandard(
            id="STD-WRK-005",
            category="architecture",
            rule="Every design MUST state the chosen pattern and its rationale",
            severity="error",
        ),
        SkillStandard(
            id="STD-WRK-006",
            category="architecture",
            rule="Component designs MUST define clear boundaries (inputs, outputs, dependencies)",
            severity="error",
        ),
        SkillStandard(
            id="STD-WRK-007",
            category="architecture",
            rule="Database schemas MUST include indexes, constraints, and migration strategy",
            severity="error",
        ),
        SkillStandard(
            id="STD-WRK-008",
            category="architecture",
            rule="API designs MUST include authentication, authorization, and rate limiting",
            severity="error",
        ),
        SkillStandard(
            id="STD-WRK-009",
            category="architecture",
            rule="DevOps pipelines MUST include rollback procedure and health checks",
            severity="error",
        ),
        SkillStandard(
            id="STD-WRK-010",
            category="architecture",
            rule="Designs MUST list alternatives considered with trade-off analysis",
            severity="warning",
        ),
        # -- Build standards --
        SkillStandard(
            id="STD-WRK-011",
            category="code_quality",
            rule="Build mode is the SOLE writer of production files - no other mode may modify code",
            severity="error",
            check_hint="only build/image_generation modes have write permissions",
        ),
        SkillStandard(
            id="STD-WRK-012",
            category="code_quality",
            rule="All new code MUST have type annotations, Google-style docstrings, and tests",
            severity="error",
        ),
        SkillStandard(
            id="STD-WRK-013",
            category="code_quality",
            rule="Imports MUST use canonical sources (enums from vetinari.types, specs from contracts)",
            severity="error",
        ),
        SkillStandard(
            id="STD-WRK-014",
            category="code_quality",
            rule="No direct stdout writes in production code - use logging module with %-style formatting",
            severity="error",
        ),
        SkillStandard(
            id="STD-WRK-015",
            category="code_quality",
            rule="File I/O MUST specify encoding='utf-8'",
            severity="error",
        ),
        SkillStandard(
            id="STD-WRK-016",
            category="completeness",
            rule="No TODO, FIXME, pass bodies, NotImplementedError, or stub strings in delivered code",
            severity="error",
        ),
        # -- Operations standards --
        SkillStandard(
            id="STD-WRK-017",
            category="documentation",
            rule="Documentation mode output MUST have clear section structure with table of contents for >3 sections",
            severity="warning",
        ),
        SkillStandard(
            id="STD-WRK-018",
            category="operations",
            rule="Cost analysis MUST include per-model token breakdown and total estimated cost",
            severity="error",
        ),
        SkillStandard(
            id="STD-WRK-019",
            category="operations",
            rule="Error recovery MUST classify failure type before prescribing fixes",
            severity="error",
            check_hint="output contains failure_type classification",
        ),
        SkillStandard(
            id="STD-WRK-020",
            category="operations",
            rule="Improvement mode MUST follow PDCA cycle: Plan, Do, Check, Act",
            severity="warning",
        ),
    ],
    "guidelines": [
        # -- Research guidelines --
        SkillGuideline(
            id="GDL-WRK-001",
            category="usage",
            recommendation="Use code_discovery before build to understand existing patterns",
            rationale="Prevents reinventing existing utilities or contradicting conventions",
        ),
        SkillGuideline(
            id="GDL-WRK-002",
            category="usage",
            recommendation="Use lateral_thinking for novel problems with no clear precedent",
            rationale="Cross-domain analogies surface solutions invisible to domain experts",
        ),
        # -- Architecture guidelines --
        SkillGuideline(
            id="GDL-WRK-003",
            category="usage",
            recommendation="Always run contrarian_review after architecture for high-stakes decisions",
            rationale="Devil's advocate catches failure modes that optimistic review misses",
        ),
        SkillGuideline(
            id="GDL-WRK-004",
            category="integration",
            recommendation="Architecture decisions in risk_assessment should feed into Foreman's plan risk section",
            rationale="Risk-informed planning prevents costly mid-execution pivots",
        ),
        # -- Build guidelines --
        SkillGuideline(
            id="GDL-WRK-005",
            category="usage",
            recommendation="Write tests before implementation (TDD) for complex logic",
            rationale="Tests-first catches design issues early and ensures testable interfaces",
        ),
        SkillGuideline(
            id="GDL-WRK-006",
            category="performance",
            recommendation="Profile before optimizing - avoid premature optimization",
            rationale="Measured optimization targets real bottlenecks, not imagined ones",
        ),
        # -- Operations guidelines --
        SkillGuideline(
            id="GDL-WRK-007",
            category="usage",
            recommendation="Run documentation mode after every major feature completion",
            rationale="Fresh documentation captures intent that fades over time",
        ),
        SkillGuideline(
            id="GDL-WRK-008",
            category="integration",
            recommendation="Run improvement mode after every major feature completion to identify tech debt",
            rationale="Continuous improvement prevents debt accumulation",
        ),
    ],
    "constraints": [
        # -- Research constraints --
        SkillConstraint(
            id="CON-WRK-001",
            category="scope",
            description="Research modes are READ-ONLY - MUST NOT modify production files",
            limit="write_permission=false for research modes",
            enforcement="hard",
        ),
        # -- Architecture constraints --
        SkillConstraint(
            id="CON-WRK-002",
            category="scope",
            description="Architecture modes are READ-ONLY - produce ADRs, not code changes",
            limit="write_permission=false for architecture modes",
            enforcement="hard",
        ),
        SkillConstraint(
            id="CON-WRK-003",
            category="safety",
            description="High-stakes categories (architecture, security, data_flow) require 3+ alternatives evaluated",
            limit="min_alternatives=3 for high_stakes categories",
            enforcement="hard",
        ),
        # -- Build constraints --
        SkillConstraint(
            id="CON-WRK-004",
            category="scope",
            description="Build mode is the SOLE production file writer - enforced by execution context",
            limit="exclusive_write_lock=true",
            enforcement="hard",
        ),
        SkillConstraint(
            id="CON-WRK-005",
            category="safety",
            description="Destructive operations (delete, overwrite) require confirmation",
            limit="confirmation_required=true for destructive ops",
            enforcement="hard",
        ),
        # -- Operations constraints --
        SkillConstraint(
            id="CON-WRK-006",
            category="resource",
            description="Maximum output length for documentation generation",
            limit="10000 words",
            enforcement="soft",
        ),
        SkillConstraint(
            id="CON-WRK-007",
            category="safety",
            description="Error recovery MUST NOT execute destructive operations without confirmation",
            limit="confirmation_required=true",
            enforcement="hard",
        ),
        SkillConstraint(
            id="CON-WRK-008",
            category="scope",
            description="Cost analysis must not access external billing APIs without explicit permission",
            limit="external_api_permission_required=true",
            enforcement="hard",
        ),
        SkillConstraint(
            id="CON-WRK-009",
            category="scope",
            description="Operations modes run post-execution - MUST NOT modify already-reviewed code",
            limit="post_execution_only=true for operations modes",
            enforcement="hard",
        ),
    ],
})


def _merged_worker_skill_kwargs(
    universal_standards: list[SkillStandard],
    universal_constraints: list[SkillConstraint],
) -> dict[str, object]:
    """Merge injected universal rules into the static Worker SkillSpec kwargs."""
    kwargs = dict(_WORKER_SKILL_BASE)
    kwargs["standards"] = [*universal_standards, *kwargs["standards"]]
    kwargs["constraints"] = [*universal_constraints, *kwargs["constraints"]]
    return kwargs


def _build_worker_skill(
    universal_standards: list[SkillStandard],
    universal_constraints: list[SkillConstraint],
) -> SkillSpec:
    """Build the Worker SkillSpec with injected universal standards and constraints."""
    return SkillSpec(**_merged_worker_skill_kwargs(universal_standards, universal_constraints))
