"""Worker agent interface contract — implementation, research, and analysis.

Imported by vetinari.agents.interfaces — do not use directly.
"""

from __future__ import annotations

from vetinari.agents.interface_types import AgentInterface, Capability, CapabilityType
from vetinari.types import AgentType

# ===== WORKER INTERFACE =====
WORKER_INTERFACE = AgentInterface(
    agent_name="Worker",
    agent_type=AgentType.WORKER.value,
    version="1.0.0",
    capabilities=[
        Capability(
            name="build",
            type=CapabilityType.GENERATION,
            description="Generate code scaffold and full implementations from specification",
            input_schema={
                "type": "object",
                "properties": {
                    "spec": {"type": "string"},
                    "language": {"type": "string"},
                    "framework": {"type": "string"},
                },
                "required": ["spec"],
            },
            output_schema={
                "type": "object",
                "properties": {
                    "scaffold_code": {"type": "string"},
                    "tests": {"type": "array"},
                    "artifacts": {"type": "array"},
                },
            },
        ),
        Capability(
            name="code_discovery",
            type=CapabilityType.DISCOVERY,
            description="Search for code patterns, symbols, and implementations in the codebase",
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "scope": {"type": "string", "enum": ["code", "docs", "apis", "all"]},
                    "max_results": {"type": "integer"},
                },
                "required": ["query"],
            },
            output_schema={
                "type": "object",
                "properties": {
                    "findings": {"type": "array"},
                    "references": {"type": "array"},
                },
            },
        ),
        Capability(
            name="domain_research",
            type=CapabilityType.ANALYSIS,
            description="Perform domain-specific research and feasibility analysis",
            input_schema={
                "type": "object",
                "properties": {
                    "domain": {"type": "string"},
                    "questions": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["domain"],
            },
            output_schema={
                "type": "object",
                "properties": {
                    "findings": {"type": "array"},
                    "feasibility_score": {"type": "number"},
                    "recommendations": {"type": "array"},
                },
            },
        ),
        Capability(
            name="api_lookup",
            type=CapabilityType.DISCOVERY,
            description="Look up authoritative API and library documentation",
            input_schema={
                "type": "object",
                "properties": {
                    "topic": {"type": "string"},
                    "sources": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["topic"],
            },
            output_schema={
                "type": "object",
                "properties": {
                    "summary": {"type": "string"},
                    "sources": {"type": "array"},
                    "fit_assessment": {"type": "string"},
                },
            },
        ),
        Capability(
            name="lateral_thinking",
            type=CapabilityType.ANALYSIS,
            description="Generate alternative approaches and creative solutions",
            input_schema={
                "type": "object",
                "properties": {
                    "problem": {"type": "string"},
                    "constraints": {"type": "array"},
                },
                "required": ["problem"],
            },
            output_schema={
                "type": "object",
                "properties": {
                    "alternatives": {"type": "array"},
                    "rationale": {"type": "string"},
                },
            },
        ),
        Capability(
            name="ui_design",
            type=CapabilityType.GENERATION,
            description="Design UI/UX specifications and component structures",
            input_schema={
                "type": "object",
                "properties": {
                    "requirements": {"type": "string"},
                    "framework": {"type": "string"},
                    "accessibility_level": {
                        "type": "string",
                        "enum": ["wcag2.2-aa", "wcag2.2-aaa"],
                        "description": "Required accessibility target for generated UI specifications.",
                    },
                },
                "required": ["requirements", "accessibility_level"],
            },
            output_schema={
                "type": "object",
                "properties": {
                    "ui_spec": {"type": "object"},
                    "components": {"type": "array"},
                    "design_tokens": {"type": "object"},
                    "accessibility_notes": {"type": "array", "items": {"type": "string"}},
                    "keyboard_interactions": {"type": "array", "items": {"type": "string"}},
                    "aria_contracts": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["ui_spec", "components", "accessibility_notes", "keyboard_interactions", "aria_contracts"],
            },
        ),
        Capability(
            name="database_schema_design",
            type=CapabilityType.GENERATION,
            description="Design database schemas, indexes, and data models",
            input_schema={
                "type": "object",
                "properties": {
                    "domain": {"type": "string"},
                    "entities": {"type": "array"},
                    "db_type": {"type": "string"},
                },
                "required": ["domain"],
            },
            output_schema={
                "type": "object",
                "properties": {
                    "schema": {"type": "object"},
                    "migrations": {"type": "array"},
                },
            },
        ),
        Capability(
            name="devops_pipeline_design",
            type=CapabilityType.GENERATION,
            description="Design CI/CD pipelines, infrastructure, and deployment strategies",
            input_schema={
                "type": "object",
                "properties": {
                    "project_type": {"type": "string"},
                    "target_platform": {"type": "string"},
                },
                "required": ["project_type"],
            },
            output_schema={
                "type": "object",
                "properties": {
                    "pipeline_config": {"type": "object"},
                    "steps": {"type": "array"},
                },
            },
        ),
        Capability(
            name="git_workflow_analysis",
            type=CapabilityType.ANALYSIS,
            description="Analyse git history, branches, and recommend workflow improvements",
            input_schema={
                "type": "object",
                "properties": {
                    "repo_path": {"type": "string"},
                    "focus": {"type": "string"},
                },
                "required": ["repo_path"],
            },
            output_schema={
                "type": "object",
                "properties": {
                    "analysis": {"type": "string"},
                    "recommendations": {"type": "array"},
                },
            },
        ),
        Capability(
            name="architecture_decision",
            type=CapabilityType.ANALYSIS,
            description="Evaluate architectural options and produce a decision record",
            input_schema={
                "type": "object",
                "properties": {
                    "options": {"type": "array"},
                    "constraints": {"type": "array"},
                    "goals": {"type": "array"},
                },
                "required": ["options"],
            },
            output_schema={
                "type": "object",
                "properties": {
                    "decision": {"type": "string"},
                    "rationale": {"type": "string"},
                    "trade_offs": {"type": "array"},
                },
            },
        ),
        Capability(
            name="risk_assessment",
            type=CapabilityType.ANALYSIS,
            description="Identify and score risks for a proposed plan or design",
            input_schema={
                "type": "object",
                "properties": {
                    "proposal": {"type": "string"},
                    "context": {"type": "object"},
                },
                "required": ["proposal"],
            },
            output_schema={
                "type": "object",
                "properties": {
                    "risks": {"type": "array"},
                    "severity_scores": {"type": "object"},
                    "mitigations": {"type": "array"},
                },
            },
        ),
        Capability(
            name="ontological_analysis",
            type=CapabilityType.ANALYSIS,
            description="Deep conceptual analysis of problem domain and relationships",
            input_schema={
                "type": "object",
                "properties": {
                    "concept": {"type": "string"},
                    "domain": {"type": "string"},
                },
                "required": ["concept"],
            },
            output_schema={
                "type": "object",
                "properties": {
                    "analysis": {"type": "string"},
                    "related_concepts": {"type": "array"},
                },
            },
        ),
        Capability(
            name="contrarian_review",
            type=CapabilityType.VERIFICATION,
            description="Challenge assumptions and surface blind spots in a proposal",
            input_schema={
                "type": "object",
                "properties": {
                    "proposal": {"type": "string"},
                    "assumptions": {"type": "array"},
                },
                "required": ["proposal"],
            },
            output_schema={
                "type": "object",
                "properties": {
                    "challenges": {"type": "array"},
                    "blind_spots": {"type": "array"},
                    "verdict": {"type": "string"},
                },
            },
        ),
        Capability(
            name="documentation_generation",
            type=CapabilityType.DOCUMENTATION,
            description="Generate technical documentation, README, and API docs",
            input_schema={
                "type": "object",
                "properties": {
                    "artifacts": {"type": "array"},
                    "doc_type": {"type": "string", "enum": ["readme", "api", "guide", "changelog"]},
                },
                "required": ["artifacts"],
            },
            output_schema={
                "type": "object",
                "properties": {
                    "document": {"type": "string"},
                    "format": {"type": "string"},
                },
            },
        ),
        Capability(
            name="creative_writing",
            type=CapabilityType.SYNTHESIS,
            description="Produce creative or narrative content",
            input_schema={
                "type": "object",
                "properties": {
                    "prompt": {"type": "string"},
                    "style": {"type": "string"},
                    "length": {"type": "integer"},
                },
                "required": ["prompt"],
            },
            output_schema={
                "type": "object",
                "properties": {
                    "content": {"type": "string"},
                },
            },
        ),
        Capability(
            name="cost_analysis",
            type=CapabilityType.OPTIMIZATION,
            description="Analyse and estimate costs for a proposed solution",
            input_schema={
                "type": "object",
                "properties": {
                    "plan": {"type": "object"},
                    "budget_constraints": {"type": "object"},
                },
                "required": ["plan"],
            },
            output_schema={
                "type": "object",
                "properties": {
                    "estimated_cost": {"type": "number"},
                    "cost_breakdown": {"type": "object"},
                    "optimisation_suggestions": {"type": "array"},
                },
            },
        ),
        Capability(
            name="experiment_management",
            type=CapabilityType.ANALYSIS,
            description="Design and track experiments and A/B tests",
            input_schema={
                "type": "object",
                "properties": {
                    "hypothesis": {"type": "string"},
                    "metrics": {"type": "array"},
                },
                "required": ["hypothesis"],
            },
            output_schema={
                "type": "object",
                "properties": {
                    "experiment_plan": {"type": "object"},
                    "success_criteria": {"type": "array"},
                },
            },
        ),
        Capability(
            name="error_recovery",
            type=CapabilityType.ANALYSIS,
            description="Diagnose failures and propose recovery strategies",
            input_schema={
                "type": "object",
                "properties": {
                    "error": {"type": "string"},
                    "context": {"type": "object"},
                },
                "required": ["error"],
            },
            output_schema={
                "type": "object",
                "properties": {
                    "root_cause": {"type": "string"},
                    "recovery_steps": {"type": "array"},
                },
            },
        ),
        Capability(
            name="synthesis",
            type=CapabilityType.SYNTHESIS,
            description="Synthesise findings from multiple agents into a coherent output",
            input_schema={
                "type": "object",
                "properties": {
                    "inputs": {"type": "array"},
                    "synthesis_goal": {"type": "string"},
                },
                "required": ["inputs"],
            },
            output_schema={
                "type": "object",
                "properties": {
                    "synthesis": {"type": "string"},
                    "key_points": {"type": "array"},
                },
            },
        ),
        Capability(
            name="improvement_suggestions",
            type=CapabilityType.OPTIMIZATION,
            description="Identify and propose improvements to code, processes, or architecture",
            input_schema={
                "type": "object",
                "properties": {
                    "target": {"type": "string"},
                    "metrics": {"type": "array"},
                },
                "required": ["target"],
            },
            output_schema={
                "type": "object",
                "properties": {
                    "suggestions": {"type": "array"},
                    "priority_order": {"type": "array"},
                },
            },
        ),
        Capability(
            name="monitoring",
            type=CapabilityType.ANALYSIS,
            description="Set up and interpret monitoring, alerts, and observability",
            input_schema={
                "type": "object",
                "properties": {
                    "service": {"type": "string"},
                    "slo_targets": {"type": "object"},
                },
                "required": ["service"],
            },
            output_schema={
                "type": "object",
                "properties": {
                    "dashboard_config": {"type": "object"},
                    "alert_rules": {"type": "array"},
                },
            },
        ),
        Capability(
            name="reporting",
            type=CapabilityType.DOCUMENTATION,
            description="Generate progress reports and executive summaries",
            input_schema={
                "type": "object",
                "properties": {
                    "data": {"type": "object"},
                    "report_type": {"type": "string"},
                    "audience": {"type": "string"},
                },
                "required": ["data"],
            },
            output_schema={
                "type": "object",
                "properties": {
                    "report": {"type": "string"},
                    "highlights": {"type": "array"},
                },
            },
        ),
        Capability(
            name="image_generation",
            type=CapabilityType.GENERATION,
            description="Generate images from text prompts",
            input_schema={
                "type": "object",
                "properties": {
                    "prompt": {"type": "string"},
                    "style": {"type": "string"},
                    "dimensions": {"type": "object"},
                    "alt_text": {"type": "string"},
                },
                "required": ["prompt", "alt_text"],
            },
            output_schema={
                "type": "object",
                "properties": {
                    "file_path": {"type": "string"},
                    "format": {"type": "string"},
                    "alt_text": {"type": "string"},
                },
                "required": ["file_path", "format", "alt_text"],
            },
        ),
    ],
    required_context=["codebase_path", "search_tools", "problem_statement", "project_context"],
    error_codes={
        "NOT_FOUND": "No results found for the given query",
        "SOURCE_UNAVAILABLE": "Required documentation source is unreachable",
        "INSUFFICIENT_CONTEXT": "Not enough context to make an architectural decision",
        "CONFLICTING_CONSTRAINTS": "Constraints are mutually exclusive",
        "NO_INPUT": "No input data provided",
        "SYNTHESIS_CONFLICT": "Conflicting inputs cannot be synthesised without disambiguation",
    },
)
