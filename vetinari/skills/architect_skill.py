"""Architect Skill Tool — internal component of WorkerSkillTool.

Architecture-focused modes providing design analysis and ADR production.
This module is an *internal component* of WorkerSkillTool (the primary Worker
skill tool in the 3-agent factory pipeline, ADR-0061). WorkerSkillTool
delegates architecture mode group calls here.

Direct usage is supported for backwards compatibility but all new code
should go through ``WorkerSkillTool(mode="architecture", ...)``.

Modes:
  - ui_design: Component-based UI architecture
  - database: Normalized relational schema design
  - devops: CI/CD with deployment strategies
  - git_workflow: Trunk-based development strategy
  - system_design: Modular monolith architecture
  - api_design: RESTful API with versioning

Standards enforced:
  - STD-WRK-005: Every design states pattern and rationale
  - STD-WRK-006: Components define boundaries
  - STD-WRK-007: DB schemas include indexes, constraints, migration
  - STD-WRK-008: API designs include auth, authz, rate limiting
  - STD-WRK-009: DevOps pipelines include rollback and health checks
  - STD-WRK-010: Designs list alternatives considered
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from vetinari.execution_context import ToolPermission
from vetinari.tool_interface import (
    Tool,
    ToolCategory,
    ToolMetadata,
    ToolParameter,
    ToolResult,
)
from vetinari.types import (
    ExecutionMode,
    ThinkingMode,  # canonical enum from types.py
)
from vetinari.utils.serialization import dataclass_to_dict

logger = logging.getLogger(__name__)


class ArchitectMode(str, Enum):
    """Modes of the unified architect skill."""

    UI_DESIGN = "ui_design"
    DATABASE = "database"
    DEVOPS = "devops"
    GIT_WORKFLOW = "git_workflow"
    SYSTEM_DESIGN = "system_design"
    API_DESIGN = "api_design"


@dataclass
class ArchitectRequest:
    """Request structure for architect operations."""

    mode: ArchitectMode
    design_request: str
    domain: str | None = None
    context: str | None = None
    thinking_mode: ThinkingMode = ThinkingMode.MEDIUM
    constraints: list[str] = field(default_factory=list)

    def __repr__(self) -> str:
        """Show key identifying fields for debugging."""
        return f"ArchitectRequest(mode={self.mode!r}, thinking_mode={self.thinking_mode!r})"

    def to_dict(self) -> dict[str, Any]:
        """Convert to a plain dictionary for JSON output; enum fields become their string values."""
        return dataclass_to_dict(self)


@dataclass
class ArchitectComponent:
    """A component in an architecture design."""

    name: str
    responsibility: str
    interfaces: list[str] = field(default_factory=list)
    dependencies: list[str] = field(default_factory=list)

    def __repr__(self) -> str:
        """Show key identifying fields for debugging."""
        return f"ArchitectComponent(name={self.name!r}, interfaces={len(self.interfaces)!r})"

    def to_dict(self) -> dict[str, Any]:
        """Convert to a plain dictionary for JSON output."""
        return dataclass_to_dict(self)


@dataclass
class ArchitectResult:
    """Result of an architect operation."""

    success: bool
    summary: str | None = None
    architecture_pattern: str | None = None
    rationale: str | None = None
    components: list[ArchitectComponent] = field(default_factory=list)
    alternatives_considered: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    migration_steps: list[str] = field(default_factory=list)

    def __repr__(self) -> str:
        """Show key identifying fields for debugging."""
        return (
            f"ArchitectResult(success={self.success!r},"
            f" architecture_pattern={self.architecture_pattern!r},"
            f" components={len(self.components)!r})"
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize this ArchitectResult to a plain dictionary suitable for JSON output.

        Returns:
            Dictionary containing the design summary, components, alternatives
            considered, and migration steps.
        """
        return {
            "success": self.success,
            "design": {
                "summary": self.summary,
                "architecture_pattern": self.architecture_pattern,
                "rationale": self.rationale,
                "alternatives_considered": self.alternatives_considered,
            },
            "components": [c.to_dict() for c in self.components],
            "warnings": self.warnings,
            "migration_plan": {"steps": self.migration_steps},
        }


class ArchitectSkillTool(Tool):
    """Unified tool for the ARCHITECT consolidated agent.

    Replaces: UIPlannerSkillTool, DataEngineerSkill, DevOpsSkill, VersionControlSkill.

    Provides design and architecture capabilities through a standardized
    Tool interface with clear standards enforcement.

    Permissions:
    - FILE_READ: Read existing code/schemas for analysis
    - MODEL_INFERENCE: Use LLM for design generation

    Constraints:
    - CON-ARC-001: Read-only — does not modify code or infrastructure
    - CON-ARC-002: Max 20 components per design
    - CON-ARC-003: DB changes flag potential data loss
    """

    def __init__(self):
        metadata = ToolMetadata(
            name="architect",
            description=(
                "System architecture, UI/UX design, database schema, DevOps pipelines, "
                "git workflow, and API design. Use for any design or architecture decision."
            ),
            category=ToolCategory.SEARCH_ANALYSIS,
            version="1.1.0",
            author="Vetinari",
            parameters=[
                ToolParameter(
                    name="mode",
                    type=str,
                    description="Architecture mode to use",
                    required=True,
                    allowed_values=[m.value for m in ArchitectMode],
                ),
                ToolParameter(
                    name="design_request",
                    type=str,
                    description="What to design or architect",
                    required=True,
                ),
                ToolParameter(
                    name="domain",
                    type=str,
                    description="Domain context (web, mobile, backend, infra)",
                    required=False,
                ),
                ToolParameter(
                    name="context",
                    type=str,
                    description="Existing architecture or code context",
                    required=False,
                ),
                ToolParameter(
                    name="thinking_mode",
                    type=str,
                    description="Design depth (none/low/medium/high/xhigh/max)",
                    required=False,
                    default="medium",
                    allowed_values=[m.value for m in ThinkingMode],
                ),
                ToolParameter(
                    name="constraints",
                    type=list,
                    description="Technical constraints to respect",
                    required=False,
                ),
            ],
            required_permissions=[
                ToolPermission.FILE_READ,
                ToolPermission.MODEL_INFERENCE,
            ],
            allowed_modes=[ExecutionMode.EXECUTION, ExecutionMode.PLANNING],
            tags=[
                "architecture",
                "design",
                "infrastructure",
                "ui",
                "database",
                "devops",
                "api",
            ],
        )
        super().__init__(metadata)

    def execute(self, **kwargs) -> ToolResult:
        """Execute an architect operation.

        Returns:
            The ToolResult result.
        """
        try:
            mode_str = kwargs.get("mode")
            design_request = kwargs.get("design_request")
            domain = kwargs.get("domain")
            context = kwargs.get("context")
            thinking_mode_str = kwargs.get("thinking_mode", "medium")
            constraints = kwargs.get("constraints", [])

            if not design_request:
                return ToolResult(
                    success=False,
                    output=None,
                    error="design_request parameter is required",
                )

            try:
                mode = ArchitectMode(mode_str)
            except ValueError:
                logger.warning("Invalid ArchitectMode %r in tool call — returning error to caller", mode_str)
                return ToolResult(
                    success=False,
                    output=None,
                    error=f"Invalid mode: {mode_str}. Valid: {[m.value for m in ArchitectMode]}",
                )

            try:
                thinking_mode = ThinkingMode(thinking_mode_str)
            except ValueError:
                logger.warning(
                    "Invalid ThinkingMode %r in architect tool call — returning error to caller",
                    thinking_mode_str,
                )
                return ToolResult(
                    success=False,
                    output=None,
                    error=f"Invalid thinking_mode: {thinking_mode_str}",
                )

            request = ArchitectRequest(
                mode=mode,
                design_request=design_request,
                domain=domain,
                context=context,
                thinking_mode=thinking_mode,
                constraints=constraints,
            )

            result = self._apply_thinking_depth(request, self._execute_mode(request))

            return ToolResult(
                success=result.success,
                output=result.to_dict(),
                error=None if result.success else "Architecture design failed",
                metadata={
                    "mode": mode.value,
                    "thinking_mode": thinking_mode.value,
                    "components_count": len(result.components),
                },
            )

        except Exception as e:
            logger.error("Architect tool execution failed: %s", e, exc_info=True)
            return ToolResult(success=False, output=None, error=str(e))

    def _execute_mode(self, request: ArchitectRequest) -> ArchitectResult:
        """Route to the appropriate design mode."""
        mode = request.mode

        if mode == ArchitectMode.UI_DESIGN:
            return self._design_ui(request)
        if mode == ArchitectMode.DATABASE:
            return self._design_database(request)
        if mode == ArchitectMode.DEVOPS:
            return self._design_devops(request)
        if mode == ArchitectMode.GIT_WORKFLOW:
            return self._design_git_workflow(request)
        if mode == ArchitectMode.SYSTEM_DESIGN:
            return self._design_system(request)
        if mode == ArchitectMode.API_DESIGN:
            return self._design_api(request)
        return ArchitectResult(success=False, summary=f"Unknown mode: {mode.value}")

    @staticmethod
    def _apply_thinking_depth(request: ArchitectRequest, result: ArchitectResult) -> ArchitectResult:
        """Make thinking budget materially affect the produced design."""
        if request.thinking_mode not in (ThinkingMode.HIGH, ThinkingMode.XHIGH):
            return result

        result.warnings = [
            *result.warnings,
            "Validate failure isolation and interface contracts before implementation.",
        ]
        if not result.migration_steps:
            result.migration_steps = [
                "1. Capture the highest-risk tradeoffs in an ADR before implementation.",
                "2. Prototype the boundary with the most external dependencies.",
                "3. Add contract and rollback checks before rollout.",
            ]

        if request.mode == ArchitectMode.SYSTEM_DESIGN:
            result.components.extend((
                ArchitectComponent(
                    name="Security Boundary",
                    responsibility="Centralize authn, authz, secret handling, and audit events",
                    interfaces=["policy enforcement", "audit log", "secret provider"],
                    dependencies=["API Layer", "Infrastructure Layer"],
                ),
                ArchitectComponent(
                    name="Observability Spine",
                    responsibility="Expose traces, structured logs, and service health signals",
                    interfaces=["metrics", "traces", "structured logs", "health endpoints"],
                    dependencies=["API Layer", "Domain Layer", "Infrastructure Layer"],
                ),
            ))
            result.alternatives_considered.append(
                "Layered modular monolith with service extraction seams later — viable when team size or traffic grows",
            )

        if request.thinking_mode == ThinkingMode.XHIGH:
            result.warnings.append("Model rollback and data migration failure paths need explicit rehearsal.")
            result.migration_steps.extend((
                "4. Run a failure-injection exercise for the shared boundaries.",
                "5. Gate rollout on observable rollback signals and operator runbooks.",
            ))

        return result

    @staticmethod
    def _design_ui(request: ArchitectRequest) -> ArchitectResult:
        """Design UI/UX architecture."""
        logger.info("Designing UI: %s", request.design_request)
        return ArchitectResult(
            success=True,
            summary=f"UI Design: {request.design_request}",
            architecture_pattern="Component-based UI Architecture",
            rationale="Component-based architecture enables reusability, testability, and consistent design language",
            components=[
                ArchitectComponent(
                    name="Layout System",
                    responsibility="Responsive grid layout and spacing",
                    interfaces=["Grid", "Stack", "Container"],
                ),
                ArchitectComponent(
                    name="Design Tokens",
                    responsibility="Colors, typography, spacing, and breakpoint definitions",
                    interfaces=["ThemeProvider", "useTheme"],
                ),
            ],
            alternatives_considered=[
                "Utility-first CSS (Tailwind) — rejected for maintainability at scale",
                "CSS-in-JS (styled-components) — considered but adds runtime overhead",
            ],
        )

    @staticmethod
    def _design_database(request: ArchitectRequest) -> ArchitectResult:
        """Design database schema."""
        logger.info("Designing database: %s", request.design_request)
        return ArchitectResult(
            success=True,
            summary=f"Database Design: {request.design_request}",
            architecture_pattern="Normalized relational schema with strategic denormalization",
            rationale="Normalization ensures data integrity; targeted denormalization optimizes read-heavy paths",
            components=[
                ArchitectComponent(
                    name="Core Tables",
                    responsibility="Primary entities with referential integrity",
                    interfaces=["Primary keys", "Foreign keys", "Unique constraints"],
                ),
                ArchitectComponent(
                    name="Index Strategy",
                    responsibility="Query optimization via targeted indexes",
                    interfaces=["B-tree indexes", "Composite indexes", "Partial indexes"],
                ),
            ],
            alternatives_considered=[
                "Document store (MongoDB) — rejected due to need for strong consistency",
                "Graph database — considered for relationship-heavy queries but adds operational complexity",
            ],
            warnings=["Review migration path for potential data loss on column type changes"],
        )

    @staticmethod
    def _design_devops(request: ArchitectRequest) -> ArchitectResult:
        """Design DevOps pipeline."""
        logger.info("Designing DevOps: %s", request.design_request)
        return ArchitectResult(
            success=True,
            summary=f"DevOps Pipeline: {request.design_request}",
            architecture_pattern="CI/CD with blue-green deployment",
            rationale="Blue-green deployment enables zero-downtime releases with instant rollback capability",
            components=[
                ArchitectComponent(
                    name="CI Pipeline",
                    responsibility="Build, test, lint, security scan on every push",
                    interfaces=["build", "test", "lint", "security-scan"],
                ),
                ArchitectComponent(
                    name="CD Pipeline",
                    responsibility="Automated deployment with health checks",
                    interfaces=["deploy", "health-check", "rollback"],
                ),
                ArchitectComponent(
                    name="Monitoring",
                    responsibility="Observability, alerting, and incident response",
                    interfaces=["metrics", "logs", "traces", "alerts"],
                ),
            ],
            alternatives_considered=[
                "Canary deployment — viable but adds complexity for small teams",
                "Rolling deployment — simpler but slower rollback",
            ],
            migration_steps=[
                "1. Set up CI pipeline with automated tests",
                "2. Add security scanning stage",
                "3. Configure staging environment",
                "4. Implement health check endpoints",
                "5. Set up blue-green deployment",
                "6. Configure monitoring and alerts",
            ],
        )

    @staticmethod
    def _design_git_workflow(request: ArchitectRequest) -> ArchitectResult:
        """Design git workflow strategy."""
        logger.info("Designing git workflow: %s", request.design_request)
        return ArchitectResult(
            success=True,
            summary=f"Git Workflow: {request.design_request}",
            architecture_pattern="Trunk-based development with short-lived feature branches",
            rationale="Trunk-based development reduces merge conflicts and enables continuous integration",
            components=[
                ArchitectComponent(
                    name="Branch Strategy",
                    responsibility="Feature branches off main, merged via PR",
                    interfaces=["main", "feature/*", "hotfix/*"],
                ),
                ArchitectComponent(
                    name="PR Workflow",
                    responsibility="Code review, CI checks, and merge strategy",
                    interfaces=["review", "approve", "squash-merge"],
                ),
            ],
            alternatives_considered=[
                "GitFlow — rejected for unnecessary complexity with CI/CD",
                "GitHub Flow — close match but we add release tagging",
            ],
        )

    @staticmethod
    def _design_system(request: ArchitectRequest) -> ArchitectResult:
        """Design overall system architecture."""
        logger.info("Designing system: %s", request.design_request)
        return ArchitectResult(
            success=True,
            summary=f"System Design: {request.design_request}",
            architecture_pattern="Modular monolith with clear bounded contexts",
            rationale="Modular monolith provides service boundary clarity without distributed system complexity",
            components=[
                ArchitectComponent(
                    name="API Layer",
                    responsibility="HTTP/REST interface with request validation",
                    interfaces=["REST endpoints", "middleware", "serialization"],
                    dependencies=["Domain Layer"],
                ),
                ArchitectComponent(
                    name="Domain Layer",
                    responsibility="Business logic and domain models",
                    interfaces=["Services", "Repositories", "Events"],
                    dependencies=["Infrastructure Layer"],
                ),
                ArchitectComponent(
                    name="Infrastructure Layer",
                    responsibility="Database, external APIs, file system",
                    interfaces=["Database adapters", "HTTP clients", "File I/O"],
                ),
            ],
            alternatives_considered=[
                "Microservices — rejected for current scale; adds operational overhead",
                "Serverless — considered for event-driven workloads but complicates local development",
            ],
        )

    @staticmethod
    def _design_api(request: ArchitectRequest) -> ArchitectResult:
        """Design API architecture."""
        logger.info("Designing API: %s", request.design_request)
        return ArchitectResult(
            success=True,
            summary=f"API Design: {request.design_request}",
            architecture_pattern="RESTful API with versioning and HATEOAS links",
            rationale="REST with versioning ensures backward compatibility; HATEOAS enables API discoverability",
            components=[
                ArchitectComponent(
                    name="Authentication",
                    responsibility="JWT-based auth with refresh tokens",
                    interfaces=["POST /auth/login", "POST /auth/refresh", "POST /auth/logout"],
                ),
                ArchitectComponent(
                    name="Authorization",
                    responsibility="Role-based access control (RBAC)",
                    interfaces=["middleware", "permission decorators"],
                ),
                ArchitectComponent(
                    name="Rate Limiting",
                    responsibility="Per-user and per-endpoint rate limits",
                    interfaces=["X-RateLimit-* headers", "429 responses"],
                ),
                ArchitectComponent(
                    name="Versioning",
                    responsibility="URL-based API versioning",
                    interfaces=["/api/v1/*", "/api/v2/*"],
                ),
            ],
            alternatives_considered=[
                "GraphQL — powerful for complex queries but adds schema management overhead",
                "gRPC — excellent performance but limits browser client access",
            ],
        )
