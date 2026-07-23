"""Skill definitions for the Vetinari factory pipeline.

Contains the programmatic ``SKILL_REGISTRY`` dictionary with typed ``SkillSpec``
entries for each of the three canonical agents (Foreman, Worker, Inspector),
along with shared universal standards, constraints, and the agent-to-skill
mapping used by the public API in ``skill_registry``.

Split from ``skill_registry.py`` to separate data declarations from the
``SkillRegistry`` class and its public API surface.
"""

from __future__ import annotations

import logging

from vetinari.constants import MAX_TOKENS_FOREMAN, MAX_TOKENS_INSPECTOR_SKILL
from vetinari.skills.skill_spec import (
    SkillConstraint,
    SkillGuideline,
    SkillSpec,
    SkillStandard,
)
from vetinari.skills.worker_skill_def import _build_worker_skill
from vetinari.types import AgentType

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════
# Shared / Cross-Cutting Standards
# ═══════════════════════════════════════════════════════════════════════════

_UNIVERSAL_STANDARDS = [
    SkillStandard(
        id="STD-UNI-001",
        category="output_format",
        rule="All skill outputs MUST conform to the declared output_schema",
        severity="error",
        check_hint="jsonschema.validate(output, spec.output_schema)",
    ),
    SkillStandard(
        id="STD-UNI-002",
        category="error_handling",
        rule="All skill executions MUST return a ToolResult; exceptions MUST be caught and reported via error field",
        severity="error",
        check_hint="assert isinstance(result, ToolResult)",
    ),
    SkillStandard(
        id="STD-UNI-003",
        category="logging",
        rule="All skill executions MUST log entry and exit at INFO level with timing",
        severity="warning",
        check_hint="logger.info() at start and end",
    ),
    SkillStandard(
        id="STD-UNI-004",
        category="idempotency",
        rule="Skill executions SHOULD be idempotent — running twice with same input produces same output",
        severity="warning",
    ),
]

_UNIVERSAL_CONSTRAINTS = [
    SkillConstraint(
        id="CON-UNI-001",
        category="safety",
        description="No skill may execute arbitrary code from untrusted input without sandbox",
        limit="sandbox_required_for_untrusted=true",
        enforcement="hard",
    ),
    SkillConstraint(
        id="CON-UNI-002",
        category="resource",
        description="All skills must respect their max_tokens and timeout_seconds limits",
        limit="enforced_at_runtime",
        enforcement="hard",
    ),
    SkillConstraint(
        id="CON-UNI-003",
        category="resource",
        description="All skills must respect their max_cost_usd budget per invocation",
        limit="enforced_at_runtime",
        enforcement="hard",
    ),
]


# ═══════════════════════════════════════════════════════════════════════════
# Registry — 3 skills for the factory pipeline (ADR-0061)
# ═══════════════════════════════════════════════════════════════════════════

SKILL_REGISTRY: dict[str, SkillSpec] = {
    # ───────────────────────────────────────────────────────────────────
    # FOREMAN — plans, decomposes, orchestrates (6 modes)
    # ───────────────────────────────────────────────────────────────────
    "foreman": SkillSpec(
        skill_id="foreman",
        name="Foreman",
        version="2.0.0",
        description=(
            "Goal decomposition, task scheduling, dependency mapping, "
            "clarification, context consolidation, and knowledge extraction"
        ),
        agent_type=AgentType.FOREMAN.value,
        modes=["plan", "clarify", "consolidate", "summarise", "prune", "extract"],
        capabilities=[
            "goal_decomposition",
            "task_scheduling",
            "dependency_mapping",
            "specification",
            "clarification",
            "context_consolidation",
            "summarization",
            "token_management",
            "knowledge_extraction",
        ],
        input_schema={
            "type": "object",
            "required": ["goal"],
            "properties": {
                "goal": {
                    "type": "string",
                    "minLength": 1,
                    "description": "The high-level goal to decompose or clarify",
                },
                "constraints": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Constraints to respect during planning",
                },
                "context": {
                    "type": "object",
                    "description": "Additional context (codebase state, prior plans, memories)",
                },
                "mode": {
                    "type": "string",
                    "enum": ["plan", "clarify", "consolidate", "summarise", "prune", "extract"],
                    "description": "Execution mode to use",
                },
                "max_depth": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 5,
                    "default": 3,
                },
            },
        },
        output_schema={
            "type": "object",
            "required": ["plan_id", "goal"],
            "properties": {
                "plan_id": {"type": "string"},
                "goal": {"type": "string"},
                "version": {"type": "string"},
                "tasks": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["id", "description", "assigned_agent"],
                        "properties": {
                            "id": {"type": "string"},
                            "description": {"type": "string"},
                            "assigned_agent": {
                                "type": "string",
                                "enum": [AgentType.FOREMAN.value, AgentType.WORKER.value, AgentType.INSPECTOR.value],
                            },
                            "inputs": {"type": "array", "items": {"type": "string"}},
                            "outputs": {"type": "array", "items": {"type": "string"}},
                            "dependencies": {"type": "array", "items": {"type": "string"}},
                            "effort": {
                                "type": "string",
                                "enum": ["XS", "S", "M", "L", "XL"],
                            },
                            "acceptance_criteria": {"type": "string"},
                        },
                    },
                },
                "critical_path": {"type": "array", "items": {"type": "string"}},
                "risks": {"type": "array", "items": {"type": "string"}},
                "estimated_duration": {"type": "string"},
                "needs_context": {"type": "boolean"},
                "follow_up_question": {"type": ["string", "null"]},
            },
        },
        max_tokens=MAX_TOKENS_FOREMAN,
        timeout_seconds=600,
        max_cost_usd=1.00,
        requires_tools=["GeneratePlan"],
        min_verification_score=0.6,
        tags=["planning", "decomposition", "scheduling", "orchestration"],
        standards=[
            *_UNIVERSAL_STANDARDS,
            SkillStandard(
                id="STD-FMN-001",
                category="planning",
                rule="Every generated task MUST have a unique ID and an assigned_agent from {FOREMAN, WORKER, INSPECTOR}",
                severity="error",
                check_hint="all(t.get('id') and t.get('assigned_agent') for t in tasks)",
            ),
            SkillStandard(
                id="STD-FMN-002",
                category="planning",
                rule="Dependency graphs MUST be acyclic (DAG); circular dependencies are forbidden",
                severity="error",
                check_hint="topological_sort(dependency_graph) succeeds",
            ),
            SkillStandard(
                id="STD-FMN-003",
                category="planning",
                rule="Plans MUST include at least one verification task per implementation task",
                severity="warning",
                check_hint="count(verify_tasks) >= count(impl_tasks)",
            ),
            SkillStandard(
                id="STD-FMN-004",
                category="completeness",
                rule="Plans MUST include risk assessment and rollback strategy for destructive operations",
                severity="warning",
            ),
            SkillStandard(
                id="STD-FMN-005",
                category="clarity",
                rule="Clarification mode MUST produce specific, answerable questions — not open-ended prompts",
                severity="error",
                check_hint="all questions end with '?' and are self-contained",
            ),
            SkillStandard(
                id="STD-FMN-006",
                category="completeness",
                rule="Summarise mode output MUST preserve all key decisions and action items from input",
                severity="error",
            ),
        ],
        guidelines=[
            SkillGuideline(
                id="GDL-FMN-001",
                category="usage",
                recommendation="Prefer depth-first decomposition for complex goals; limit to 3 levels for simple goals",
                rationale="Deep decomposition increases precision but adds overhead",
            ),
            SkillGuideline(
                id="GDL-FMN-002",
                category="integration",
                recommendation="Schedule I/O-bound tasks in parallel; sequence CPU-bound tasks to avoid resource contention",
                rationale="Maximizes throughput without overloading the system",
            ),
            SkillGuideline(
                id="GDL-FMN-003",
                category="output_format",
                recommendation="Include effort estimates (XS/S/M/L/XL) on each task for capacity planning",
                rationale="Helps orchestrator allocate resources and set expectations",
            ),
            SkillGuideline(
                id="GDL-FMN-004",
                category="usage",
                recommendation="Use clarify mode before plan when requirements have >2 ambiguous elements",
                rationale="Prevents cascading misalignment in task decomposition",
            ),
        ],
        constraints=[
            *_UNIVERSAL_CONSTRAINTS,
            SkillConstraint(
                id="CON-FMN-001",
                category="scope",
                description="Maximum number of tasks in a single plan",
                limit="50 tasks",
                enforcement="hard",
            ),
            SkillConstraint(
                id="CON-FMN-002",
                category="scope",
                description="Maximum decomposition depth",
                limit="5 levels",
                enforcement="hard",
            ),
            SkillConstraint(
                id="CON-FMN-003",
                category="safety",
                description="Plans involving file deletion or destructive ops require explicit confirmation",
                limit="confirmation_required=true",
                enforcement="hard",
            ),
            SkillConstraint(
                id="CON-FMN-004",
                category="scope",
                description="Foreman MUST NOT execute tasks directly — only plan, clarify, and delegate",
                limit="execution_forbidden=true",
                enforcement="hard",
            ),
        ],
    ),
    # ───────────────────────────────────────────────────────────────────
    # WORKER — executes all production work across 24 modes in 4 groups
    # (definition in worker_skill_def.py to keep this file under 800 lines)
    # ───────────────────────────────────────────────────────────────────
    "worker": _build_worker_skill(_UNIVERSAL_STANDARDS, _UNIVERSAL_CONSTRAINTS),
    # ───────────────────────────────────────────────────────────────────
    # INSPECTOR — independent quality gate (4 modes)
    # ───────────────────────────────────────────────────────────────────
    "inspector": SkillSpec(
        skill_id="inspector",
        name="Inspector",
        version="2.0.0",
        description=(
            "Independent quality gate for code review, security audit, test generation, and code simplification"
        ),
        agent_type=AgentType.INSPECTOR.value,
        modes=["code_review", "security_audit", "test_generation", "simplification"],
        capabilities=[
            "code_review",
            "security_audit",
            "test_generation",
            "simplification",
            "performance_review",
            "best_practices_enforcement",
            "vulnerability_detection",
            "complexity_analysis",
        ],
        input_schema={
            "type": "object",
            "required": ["code"],
            "properties": {
                "code": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Code or content to review",
                },
                "mode": {
                    "type": "string",
                    "enum": ["code_review", "security_audit", "test_generation", "simplification"],
                    "description": "Review mode",
                },
                "context": {
                    "type": "object",
                    "description": "Review context (PR description, affected files, self_check result)",
                },
                "focus_areas": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Specific areas to focus the review on",
                },
                "thinking_mode": {
                    "type": "string",
                    "enum": ["none", "low", "medium", "high", "xhigh", "max"],
                },
            },
        },
        output_schema={
            "type": "object",
            "required": ["passed", "issues"],
            "properties": {
                "passed": {
                    "type": "boolean",
                    "description": "Whether the code passes the quality gate",
                },
                "issues": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["severity", "description"],
                        "properties": {
                            "severity": {
                                "type": "string",
                                "enum": ["critical", "high", "medium", "low", "info"],
                            },
                            "description": {"type": "string"},
                            "file": {"type": "string"},
                            "line": {"type": "integer"},
                            "category": {"type": "string"},
                            "cwe": {"type": "string"},
                            "owasp": {"type": "string"},
                            "suggestion": {"type": "string"},
                        },
                    },
                },
                "grade": {
                    "type": "string",
                    "enum": ["A", "B", "C", "D", "F"],
                },
                "score": {
                    "type": "number",
                    "minimum": 0.0,
                    "maximum": 1.0,
                },
                "suggestions": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "metrics": {
                    "type": "object",
                    "description": "Quantitative metrics (complexity, coverage, security score)",
                },
                "self_check_passed": {
                    "type": "boolean",
                    "description": "Whether the agent's self_check passed before review",
                },
            },
        },
        max_tokens=MAX_TOKENS_INSPECTOR_SKILL,
        timeout_seconds=600,
        max_cost_usd=2.00,
        requires_tools=["Read", "Glob", "Grep", "Bash"],
        min_verification_score=0.7,
        tags=["quality", "security", "testing", "review", "gate"],
        standards=[
            *_UNIVERSAL_STANDARDS,
            SkillStandard(
                id="STD-INS-001",
                category="review",
                rule="Code review MUST check all 5 dimensions: correctness, style, security, performance, maintainability",
                severity="error",
                check_hint="output covers all 5 review dimensions",
            ),
            SkillStandard(
                id="STD-INS-002",
                category="security",
                rule="Security audit MUST check OWASP Top 10 and map findings to CWE IDs",
                severity="error",
                check_hint="security findings include cwe field",
            ),
            SkillStandard(
                id="STD-INS-003",
                category="security",
                rule="Security audit MUST scan for hardcoded credentials, secrets, and API keys",
                severity="error",
            ),
            SkillStandard(
                id="STD-INS-004",
                category="testing",
                rule="Test generation MUST cover happy path, edge cases, and error paths",
                severity="error",
                check_hint="generated tests include happy, edge, and error categories",
            ),
            SkillStandard(
                id="STD-INS-005",
                category="review",
                rule="Every issue MUST have a severity level and actionable description",
                severity="error",
                check_hint="all issues have severity and description fields",
            ),
            SkillStandard(
                id="STD-INS-006",
                category="review",
                rule="Inspector MUST NOT modify code — only report findings and suggestions",
                severity="error",
            ),
            SkillStandard(
                id="STD-INS-007",
                category="gate",
                rule="Gate decision (passed=true/false) MUST be based on objective criteria, not subjective assessment",
                severity="error",
            ),
            SkillStandard(
                id="STD-INS-008",
                category="gate",
                rule="Gate decisions cannot be overridden by any other agent — only humans can bypass",
                severity="error",
            ),
        ],
        guidelines=[
            SkillGuideline(
                id="GDL-INS-001",
                category="usage",
                recommendation="Run security_audit on any code that handles user input, authentication, or external data",
                rationale="Input handling is the primary attack surface in most applications",
            ),
            SkillGuideline(
                id="GDL-INS-002",
                category="usage",
                recommendation="Use self_check_passed flag to weight gate decisions — failed self-check warrants deeper review",
                rationale="Self-check failures indicate the producing agent had low confidence",
            ),
            SkillGuideline(
                id="GDL-INS-003",
                category="output_format",
                recommendation="Group issues by severity (critical first) for efficient human triage",
                rationale="Severity ordering focuses attention on highest-impact issues first",
            ),
            SkillGuideline(
                id="GDL-INS-004",
                category="integration",
                recommendation="Use simplification mode after code_review identifies over-engineered areas",
                rationale="Simplification addresses maintainability issues found during review",
            ),
        ],
        constraints=[
            *_UNIVERSAL_CONSTRAINTS,
            SkillConstraint(
                id="CON-INS-001",
                category="scope",
                description="Inspector is READ-ONLY — cannot modify production files",
                limit="write_permission=false",
                enforcement="hard",
            ),
            SkillConstraint(
                id="CON-INS-002",
                category="safety",
                description="Inspector cannot be the same entity that produced the code under review",
                limit="producer_reviewer_separation=true",
                enforcement="hard",
            ),
            SkillConstraint(
                id="CON-INS-003",
                category="safety",
                description="Gate decisions (pass/fail) cannot be overridden by non-human agents",
                limit="human_override_only=true",
                enforcement="hard",
            ),
            SkillConstraint(
                id="CON-INS-004",
                category="resource",
                description="Security audit must complete within timeout even for large codebases",
                limit="timeout_seconds=600",
                enforcement="hard",
            ),
        ],
    ),
}


# ═══════════════════════════════════════════════════════════════════════════
# Canonical agent type -> skill mapping
# ═══════════════════════════════════════════════════════════════════════════

_AGENT_TO_SKILL: dict[str, str] = {
    AgentType.FOREMAN.value: "foreman",
    AgentType.WORKER.value: "worker",
    AgentType.INSPECTOR.value: "inspector",
}
