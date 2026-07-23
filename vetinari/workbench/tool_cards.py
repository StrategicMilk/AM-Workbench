"""Tool cards and the fail-closed claim-promotion gate.

This module is step 2 of the source-tool pipeline:
source-card -> tool-card -> claim-promotion gate. Tool outputs remain
observations unless a tool card explicitly permits stronger claims.
Unknown freshness, provenance, caveat policy, or claim kind fails closed.

Side effects: no I/O occurs at import time. The first call to
``_get_or_load_tool_defaults()`` reads the ``tools`` section of
``config/workbench/source_policies.yaml`` under ``_TOOL_CATALOG_LOCK``.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any

import yaml

from vetinari.workbench.assets import WorkbenchAsset
from vetinari.workbench.metadata_spine import (
    WorkbenchSpine,
    WorkbenchSpineCorrupt,
    get_workbench_spine,
)
from vetinari.workbench.source_cards import (
    _POLICY_CATALOG_CONFIG_PATH,
    SourceCard,
    SourceCardLibrary,
    _canonicalize_project_id,
    evaluate_freshness,
)

logger = logging.getLogger(__name__)


_TOOL_CATALOG_LOCK: threading.Lock = threading.Lock()
_TOOL_CATALOG_CACHE: dict[str, ToolDefaults] = {}


class ToolKind(str, Enum):
    """Canonical tool kinds understood by tool cards."""

    WEB_SCRAPER = "web_scraper"
    MCP_INVOCATION = "mcp_invocation"
    HTTP_CALLER = "http_caller"
    LOCAL_FUNCTION = "local_function"
    DATASET_QUERY = "dataset_query"
    SEARCH_QUERY = "search_query"


class ToolCardLibraryError(Exception):
    """Raised when tool-card policy or spine state cannot be trusted."""


@dataclass(frozen=True, slots=True)
class ClaimPromotionPolicy:
    """Most-restrictive default policy for promoting observations to claims."""

    requires_freshness_pass: bool = True
    requires_provenance_present: bool = True
    requires_caveats_acknowledged: bool = True
    permitted_claim_kinds: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if any(not kind.strip() for kind in self.permitted_claim_kinds):
            raise ValueError("permitted_claim_kinds cannot contain blank values")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"ClaimPromotionPolicy(requires_freshness_pass={self.requires_freshness_pass!r}, requires_provenance_present={self.requires_provenance_present!r}, requires_caveats_acknowledged={self.requires_caveats_acknowledged!r})"

    def to_dict(self) -> dict[str, Any]:
        """Return the tool-card API JSON contract for this policy."""
        return {
            "requires_freshness_pass": self.requires_freshness_pass,
            "requires_provenance_present": self.requires_provenance_present,
            "requires_caveats_acknowledged": self.requires_caveats_acknowledged,
            "permitted_claim_kinds": list(self.permitted_claim_kinds),
        }


@dataclass(frozen=True, slots=True)
class ClaimPromotionDecision:
    """Decision returned by the fail-closed claim-promotion gate."""

    passed: bool
    permitted_claim_kind: str | None
    rejection_reasons: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.passed:
            if not self.permitted_claim_kind:
                raise ValueError("passed decisions require permitted_claim_kind")
            if self.rejection_reasons:
                raise ValueError("passed decisions cannot include rejection_reasons")
        elif not self.rejection_reasons:
            raise ValueError("failed decisions require rejection_reasons")

    def to_dict(self) -> dict[str, Any]:
        """Return the claim-promotion API JSON contract for this decision."""
        return {
            "passed": self.passed,
            "permitted_claim_kind": self.permitted_claim_kind,
            "rejection_reasons": list(self.rejection_reasons),
        }


@dataclass(frozen=True, slots=True)
class ToolCard:
    """Typed tool card read from real WorkbenchAsset spine data."""

    tool_card_id: str
    kind: ToolKind
    name: str
    project_id: str
    created_at_utc: str
    source_card_ids: tuple[str, ...]
    claim_promotion_policy: ClaimPromotionPolicy
    safety_caveats: tuple[str, ...]
    rate_limit_per_minute: int
    good_use_examples: tuple[str, ...]
    bad_use_examples: tuple[str, ...]
    provenance: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        _require_non_empty(self.tool_card_id, "tool_card_id")
        _require_non_empty(self.name, "name")
        _canonicalize_project_id(self.project_id)
        _require_non_empty(self.created_at_utc, "created_at_utc")
        if not isinstance(self.kind, ToolKind):
            raise ValueError("kind must be a ToolKind")
        if not isinstance(self.claim_promotion_policy, ClaimPromotionPolicy):
            raise ValueError("claim_promotion_policy must be a ClaimPromotionPolicy")
        if self.rate_limit_per_minute < 0:
            raise ValueError("rate_limit_per_minute must be non-negative")
        if not _has_source_provenance(self.provenance):
            raise ValueError("provenance must contain a non-empty source entry")

    def may_promote_to_claim(
        self,
        *,
        claim_kind: str,
        sources: tuple[SourceCard, ...],
        caveats_acknowledged: bool,
        now_utc: datetime | None = None,
    ) -> ClaimPromotionDecision:
        """Return whether observations from this tool may become a claim.

        Returns:
            ClaimPromotionDecision value produced by may_promote_to_claim().
        """
        policy = self.claim_promotion_policy
        reasons: list[str] = []
        if claim_kind not in policy.permitted_claim_kinds:
            reasons.append(f"claim_kind {claim_kind!r} is not permitted")
        if policy.requires_freshness_pass:
            verdicts = tuple(evaluate_freshness(source, now_utc=now_utc) for source in sources)
            if not verdicts:
                reasons.append("no sources supplied for required freshness check")
            for source, verdict in zip(sources, verdicts, strict=True):
                if not verdict.passed:
                    reasons.append(f"freshness failed for {source.source_card_id}: {verdict.reason}")
        if policy.requires_provenance_present:
            if not sources:
                reasons.append("no sources supplied for required provenance check")
            reasons.extend(
                f"provenance missing for {source.source_card_id}"
                for source in sources
                if not _has_source_provenance(source.provenance)
            )
        if policy.requires_caveats_acknowledged and not caveats_acknowledged:
            reasons.append("caveats must be acknowledged before claim promotion")
        if reasons:
            return ClaimPromotionDecision(
                passed=False,
                permitted_claim_kind=None,
                rejection_reasons=tuple(reasons),
            )
        return ClaimPromotionDecision(
            passed=True,
            permitted_claim_kind=claim_kind,
            rejection_reasons=(),
        )

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"ToolCard(tool_card_id={self.tool_card_id!r}, kind={self.kind!r}, name={self.name!r})"

    def to_dict(self) -> dict[str, Any]:
        """Return the tool-card API JSON contract for this card.

        Returns:
            Value produced for the caller.
        """
        policy = self.claim_promotion_policy.to_dict()
        return {
            "tool_card_id": self.tool_card_id,
            "kind": self.kind.value,
            "name": self.name,
            "project_id": self.project_id,
            "created_at_utc": self.created_at_utc,
            "source_card_ids": list(self.source_card_ids),
            "requires_freshness_pass": policy["requires_freshness_pass"],
            "requires_provenance_present": policy["requires_provenance_present"],
            "requires_caveats_acknowledged": policy["requires_caveats_acknowledged"],
            "permitted_claim_kinds": policy["permitted_claim_kinds"],
            "safety_caveats": list(self.safety_caveats),
            "rate_limit_per_minute": self.rate_limit_per_minute,
            "good_use_examples": list(self.good_use_examples),
            "bad_use_examples": list(self.bad_use_examples),
            "provenance": list(self.provenance),
        }


@dataclass(frozen=True, slots=True)
class ToolDefaults:
    """Defaults loaded from the tool policy catalog."""

    kind: ToolKind
    default_rate_limit_per_minute: int
    default_safety_caveats: tuple[str, ...]
    default_permitted_claim_kinds: tuple[str, ...]

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"ToolDefaults(kind={self.kind!r}, default_rate_limit_per_minute={self.default_rate_limit_per_minute!r}, default_safety_caveats={self.default_safety_caveats!r})"


class ToolCardLibrary:
    """Read-only tool-card builder over WorkbenchAsset spine records."""

    def __init__(
        self,
        spine: WorkbenchSpine | None = None,
        source_library: SourceCardLibrary | None = None,
    ) -> None:
        self._spine = spine
        self._source_library = source_library

    def list_cards(self, *, project_id: str, kind: ToolKind | None = None) -> tuple[ToolCard, ...]:
        """Execute the list cards operation.

        Returns:
            Collection of cards values.
        """
        canonical_project_id = _canonicalize_project_id(project_id)
        spine = self._get_or_init_spine()
        defaults = _get_or_load_tool_defaults()
        cards = [
            _asset_to_tool_card(asset, defaults, canonical_project_id)
            for asset in spine.list_assets()
            if asset.provenance.get("tool_card_kind") and asset.provenance.get("project_id") == canonical_project_id
        ]
        if kind is not None:
            cards = [card for card in cards if card.kind is kind]
        return tuple(sorted(cards, key=lambda card: card.created_at_utc, reverse=True))

    def get_card(self, *, project_id: str, tool_card_id: str) -> ToolCard | None:
        """Execute the get card operation.

        Returns:
            Resolved card value.
        """
        canonical_project_id = _canonicalize_project_id(project_id)
        for card in self.list_cards(project_id=canonical_project_id):
            if card.tool_card_id == tool_card_id:
                return card
        return None

    def evaluate_promotion(
        self,
        *,
        project_id: str,
        tool_card_id: str,
        claim_kind: str,
        caveats_acknowledged: bool,
        now_utc: datetime | None = None,
    ) -> ClaimPromotionDecision:
        """Execute the evaluate promotion operation.

        Returns:
            ClaimPromotionDecision value produced by evaluate_promotion().
        """
        canonical_project_id = _canonicalize_project_id(project_id)
        tool_card = self.get_card(project_id=canonical_project_id, tool_card_id=tool_card_id)
        if tool_card is None:
            return ClaimPromotionDecision(
                passed=False,
                permitted_claim_kind=None,
                rejection_reasons=("tool_card not found",),
            )
        source_library = self._get_or_init_source_library()
        sources = tuple(
            source
            for source_id in tool_card.source_card_ids
            if (source := source_library.get_card(project_id=canonical_project_id, source_card_id=source_id))
            is not None
        )
        return tool_card.may_promote_to_claim(
            claim_kind=claim_kind,
            sources=sources,
            caveats_acknowledged=caveats_acknowledged,
            now_utc=now_utc,
        )

    def _get_or_init_spine(self) -> WorkbenchSpine:
        if self._spine is not None:
            return self._spine
        try:
            self._spine = get_workbench_spine()
        except WorkbenchSpineCorrupt as exc:
            raise ToolCardLibraryError("workbench spine unavailable") from exc
        return self._spine

    def _get_or_init_source_library(self) -> SourceCardLibrary:
        if self._source_library is None:
            self._source_library = SourceCardLibrary(self._get_or_init_spine())
        return self._source_library


def _get_or_load_tool_defaults() -> dict[str, ToolDefaults]:
    if _TOOL_CATALOG_CACHE:
        return dict(_TOOL_CATALOG_CACHE)
    with _TOOL_CATALOG_LOCK:
        if not _TOOL_CATALOG_CACHE:
            _TOOL_CATALOG_CACHE.update(_load_tool_defaults_uncached())
        return dict(_TOOL_CATALOG_CACHE)


def _load_tool_defaults_uncached() -> dict[str, ToolDefaults]:
    try:
        raw = yaml.safe_load(_POLICY_CATALOG_CONFIG_PATH.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ToolCardLibraryError("tool policy catalog unavailable") from exc
    if not isinstance(raw, dict) or not isinstance(raw.get("tools"), list):
        raise ToolCardLibraryError("tool policy catalog missing tools list")
    enum_values = {member.value for member in ToolKind}
    defaults: dict[str, ToolDefaults] = {}
    for row in raw["tools"]:
        if not isinstance(row, dict):
            raise ToolCardLibraryError("tool policy row must be a mapping")
        kind_id = str(row.get("kind", ""))
        if kind_id not in enum_values:
            raise ToolCardLibraryError(f"unknown tool kind in catalog: {kind_id!r}")
        defaults[kind_id] = ToolDefaults(
            kind=ToolKind(kind_id),
            default_rate_limit_per_minute=int(row.get("default_rate_limit_per_minute", -1)),
            default_safety_caveats=_string_tuple(row.get("default_safety_caveats", ())),
            default_permitted_claim_kinds=_string_tuple(row.get("default_permitted_claim_kinds", ())),
        )
    if set(defaults) != enum_values:
        raise ToolCardLibraryError("tool policy catalog does not match ToolKind enum")
    return defaults


def _asset_to_tool_card(
    asset: WorkbenchAsset,
    defaults: dict[str, ToolDefaults],
    project_id: str,
) -> ToolCard:
    provenance = dict(asset.provenance)
    kind = ToolKind(provenance["tool_card_kind"])
    tool_defaults = defaults[kind.value]
    permitted = (
        _string_tuple(provenance.get("permitted_claim_kinds", ())) or tool_defaults.default_permitted_claim_kinds
    )
    return ToolCard(
        tool_card_id=provenance.get("tool_card_id", asset.asset_id),
        kind=kind,
        name=asset.name,
        project_id=project_id,
        created_at_utc=asset.created_at_utc,
        source_card_ids=_string_tuple(provenance.get("source_card_ids", ())),
        claim_promotion_policy=ClaimPromotionPolicy(
            requires_freshness_pass=_parse_bool(provenance.get("requires_freshness_pass"), True),
            requires_provenance_present=_parse_bool(provenance.get("requires_provenance_present"), True),
            requires_caveats_acknowledged=_parse_bool(provenance.get("requires_caveats_acknowledged"), True),
            permitted_claim_kinds=permitted,
        ),
        safety_caveats=_string_tuple(provenance.get("safety_caveats", ())) or tool_defaults.default_safety_caveats,
        rate_limit_per_minute=int(provenance.get("rate_limit_per_minute", tool_defaults.default_rate_limit_per_minute)),
        good_use_examples=_string_tuple(provenance.get("good_use_examples", ())),
        bad_use_examples=_string_tuple(provenance.get("bad_use_examples", ())),
        provenance=tuple(sorted((str(key), str(value)) for key, value in provenance.items() if str(value))),
    )


def _require_non_empty(value: str, field_name: str) -> None:
    if not value or not value.strip():
        raise ValueError(f"{field_name} must be non-empty")


def _has_source_provenance(provenance: tuple[tuple[str, str], ...]) -> bool:
    return any(key == "source" and bool(value.strip()) for key, value in provenance)


def _string_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, tuple):
        return tuple(str(item) for item in value if str(item).strip())
    if isinstance(value, list):
        return tuple(str(item) for item in value if str(item).strip())
    if not isinstance(value, str):
        return (str(value),)
    if not value.strip():
        return ()
    separator = "|" if "|" in value else ","
    return tuple(part.strip() for part in value.split(separator) if part.strip())


def _parse_bool(value: str | None, default: bool) -> bool:
    if value is None or value == "":
        return default
    lowered = value.lower()
    if lowered in {"1", "true", "yes"}:
        return True
    if lowered in {"0", "false", "no"}:
        return False
    raise ToolCardLibraryError(f"invalid boolean value {value!r}")


__all__ = [
    "ClaimPromotionDecision",
    "ClaimPromotionPolicy",
    "ToolCard",
    "ToolCardLibrary",
    "ToolCardLibraryError",
    "ToolKind",
]
