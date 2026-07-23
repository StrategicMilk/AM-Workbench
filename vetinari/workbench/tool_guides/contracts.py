"""Contracts for contextual Workbench tool guide selection."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import date
from enum import Enum
from typing import Any

SCHEMA_VERSION = 1


class ToolGuideError(ValueError):
    """Raised when tool guide catalog state cannot be trusted."""


class ToolGuideStatus(str, Enum):
    """Typed statuses emitted by the tool guide selector."""

    SELECTED = "selected"
    INACTIVE_TOOL = "inactive_tool"
    STALE_GUIDE = "stale_guide"
    FINGERPRINT_MISMATCH = "fingerprint_mismatch"
    BUDGET_EXCEEDED = "over_token_budget"
    BLOCKED = "blocked"


_TOKEN_RE = re.compile(r"\w+|[^\w\s]", re.UNICODE)


def fallback_estimate_tokens(text: str) -> int:
    """Return a bounded lexical fallback when AM Engine is unavailable."""
    return max(1, len(_TOKEN_RE.findall(text)))


def _non_empty_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ToolGuideError(f"{field_name} must be non-empty")
    return value.strip()


def _string_tuple(value: object, field_name: str, *, required: bool = False) -> tuple[str, ...]:
    if value is None:
        rows: tuple[str, ...] = ()
    elif isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ToolGuideError(f"{field_name} must be a list of strings")
    else:
        rows = tuple(str(item).strip() for item in value if str(item).strip())
    if required and not rows:
        raise ToolGuideError(f"{field_name} must be non-empty")
    if len(rows) != len(set(rows)):
        raise ToolGuideError(f"{field_name} must not contain duplicates")
    return rows


def _positive_int(value: object, field_name: str) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ToolGuideError(f"{field_name} must be an integer") from exc
    if number < 1:
        raise ToolGuideError(f"{field_name} must be positive")
    return number


def _date_text(value: object, field_name: str) -> str:
    text = _non_empty_text(value, field_name)
    try:
        date.fromisoformat(text)
    except ValueError as exc:
        raise ToolGuideError(f"{field_name} must be an ISO date") from exc
    return text


def _jsonable(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _jsonable(row) for key, row in value.items()}
    return value


@dataclass(frozen=True, slots=True)
class ToolGuideApplicability:
    """Rules that determine whether a guide can apply to an active tool."""

    tool_ids: tuple[str, ...]
    surface_kinds: tuple[str, ...] = ()
    workflow_action_ids: tuple[str, ...] = ()
    capability_fingerprints: tuple[str, ...] = ()
    capability_pack_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "tool_ids", _string_tuple(self.tool_ids, "tool_ids", required=True))
        object.__setattr__(self, "surface_kinds", _string_tuple(self.surface_kinds, "surface_kinds"))
        object.__setattr__(
            self,
            "workflow_action_ids",
            _string_tuple(self.workflow_action_ids, "workflow_action_ids"),
        )
        object.__setattr__(
            self,
            "capability_fingerprints",
            _string_tuple(self.capability_fingerprints, "capability_fingerprints"),
        )
        object.__setattr__(self, "capability_pack_ids", _string_tuple(self.capability_pack_ids, "capability_pack_ids"))

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> ToolGuideApplicability:
        return cls(
            tool_ids=_string_tuple(payload.get("tool_ids"), "tool_ids", required=True),
            surface_kinds=_string_tuple(payload.get("surface_kinds"), "surface_kinds"),
            workflow_action_ids=_string_tuple(payload.get("workflow_action_ids"), "workflow_action_ids"),
            capability_fingerprints=_string_tuple(payload.get("capability_fingerprints"), "capability_fingerprints"),
            capability_pack_ids=_string_tuple(payload.get("capability_pack_ids"), "capability_pack_ids"),
        )

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(asdict(self))

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"ToolGuideApplicability(tool_ids={self.tool_ids!r}, surface_kinds={self.surface_kinds!r}, workflow_action_ids={self.workflow_action_ids!r})"


@dataclass(frozen=True, slots=True)
class ToolGuide:
    """Versioned guidance row for one or more tool identities."""

    guide_id: str
    version: str
    applicability: ToolGuideApplicability
    title: str
    guidance: str
    safety_notes: tuple[str, ...]
    examples: tuple[str, ...]
    stale_after: str
    authored_at: str
    provenance_refs: tuple[str, ...]
    token_budget: int
    refresh_owner: str
    refresh_calendar_ref: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "guide_id", _non_empty_text(self.guide_id, "guide_id"))
        object.__setattr__(self, "version", _non_empty_text(self.version, "version"))
        object.__setattr__(self, "title", _non_empty_text(self.title, "title"))
        object.__setattr__(self, "guidance", _non_empty_text(self.guidance, "guidance"))
        object.__setattr__(self, "safety_notes", _string_tuple(self.safety_notes, "safety_notes", required=True))
        object.__setattr__(self, "examples", _string_tuple(self.examples, "examples"))
        object.__setattr__(self, "stale_after", _date_text(self.stale_after, "stale_after"))
        object.__setattr__(self, "authored_at", _date_text(self.authored_at, "authored_at"))
        object.__setattr__(
            self, "provenance_refs", _string_tuple(self.provenance_refs, "provenance_refs", required=True)
        )
        object.__setattr__(self, "token_budget", _positive_int(self.token_budget, "token_budget"))
        object.__setattr__(self, "refresh_owner", _non_empty_text(self.refresh_owner, "refresh_owner"))
        object.__setattr__(
            self, "refresh_calendar_ref", _non_empty_text(self.refresh_calendar_ref, "refresh_calendar_ref")
        )
        if date.fromisoformat(self.stale_after) < date.fromisoformat(self.authored_at):
            raise ToolGuideError("stale_after must not be older than authored_at")

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> ToolGuide:
        return cls(
            guide_id=str(payload.get("guide_id", "")),
            version=str(payload.get("version", "")),
            applicability=ToolGuideApplicability.from_mapping(payload),
            title=str(payload.get("title", "")),
            guidance=str(payload.get("guidance", "")),
            safety_notes=_string_tuple(payload.get("safety_notes"), "safety_notes", required=True),
            examples=_string_tuple(payload.get("examples"), "examples"),
            stale_after=str(payload.get("stale_after", "")),
            authored_at=str(payload.get("authored_at", "")),
            provenance_refs=_string_tuple(payload.get("provenance_refs"), "provenance_refs", required=True),
            token_budget=_positive_int(payload.get("token_budget"), "token_budget"),
            refresh_owner=str(payload.get("refresh_owner", "")),
            refresh_calendar_ref=str(payload.get("refresh_calendar_ref", "")),
        )

    def to_dict(self, *, include_guidance: bool = True) -> dict[str, Any]:
        """Execute the to dict operation.

        Returns:
            dict[str, Any] value produced by to_dict().
        """
        payload = {
            "guide_id": self.guide_id,
            "version": self.version,
            "title": self.title,
            "tool_ids": list(self.applicability.tool_ids),
            "surface_kinds": list(self.applicability.surface_kinds),
            "workflow_action_ids": list(self.applicability.workflow_action_ids),
            "capability_fingerprints": list(self.applicability.capability_fingerprints),
            "capability_pack_ids": list(self.applicability.capability_pack_ids),
            "safety_notes": list(self.safety_notes),
            "stale_after": self.stale_after,
            "authored_at": self.authored_at,
            "provenance_refs": list(self.provenance_refs),
            "token_budget": self.token_budget,
            "refresh_owner": self.refresh_owner,
            "refresh_calendar_ref": self.refresh_calendar_ref,
        }
        if include_guidance:
            payload["guidance"] = self.guidance
            payload["examples"] = list(self.examples)
        return payload

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"ToolGuide(guide_id={self.guide_id!r}, version={self.version!r}, applicability={self.applicability!r})"


@dataclass(frozen=True, slots=True)
class ActiveToolContext:
    """Caller-supplied active tool identity and narrowing metadata."""

    tool_id: str
    surface_kind: str | None = None
    workflow_action_id: str | None = None
    capability_fingerprints: tuple[str, ...] = ()
    capability_pack_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "tool_id", _non_empty_text(self.tool_id, "tool_id"))
        object.__setattr__(
            self,
            "surface_kind",
            None if self.surface_kind is None or not str(self.surface_kind).strip() else str(self.surface_kind).strip(),
        )
        object.__setattr__(
            self,
            "workflow_action_id",
            None
            if self.workflow_action_id is None or not str(self.workflow_action_id).strip()
            else str(self.workflow_action_id).strip(),
        )
        object.__setattr__(
            self,
            "capability_fingerprints",
            _string_tuple(self.capability_fingerprints, "capability_fingerprints"),
        )
        object.__setattr__(self, "capability_pack_ids", _string_tuple(self.capability_pack_ids, "capability_pack_ids"))

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> ActiveToolContext:
        return cls(
            tool_id=str(payload.get("tool_id", "")),
            surface_kind=payload.get("surface_kind"),
            workflow_action_id=payload.get("workflow_action_id"),
            capability_fingerprints=_string_tuple(payload.get("capability_fingerprints"), "capability_fingerprints"),
            capability_pack_ids=_string_tuple(payload.get("capability_pack_ids"), "capability_pack_ids"),
        )

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(asdict(self))

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"ActiveToolContext(tool_id={self.tool_id!r}, surface_kind={self.surface_kind!r}, workflow_action_id={self.workflow_action_id!r})"


@dataclass(frozen=True, slots=True)
class ToolGuideDiagnostic:
    """Typed diagnostic explaining why a guide was not selected or needs review."""

    status: ToolGuideStatus
    message: str
    guide_id: str | None = None
    detail: str | None = None
    provenance_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", ToolGuideStatus(self.status))
        object.__setattr__(self, "message", _non_empty_text(self.message, "message"))
        object.__setattr__(self, "provenance_refs", _string_tuple(self.provenance_refs, "provenance_refs"))

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(asdict(self))

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"ToolGuideDiagnostic(status={self.status!r}, message={self.message!r}, guide_id={self.guide_id!r})"


@dataclass(frozen=True, slots=True)
class SelectedToolGuide:
    """One selected guide with attribution and bounded text."""

    guide_id: str
    version: str
    text: str
    token_count: int
    attribution: str
    provenance_refs: tuple[str, ...]
    safety_notes: tuple[str, ...] = ()
    examples: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "guide_id", _non_empty_text(self.guide_id, "guide_id"))
        object.__setattr__(self, "version", _non_empty_text(self.version, "version"))
        object.__setattr__(self, "text", _non_empty_text(self.text, "text"))
        object.__setattr__(self, "token_count", _positive_int(self.token_count, "token_count"))
        object.__setattr__(self, "attribution", _non_empty_text(self.attribution, "attribution"))
        object.__setattr__(
            self, "provenance_refs", _string_tuple(self.provenance_refs, "provenance_refs", required=True)
        )
        object.__setattr__(self, "safety_notes", _string_tuple(self.safety_notes, "safety_notes"))
        object.__setattr__(self, "examples", _string_tuple(self.examples, "examples"))

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(asdict(self))

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"SelectedToolGuide(guide_id={self.guide_id!r}, version={self.version!r}, text={self.text!r})"


@dataclass(frozen=True, slots=True)
class ToolGuideSelection:
    """Selection response returned by runtime and API callers."""

    selected_guides: tuple[SelectedToolGuide, ...] = ()
    diagnostics: tuple[ToolGuideDiagnostic, ...] = ()
    bounded_text: str = ""
    total_token_count: int = 0
    token_budget: int = 0

    def __post_init__(self) -> None:
        if self.total_token_count < 0:
            raise ToolGuideError("total_token_count must be non-negative")
        if self.token_budget < 0:
            raise ToolGuideError("token_budget must be non-negative")
        if self.selected_guides and not self.bounded_text.strip():
            raise ToolGuideError("selected rows require bounded_text")
        for selected in self.selected_guides:
            if selected.attribution not in self.bounded_text:
                raise ToolGuideError("selected rows require attribution in bounded_text")

    def to_dict(self) -> dict[str, Any]:
        return {
            "selected_guides": [guide.to_dict() for guide in self.selected_guides],
            "diagnostics": [diagnostic.to_dict() for diagnostic in self.diagnostics],
            "bounded_text": self.bounded_text,
            "total_token_count": self.total_token_count,
            "token_budget": self.token_budget,
        }

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"ToolGuideSelection(selected_guides={self.selected_guides!r}, diagnostics={self.diagnostics!r}, bounded_text={self.bounded_text!r})"


def validate_tool_guide_catalog_payload(payload: Mapping[str, Any]) -> tuple[ToolGuide, ...]:
    """Validate and return catalog rows from a schema-shaped payload.

    Returns:
        Validation outcome for tool guide catalog payload.

    Raises:
        Exception: Propagates validation or runtime failures from the underlying operation.
    """
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise ToolGuideError(f"schema_version must be {SCHEMA_VERSION}")
    raw_guides = payload.get("guides")
    if not isinstance(raw_guides, Sequence) or isinstance(raw_guides, (str, bytes)) or not raw_guides:
        raise ToolGuideError("guides must be a non-empty list")
    guides: list[ToolGuide] = []
    seen: set[str] = set()
    for raw in raw_guides:
        if not isinstance(raw, Mapping):
            raise ToolGuideError("guide rows must be mappings")
        guide = ToolGuide.from_mapping(raw)
        if guide.guide_id in seen:
            raise ToolGuideError(f"duplicate guide id {guide.guide_id}")
        seen.add(guide.guide_id)
        guides.append(guide)
    return tuple(guides)


__all__ = [
    "SCHEMA_VERSION",
    "ActiveToolContext",
    "SelectedToolGuide",
    "ToolGuide",
    "ToolGuideApplicability",
    "ToolGuideDiagnostic",
    "ToolGuideError",
    "ToolGuideSelection",
    "ToolGuideStatus",
    "fallback_estimate_tokens",
    "validate_tool_guide_catalog_payload",
]
