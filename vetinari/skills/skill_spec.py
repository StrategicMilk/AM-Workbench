"""Skill Specification Contract.

=============================
Defines the ``SkillSpec`` dataclass — the canonical contract for all Vetinari
skills, parallel to ``AgentSpec`` for agents.

Every skill MUST have a corresponding ``SkillSpec`` entry in the programmatic
registry (``skill_registry.py``).

The spec enforces three pillars:
  1. **Standards** — coding/output quality rules the skill must uphold
  2. **Guidelines** — best-practice advice for skill consumers
  3. **Constraints** — hard limits on resources, scope, and safety
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from vetinari.errors import FailClosedError, require_mapping
from vetinari.types import TrustTier as _CoreTrustTier
from vetinari.utils.serialization import dataclass_to_dict

# ---------------------------------------------------------------------------
# Supporting types
# ---------------------------------------------------------------------------


class _SkillTrustTier:
    VERIFIED = _CoreTrustTier.T2_VERIFIED
    CORE = _CoreTrustTier.T4_CORE


TrustTier = _SkillTrustTier


@dataclass(frozen=True, slots=True)
class SkillStandard:
    """A single quality standard that a skill must enforce.

    Standards are *mandatory* — a skill execution that violates any standard
    is considered non-conformant and should be flagged by the verifier.
    """

    id: str  # e.g. "STD-BLD-001"
    category: str  # "code_quality", "security", "testing", "documentation", "performance"
    rule: str  # Human-readable rule statement
    severity: str = "error"  # "error" (must fix) | "warning" (should fix)
    check_hint: str = ""  # Machine-actionable hint for automated validation

    def __repr__(self) -> str:
        """Show key identifying fields for debugging."""
        return f"SkillStandard(id={self.id!r}, category={self.category!r}, severity={self.severity!r})"

    def to_dict(self) -> dict[str, Any]:
        """Convert to a plain dictionary for JSON export."""
        return dataclass_to_dict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SkillStandard:
        """Deserialize a SkillStandard from a dictionary.

        Args:
            data: Dictionary with standard fields as produced by ``to_dict()``.

        Returns:
            A new SkillStandard instance.

        Raises:
            FailClosedError: If ``data`` is not a mapping or omits required
                standard fields.
        """
        data = require_mapping(data, "skill_standard.row", recovery="provide a JSON object for each standard")
        if not data.get("id") or not data.get("category") or not data.get("rule"):
            raise FailClosedError(
                "skill_standard.required_fields",
                "standard rows require id, category, and rule",
                recovery="repair the skill registry row before loading",
            )
        return cls(
            id=data.get("id", ""),
            category=data.get("category", ""),
            rule=data.get("rule", ""),
            severity=data.get("severity", "error"),
            check_hint=data.get("check_hint", ""),
        )


@dataclass(frozen=True, slots=True)
class SkillGuideline:
    """A best-practice guideline for skill consumers.

    Guidelines are *advisory* — they improve quality but are not enforced
    as hard failures.
    """

    id: str  # e.g. "GDL-BLD-001"
    category: str  # "usage", "integration", "performance", "output_format"
    recommendation: str  # What to do
    rationale: str = ""  # Why it matters

    def __repr__(self) -> str:
        """Show key identifying fields for debugging."""
        return f"SkillGuideline(id={self.id!r}, category={self.category!r})"

    def to_dict(self) -> dict[str, Any]:
        """Convert to a plain dictionary for JSON export."""
        return dataclass_to_dict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SkillGuideline:
        """Deserialize a SkillGuideline from a dictionary.

        Args:
            data: Dictionary with guideline fields as produced by ``to_dict()``.

        Returns:
            A new SkillGuideline instance.

        Raises:
            FailClosedError: If ``data`` is not a mapping or omits required
                guideline fields.
        """
        data = require_mapping(data, "skill_guideline.row", recovery="provide a JSON object for each guideline")
        if not data.get("id") or not data.get("category") or not data.get("recommendation"):
            raise FailClosedError(
                "skill_guideline.required_fields",
                "guideline rows require id, category, and recommendation",
                recovery="repair the skill registry row before loading",
            )
        return cls(
            id=data.get("id", ""),
            category=data.get("category", ""),
            recommendation=data.get("recommendation", ""),
            rationale=data.get("rationale", ""),
        )


@dataclass(frozen=True, slots=True)
class SkillConstraint:
    """A hard constraint on skill execution.

    Constraints are *immutable limits* — they cannot be overridden by the
    caller and are enforced at the framework level.
    """

    id: str  # e.g. "CON-BLD-001"
    category: str  # "resource", "scope", "safety", "concurrency"
    description: str  # What is constrained
    limit: str  # The actual limit value (human-readable)
    enforcement: str = "hard"  # "hard" (runtime enforced) | "soft" (logged warning)

    def __repr__(self) -> str:
        """Show key identifying fields for debugging."""
        return f"SkillConstraint(id={self.id!r}, category={self.category!r}, enforcement={self.enforcement!r})"

    def to_dict(self) -> dict[str, Any]:
        """Convert to a plain dictionary for JSON export."""
        return dataclass_to_dict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SkillConstraint:
        """Deserialize a SkillConstraint from a dictionary.

        Args:
            data: Dictionary with constraint fields as produced by ``to_dict()``.

        Returns:
            A new SkillConstraint instance.

        Raises:
            FailClosedError: If ``data`` is not a mapping or omits required
                constraint fields.
        """
        data = require_mapping(data, "skill_constraint.row", recovery="provide a JSON object for each constraint")
        if not data.get("id") or not data.get("category") or not data.get("description") or not data.get("limit"):
            raise FailClosedError(
                "skill_constraint.required_fields",
                "constraint rows require id, category, description, and limit",
                recovery="repair the skill registry row before loading",
            )
        return cls(
            id=data.get("id", ""),
            category=data.get("category", ""),
            description=data.get("description", ""),
            limit=data.get("limit", ""),
            enforcement=data.get("enforcement", "hard"),
        )


# ---------------------------------------------------------------------------
# Main SkillSpec
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SkillSpec:
    """Specification contract for all Vetinari skills — parallel to AgentSpec for agents.

    Three pillars of quality governance:
      - ``standards``: Mandatory rules — violations are errors.
      - ``guidelines``: Best-practice advice — violations are warnings.
      - ``constraints``: Hard limits — enforced by the runtime.
    """

    # ── Identity ──────────────────────────────────────────────────────────
    skill_id: str  # Unique kebab-case identifier
    name: str = ""  # Human-readable name
    description: str = ""  # One-line purpose
    version: str = "1.0.0"  # Semantic version
    agent_type: str | None = None  # Owning agent type (AgentType.value)
    modes: list[str] = field(default_factory=list)  # Compatible agent modes

    # ── Capability declaration ────────────────────────────────────────────
    capabilities: list[str] = field(default_factory=list)
    input_schema: dict[str, Any] = field(default_factory=dict)  # JSON schema for inputs
    output_schema: dict[str, Any] = field(default_factory=dict)  # JSON schema for outputs

    # ── Resource constraints (aligned with Phase 8 ResourceConstraint) ───
    max_tokens: int = 4096
    max_retries: int = 3
    timeout_seconds: int = 120
    max_cost_usd: float = 0.50
    requires_tools: list[str] = field(default_factory=list)  # Tool dependencies

    # ── Quality standards ─────────────────────────────────────────────────
    min_verification_score: float = 0.5
    require_schema_validation: bool = True
    forbidden_patterns: list[str] = field(default_factory=list)

    # ── Standards / Guidelines / Constraints (the three pillars) ──────────
    standards: list[SkillStandard] = field(default_factory=list)
    guidelines: list[SkillGuideline] = field(default_factory=list)
    constraints: list[SkillConstraint] = field(default_factory=list)

    # ── Trust and Loading ─────────────────────────────────────────────────
    trust_tier: str = "t4_core"  # TrustTier value; built-in skills are T4
    loading_level: int = 1  # 1=name+desc, 2=full spec, 3=resources
    output_validators: tuple = ()  # Callables for postcondition checks

    # ── Metadata ──────────────────────────────────────────────────────────
    author: str = "vetinari"
    tags: list[str] = field(default_factory=list)
    enabled: bool = True
    deprecated: bool = False
    deprecated_by: str = ""  # Replacement skill_id if deprecated

    def __repr__(self) -> str:
        """Show key identifying fields for debugging."""
        return f"SkillSpec(skill_id={self.skill_id!r}, version={self.version!r}, enabled={self.enabled!r})"

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def validate(self) -> list[str]:
        """Validate this spec meets contract requirements.

        Checks for required fields, positive resource limits, valid score ranges,
        and unique IDs across standards, guidelines, and constraints.

        Returns:
            List of error message strings; empty when the spec is valid.
        """
        errors = []
        if not self.skill_id:
            errors.append("skill_id is required")
        if not self.name:
            errors.append("name is required")
        if not self.description:
            errors.append("description is required")
        if not self.modes:
            errors.append(f"Skill '{self.skill_id}' has no modes defined")
        if not self.input_schema:
            errors.append(f"Skill '{self.skill_id}' missing input_schema")
        if not self.output_schema:
            errors.append(f"Skill '{self.skill_id}' missing output_schema")
        if self.max_tokens < 1:
            errors.append(f"Skill '{self.skill_id}' max_tokens must be positive")
        if self.timeout_seconds < 1:
            errors.append(f"Skill '{self.skill_id}' timeout_seconds must be positive")
        if not 0.0 <= self.min_verification_score <= 1.0:
            errors.append(f"Skill '{self.skill_id}' min_verification_score must be 0-1")
        # Validate standards have unique IDs
        std_ids = [s.id for s in self.standards]
        if len(std_ids) != len(set(std_ids)):
            errors.append(f"Skill '{self.skill_id}' has duplicate standard IDs")
        # Validate guidelines have unique IDs
        gdl_ids = [g.id for g in self.guidelines]
        if len(gdl_ids) != len(set(gdl_ids)):
            errors.append(f"Skill '{self.skill_id}' has duplicate guideline IDs")
        # Validate constraints have unique IDs
        con_ids = [c.id for c in self.constraints]
        if len(con_ids) != len(set(con_ids)):
            errors.append(f"Skill '{self.skill_id}' has duplicate constraint IDs")
        return errors

    def get_standards_by_category(self, category: str) -> list[SkillStandard]:
        """Return all standards in a given category."""
        return [s for s in self.standards if s.category == category]

    def get_error_standards(self) -> list[SkillStandard]:
        """Return all mandatory (severity=error) standards."""
        return [s for s in self.standards if s.severity == "error"]

    def get_constraints_by_category(self, category: str) -> list[SkillConstraint]:
        """Return all constraints in a given category."""
        return [c for c in self.constraints if c.category == category]

    def get_hard_constraints(self) -> list[SkillConstraint]:
        """Return all hard-enforced constraints."""
        return [c for c in self.constraints if c.enforcement == "hard"]

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dictionary for JSON export."""
        return {
            "id": self.skill_id,  # Backward compat with disk-based registry
            "skill_id": self.skill_id,
            "name": self.name,
            "description": self.description,
            "version": self.version,
            "agent_type": self.agent_type,
            "modes": self.modes,
            "capabilities": self.capabilities,
            "input_schema": self.input_schema,
            "output_schema": self.output_schema,
            "max_tokens": self.max_tokens,
            "max_retries": self.max_retries,
            "timeout_seconds": self.timeout_seconds,
            "max_cost_usd": self.max_cost_usd,
            "requires_tools": self.requires_tools,
            "min_verification_score": self.min_verification_score,
            "require_schema_validation": self.require_schema_validation,
            "forbidden_patterns": self.forbidden_patterns,
            "standards": [s.to_dict() for s in self.standards],
            "guidelines": [g.to_dict() for g in self.guidelines],
            "constraints": [c.to_dict() for c in self.constraints],
            "trust_tier": self.trust_tier,
            "loading_level": self.loading_level,
            "output_validators": self.output_validators,
            "author": self.author,
            "tags": self.tags,
            "enabled": self.enabled,
            "deprecated": self.deprecated,
            "deprecated_by": self.deprecated_by,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SkillSpec:
        """Deserialize a skill specification from a dictionary.

        Args:
            data: Dictionary with fields as produced by ``to_dict()``.

        Returns:
            A new SkillSpec instance.

        Raises:
            FailClosedError: If ``data`` is not a mapping or omits required
                skill-spec fields.
        """
        data = require_mapping(data, "skill_spec.row", recovery="provide a JSON object for each skill spec")
        missing = [
            key
            for key in ("skill_id", "name", "description", "modes", "input_schema", "output_schema")
            if not data.get(key)
        ]
        if missing:
            raise FailClosedError(
                "skill_spec.required_fields",
                f"skill spec missing required fields: {', '.join(missing)}",
                recovery="repair the skill registry row before loading",
            )
        return cls(
            skill_id=data.get("skill_id", ""),
            name=data.get("name", ""),
            description=data.get("description", ""),
            version=data.get("version", "1.0.0"),
            agent_type=data.get("agent_type"),
            modes=data.get("modes", []),
            capabilities=data.get("capabilities", []),
            input_schema=data.get("input_schema", {}),
            output_schema=data.get("output_schema", {}),
            max_tokens=data.get("max_tokens", 4096),
            max_retries=data.get("max_retries", 3),
            timeout_seconds=data.get("timeout_seconds", 120),
            max_cost_usd=data.get("max_cost_usd", 0.50),
            requires_tools=data.get("requires_tools", []),
            min_verification_score=data.get("min_verification_score", 0.5),
            require_schema_validation=data.get("require_schema_validation", True),
            forbidden_patterns=data.get("forbidden_patterns", []),
            standards=[SkillStandard.from_dict(s) for s in data.get("standards", [])],
            guidelines=[SkillGuideline.from_dict(g) for g in data.get("guidelines", [])],
            constraints=[SkillConstraint.from_dict(c) for c in data.get("constraints", [])],
            trust_tier=data.get("trust_tier", "t4_core"),
            loading_level=data.get("loading_level", 1),
            output_validators=data.get("output_validators", ()),
            author=data.get("author", "vetinari"),
            tags=data.get("tags", []),
            enabled=data.get("enabled", True),
            deprecated=data.get("deprecated", False),
            deprecated_by=data.get("deprecated_by", ""),
        )

    def model_dump(self, *, by_alias: bool = False) -> dict[str, Any]:
        """Pydantic-compatible serialization alias used by runtime contract tests.

        Returns:
            A dictionary representation of this skill specification.
        """
        data = self.to_dict()
        if not by_alias:
            data.pop("id", None)
        return data
