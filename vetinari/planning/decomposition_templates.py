"""Built-in decomposition templates."""

from __future__ import annotations

from typing import Any

from vetinari.types import AgentType

_TemplateSpec = tuple[str, str, list[str], str, str, list[str]]

_DEFAULT_TEMPLATE_SPECS: tuple[_TemplateSpec, ...] = (
    (
        "web_app",
        "Web Application",
        ["web", "app", "frontend", "react", "vue", "html"],
        AgentType.WORKER.value,
        "Standard",
        [
            "Define requirements and wireframes",
            "Set up project structure and dependencies",
            "Implement backend API",
            "Implement frontend components",
            "Write tests",
            "Deploy and configure CI/CD",
        ],
    ),
    (
        "data_pipeline",
        "Data Pipeline",
        ["data", "pipeline", "etl", "database", "sql"],
        AgentType.WORKER.value,
        "Standard",
        [
            "Define data schema and models",
            "Implement data ingestion",
            "Implement transformation logic",
            "Add validation and error handling",
            "Write pipeline tests",
            "Document data flow",
        ],
    ),
    (
        "research",
        "Research Task",
        ["research", "analyze", "investigate", "study"],
        AgentType.WORKER.value,
        "Light",
        [
            "Define research scope and questions",
            "Gather sources and references",
            "Analyze and synthesize findings",
            "Write research report",
        ],
    ),
    (
        "cli_tool",
        "CLI Tool",
        ["cli", "command", "terminal", "argparse", "click", "typer", "shell", "script"],
        AgentType.WORKER.value,
        "Standard",
        [
            "Define commands, flags, and argument schema",
            "Implement argument parsing and validation",
            "Implement core command logic",
            "Add help text, usage examples, and error messages",
            "Write unit and integration tests",
            "Package and document installation instructions",
        ],
    ),
    (
        "api_service",
        "REST API Service",
        ["api", "rest", "endpoint", "service", "fastapi", "flask", "django", "http"],
        AgentType.WORKER.value,
        "Hard",
        [
            "Define API contract, endpoints, and data models",
            "Implement authentication and authorization",
            "Implement endpoint handlers and business logic",
            "Add request validation and error handling",
            "Write unit and integration tests",
            "Generate OpenAPI documentation",
            "Configure deployment and environment settings",
        ],
    ),
    (
        "library",
        "Reusable Library",
        ["library", "package", "module", "sdk", "framework", "reusable", "pypi", "npm"],
        AgentType.WORKER.value,
        "Hard",
        [
            "Design public API and interface contracts",
            "Implement core functionality",
            "Write comprehensive unit and integration tests",
            "Write API reference documentation and usage examples",
            "Configure packaging, versioning, and build tooling",
            "Publish to package registry",
        ],
    ),
    (
        "document_generation",
        "Document Generation",
        ["document", "report", "generate", "pdf", "markdown", "template", "export"],
        AgentType.WORKER.value,
        "Standard",
        [
            "Define document structure and outline",
            "Gather and validate source data or content",
            "Draft document sections",
            "Review and revise for accuracy and clarity",
            "Apply formatting, styling, and branding",
            "Export and validate final output",
        ],
    ),
    (
        "creative_writing",
        "Creative Writing",
        ["creative", "writing", "story", "content", "blog", "article", "fiction", "copy"],
        AgentType.WORKER.value,
        "Light",
        [
            "Brainstorm concepts, themes, and angle",
            "Create detailed outline and structure",
            "Write first draft",
            "Revise for voice, pacing, and coherence",
            "Polish grammar, style, and final presentation",
        ],
    ),
    (
        "testing",
        "Test Suite Development",
        ["test", "testing", "qa", "coverage", "pytest", "jest", "unittest", "tdd"],
        AgentType.INSPECTOR.value,
        "Standard",
        [
            "Define test plan and coverage goals",
            "Write unit tests for core components",
            "Write integration tests for system boundaries",
            "Add edge case and negative path tests",
            "Measure coverage and close gaps",
            "Integrate tests into CI pipeline",
        ],
    ),
    (
        "refactoring",
        "Code Refactoring",
        ["refactor", "refactoring", "cleanup", "technical debt", "restructure", "reorganize"],
        AgentType.WORKER.value,
        "Standard",
        [
            "Analyze codebase and identify problem areas",
            "Define refactoring plan and risk assessment",
            "Apply incremental structural changes",
            "Verify existing tests still pass after each change",
            "Update documentation and comments",
            "Perform final review and cleanup",
        ],
    ),
    (
        "debugging",
        "Bug Investigation and Fix",
        ["bug", "debug", "fix", "error", "crash", "issue", "defect", "regression"],
        AgentType.INSPECTOR.value,
        "Standard",
        [
            "Reproduce the bug reliably with a minimal test case",
            "Diagnose root cause through logs and code inspection",
            "Implement targeted fix",
            "Write regression test to prevent recurrence",
            "Verify fix across affected scenarios",
            "Document root cause and resolution",
        ],
    ),
    (
        "migration",
        "System or Data Migration",
        ["migration", "migrate", "upgrade", "port", "convert", "transfer", "move"],
        AgentType.WORKER.value,
        "Hard",
        [
            "Inventory current state and map to target state",
            "Create migration plan with rollback strategy",
            "Implement migration scripts or procedures",
            "Run migration in staging and validate data integrity",
            "Execute production migration with monitoring",
            "Decommission legacy resources and update documentation",
        ],
    ),
    (
        "security_audit",
        "Security Audit",
        ["security", "audit", "vulnerability", "penetration", "pentest", "cve", "owasp"],
        AgentType.INSPECTOR.value,
        "Hard",
        [
            "Define scope and threat model",
            "Run automated vulnerability scans",
            "Manually analyze authentication, authorization, and data handling",
            "Prioritize findings by severity and exploitability",
            "Remediate critical and high-severity issues",
            "Verify remediations and produce final report",
        ],
    ),
    (
        "data_analysis",
        "Data Analysis Project",
        ["analysis", "analytics", "dataset", "statistics", "visualization", "insight", "notebook"],
        AgentType.WORKER.value,
        "Standard",
        [
            "Define analysis objectives and success metrics",
            "Collect and load raw data",
            "Clean, normalize, and validate data quality",
            "Perform exploratory analysis and statistical tests",
            "Create visualizations and charts",
            "Write findings report with recommendations",
        ],
    ),
    (
        "infrastructure",
        "Infrastructure and DevOps",
        ["infrastructure", "devops", "deployment", "kubernetes", "terraform", "ansible", "ci", "cd", "cloud"],
        AgentType.WORKER.value,
        "Hard",
        [
            "Define infrastructure requirements and architecture",
            "Write provisioning scripts or IaC configuration",
            "Configure networking, security groups, and IAM",
            "Deploy to staging and run smoke tests",
            "Set up monitoring, alerting, and logging",
            "Document runbooks and maintenance procedures",
        ],
    ),
)


def _template_from_spec(spec: _TemplateSpec) -> dict[str, Any]:
    template_id, name, keywords, agent_type, dod_level, subtasks = spec
    return {
        "template_id": template_id,
        "name": name,
        "keywords": list(keywords),
        "agent_type": agent_type,
        "dod_level": dod_level,
        "subtasks": list(subtasks),
    }


def build_default_templates() -> list[dict[str, Any]]:
    """Return built-in decomposition templates."""
    return [_template_from_spec(spec) for spec in _DEFAULT_TEMPLATE_SPECS]
