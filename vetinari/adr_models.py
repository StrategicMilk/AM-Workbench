"""Data models for Architecture Decision Records."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, cast

from vetinari.boundary_guards import require_nonempty
from vetinari.utils.serialization import dataclass_to_dict


class ADRStatus(str, Enum):
    """Lifecycle status of an Architecture Decision Record."""

    PROPOSED = "proposed"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    DEPRECATED = "deprecated"
    SUPERSEDED = "superseded"


class ADRCategory(str, Enum):
    """Classification categories for ADRs."""

    ARCHITECTURE = "architecture"
    SECURITY = "security"
    DATA_FLOW = "data_flow"
    API_DESIGN = "api_design"
    AGENT_DESIGN = "agent_design"
    DECOMPOSITION = "decomposition"
    PERFORMANCE = "performance"
    INTEGRATION = "integration"


HIGH_STAKES_CATEGORIES: frozenset[ADRCategory] = frozenset({
    ADRCategory.ARCHITECTURE,
    ADRCategory.SECURITY,
    ADRCategory.DATA_FLOW,
})


@dataclass
class ADR:
    """A single Architecture Decision Record."""

    adr_id: str
    title: str
    category: str
    context: str
    decision: str
    status: str = ADRStatus.PROPOSED.value
    consequences: str = ""
    alternatives_considered: list[dict[str, Any]] = field(default_factory=list)
    related_adrs: list[str] = field(default_factory=list)
    superseded_by: str | None = None
    created_at: str = ""
    updated_at: str = ""
    created_by: str = "system"
    notes: str = ""
    # Machine-readable sunset date (ISO-8601 YYYY-MM-DD).  Required for
    # ADRs that describe API deprecation policy (RFC 8594 Sunset header);
    # optional for everything else.  See
    # vetinari.release.operability_contract.validate_adr_sunset_dates.
    sunset_date: str = ""

    def __repr__(self) -> str:
        return f"ADR(adr_id={self.adr_id!r}, title={self.title!r}, category={self.category!r}, status={self.status!r})"

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-compatible dictionary."""
        return cast(dict[str, Any], dataclass_to_dict(self))

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ADR:
        """Deserialize an ADR from a dictionary.

        Returns:
            Populated ADR instance with defaults for missing fields.
        """
        raw_adr_id = data.get("adr_id")
        if not raw_adr_id and isinstance(data.get("id"), str):
            raw_adr_id = data["id"]
        raw_status = data.get("status", ADRStatus.PROPOSED.value)
        status = raw_status.lower() if isinstance(raw_status, str) else ADRStatus.PROPOSED.value
        return cls(
            adr_id=str(raw_adr_id) if raw_adr_id is not None else "",
            title=data.get("title", ""),
            category=data.get("category", "architecture"),
            context=data.get("context", ""),
            decision=data.get("decision", ""),
            status=status,
            consequences=data.get("consequences", ""),
            alternatives_considered=data.get("alternatives_considered", []),
            related_adrs=data.get("related_adrs", []),
            superseded_by=data.get("superseded_by"),
            created_at=data.get("created_at", ""),
            updated_at=data.get("updated_at", ""),
            created_by=data.get("created_by", "system"),
            notes=data.get("notes", ""),
            sunset_date=data.get("sunset_date", ""),
        )


@dataclass
class ADRProposal:
    """A proposal for an architecture decision with multiple options."""

    question: str
    options: list[dict[str, Any]]
    recommended: int = 0
    rationale: str = ""

    def __repr__(self) -> str:
        return f"ADRProposal(options={len(self.options)}, recommended={self.recommended!r})"


@dataclass
class ADRAcceptance:
    """Validated data required before an ADR proposal can be accepted."""

    selected_option: str
    rationale: str

    def __post_init__(self) -> None:
        self.selected_option = require_nonempty(self.selected_option, field_name="selected_option")
        self.rationale = require_nonempty(self.rationale, field_name="rationale")
