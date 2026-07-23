"""Source-card policy layer over the Workbench metadata spine.

This module is step 1 of the source-tool pipeline:
source-card -> tool-card -> claim-promotion gate. Source cards capture
provenance, freshness, caveats, cache policy, rate limits, citation
requirements, and known limits for external sources. The module is
read-only against the spine; it never appends or records promotions.

Side effects: no I/O occurs at import time. The first call to
``load_source_policies()`` reads ``config/workbench/source_policies.yaml``
under ``_POLICY_CATALOG_LOCK`` and populates ``_POLICY_CATALOG_CACHE``.
The first library read may construct the process WorkbenchSpine singleton.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

import yaml

from vetinari.constants import PROJECT_ROOT
from vetinari.security.redaction import redact_route_payload
from vetinari.workbench.assets import WorkbenchAsset
from vetinari.workbench.metadata_spine import (
    WorkbenchSpine,
    WorkbenchSpineCorrupt,
    get_workbench_spine,
)

logger = logging.getLogger(__name__)


_POLICY_CATALOG_CONFIG_PATH: Path = PROJECT_ROOT / "config" / "workbench" / "source_policies.yaml"
_POLICY_CATALOG_LOCK: threading.Lock = threading.Lock()
_POLICY_CATALOG_CACHE: dict[str, SourcePolicyDefaults] = {}


class SourceCardsProjectIdRejected(ValueError):
    """Raised when an inbound project id is not canonical."""

    def __init__(self, value: object) -> None:
        super().__init__(f"invalid project_id {value!r}; use [A-Za-z0-9_-] up to 64 characters")
        self.value = value


class SourceCardLibraryError(Exception):
    """Raised when source-card policy or spine state cannot be trusted."""


class SourceKind(str, Enum):
    """Canonical source kinds understood by source cards."""

    WEB_PAGE = "web_page"
    MCP_SERVER = "mcp_server"
    HTTP_API = "http_api"
    LOCAL_TOOL = "local_tool"
    SCRAPER_RECIPE = "scraper_recipe"
    DATASET_REF = "dataset_ref"
    SEARCH_INDEX = "search_index"


class StalenessAction(str, Enum):
    """Action taken when a source is stale."""

    REJECT = "reject"
    DEMOTE_TO_OBSERVATION = "demote_to_observation"
    WARN = "warn"


@dataclass(frozen=True, slots=True)
class FreshnessPolicy:
    """Deterministic freshness window for one source card."""

    max_age_seconds: int
    staleness_action: StalenessAction = StalenessAction.REJECT

    def __post_init__(self) -> None:
        if self.max_age_seconds < 0:
            raise ValueError("max_age_seconds must be non-negative")
        if not isinstance(self.staleness_action, StalenessAction):
            raise ValueError("staleness_action must be a StalenessAction")

    def to_dict(self) -> dict[str, Any]:
        """Return the source-card API JSON contract for this policy."""
        return {
            "freshness_max_age_seconds": self.max_age_seconds,
            "freshness_staleness_action": self.staleness_action.value,
        }


@dataclass(frozen=True, slots=True)
class FreshnessVerdict:
    """Result of evaluating a source card's freshness."""

    passed: bool
    age_seconds: int | None
    reason: str
    staleness_action: StalenessAction

    def __post_init__(self) -> None:
        if not self.passed and not self.reason.strip():
            raise ValueError("failed freshness verdicts require a reason")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"FreshnessVerdict(passed={self.passed!r}, age_seconds={self.age_seconds!r}, reason={self.reason!r})"


@dataclass(frozen=True, slots=True)
class SourceCard:
    """Typed source card read from real WorkbenchAsset spine data."""

    source_card_id: str
    kind: SourceKind
    name: str
    project_id: str
    created_at_utc: str
    observed_at_utc: str | None
    can_answer: tuple[str, ...]
    cannot_answer: tuple[str, ...]
    freshness_policy: FreshnessPolicy
    cache_policy: str
    rate_limit_per_minute: int
    cite_required: bool
    caveats: tuple[str, ...]
    credential_exposure: str
    good_use_examples: tuple[str, ...]
    bad_use_examples: tuple[str, ...]
    provenance: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        _require_non_empty(self.source_card_id, "source_card_id")
        _require_non_empty(self.name, "name")
        _canonicalize_project_id(self.project_id)
        _require_non_empty(self.created_at_utc, "created_at_utc")
        if not isinstance(self.kind, SourceKind):
            raise ValueError("kind must be a SourceKind")
        if not isinstance(self.freshness_policy, FreshnessPolicy):
            raise ValueError("freshness_policy must be a FreshnessPolicy")
        if self.rate_limit_per_minute < 0:
            raise ValueError("rate_limit_per_minute must be non-negative")
        if not self.provenance:
            raise ValueError("provenance must be non-empty")
        for key, value in self.provenance:
            _require_non_empty(key, "provenance key")
            _require_non_empty(value, "provenance value")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"SourceCard(source_card_id={self.source_card_id!r}, kind={self.kind!r}, name={self.name!r})"

    def to_dict(self) -> dict[str, Any]:
        """Return the source-card API JSON contract for this card.

        Returns:
            Value produced for the caller.
        """
        freshness = self.freshness_policy.to_dict()
        return {
            "source_card_id": self.source_card_id,
            "kind": self.kind.value,
            "name": self.name,
            "project_id": self.project_id,
            "created_at_utc": self.created_at_utc,
            "observed_at_utc": self.observed_at_utc,
            "can_answer": list(self.can_answer),
            "cannot_answer": list(self.cannot_answer),
            "freshness_max_age_seconds": freshness["freshness_max_age_seconds"],
            "freshness_staleness_action": freshness["freshness_staleness_action"],
            "cache_policy": self.cache_policy,
            "rate_limit_per_minute": self.rate_limit_per_minute,
            "cite_required": self.cite_required,
            "caveats": list(self.caveats),
            "credential_exposure": self.credential_exposure,
            "good_use_examples": list(self.good_use_examples),
            "bad_use_examples": list(self.bad_use_examples),
            "provenance": list(self.provenance),
        }


@dataclass(frozen=True, slots=True)
class SourcePolicyDefaults:
    """Defaults loaded from the source policy catalog."""

    kind: SourceKind
    display_label: str
    default_freshness_seconds: int
    default_staleness_action: StalenessAction
    default_rate_limit_per_minute: int
    cite_required_default: bool
    typical_caveats: tuple[str, ...]

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"SourcePolicyDefaults(kind={self.kind!r}, display_label={self.display_label!r}, default_freshness_seconds={self.default_freshness_seconds!r})"


def _require_non_empty(value: str, field_name: str) -> None:
    if not value or not value.strip():
        raise ValueError(f"{field_name} must be non-empty")


def _canonicalize_project_id(value: str | None) -> str:
    """Return the shared spine project id or fail closed with this module's error."""
    from vetinari.workbench.spine import WorkbenchProjectIdRejected, validate_project_id

    try:
        return validate_project_id(value)
    except WorkbenchProjectIdRejected as exc:
        raise SourceCardsProjectIdRejected(value) from exc


def _parse_utc(value: str) -> datetime:
    normalized = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def evaluate_freshness(
    card: SourceCard,
    *,
    now_utc: datetime | None = None,
) -> FreshnessVerdict:
    """Evaluate freshness deterministically and fail closed for unknown state.

    Returns:
        FreshnessVerdict value produced by evaluate_freshness().
    """
    now = now_utc or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    now = now.astimezone(timezone.utc)
    policy = card.freshness_policy
    if card.observed_at_utc is None:
        return FreshnessVerdict(
            passed=False,
            age_seconds=None,
            reason="source never observed; freshness unknown",
            staleness_action=policy.staleness_action,
        )
    try:
        observed = _parse_utc(card.observed_at_utc)
    except ValueError:
        logger.warning("Handled recoverable failure before fallback.", exc_info=True)
        return FreshnessVerdict(
            passed=False,
            age_seconds=None,
            reason="observed_at_utc malformed; freshness unknown",
            staleness_action=policy.staleness_action,
        )
    age = int((now - observed).total_seconds())
    if age < 0:
        return FreshnessVerdict(
            passed=False,
            age_seconds=age,
            reason="observed_at_utc is in the future",
            staleness_action=policy.staleness_action,
        )
    if age <= policy.max_age_seconds:
        return FreshnessVerdict(
            passed=True,
            age_seconds=age,
            reason="source observed within freshness window",
            staleness_action=policy.staleness_action,
        )
    return FreshnessVerdict(
        passed=False,
        age_seconds=age,
        reason=f"source age {age}s exceeds max_age_seconds {policy.max_age_seconds}",
        staleness_action=policy.staleness_action,
    )


def load_source_policies() -> dict[str, SourcePolicyDefaults]:
    """Return source policy defaults from a guarded read-mostly cache.

    Returns:
        Resolved source policies value.
    """
    if _POLICY_CATALOG_CACHE:
        return dict(_POLICY_CATALOG_CACHE)
    with _POLICY_CATALOG_LOCK:
        if not _POLICY_CATALOG_CACHE:
            _POLICY_CATALOG_CACHE.update(_load_source_policies_uncached())
        return dict(_POLICY_CATALOG_CACHE)


def _load_source_policies_uncached() -> dict[str, SourcePolicyDefaults]:
    try:
        raw = yaml.safe_load(_POLICY_CATALOG_CONFIG_PATH.read_text(encoding="utf-8"))
    except OSError as exc:
        raise SourceCardLibraryError("source policy catalog unavailable") from exc
    if not isinstance(raw, dict):
        raise SourceCardLibraryError("source policy catalog must be a mapping")
    rows = raw.get("kinds")
    if not isinstance(rows, list):
        raise SourceCardLibraryError("source policy catalog missing kinds list")
    defaults: dict[str, SourcePolicyDefaults] = {}
    enum_values = {member.value for member in SourceKind}
    for row in rows:
        if not isinstance(row, dict):
            raise SourceCardLibraryError("source policy row must be a mapping")
        kind_id = str(row.get("id", ""))
        if kind_id not in enum_values:
            raise SourceCardLibraryError(f"unknown source kind in catalog: {kind_id!r}")
        action = StalenessAction(str(row.get("default_staleness_action", "")))
        if action is not StalenessAction.REJECT:
            raise SourceCardLibraryError(f"source kind {kind_id!r} must default to reject")
        defaults[kind_id] = SourcePolicyDefaults(
            kind=SourceKind(kind_id),
            display_label=str(row.get("display_label", "")),
            default_freshness_seconds=int(row.get("default_freshness_seconds", -1)),
            default_staleness_action=action,
            default_rate_limit_per_minute=int(row.get("default_rate_limit_per_minute", -1)),
            cite_required_default=bool(row.get("cite_required_default", True)),
            typical_caveats=_string_tuple(row.get("typical_caveats", ())),
        )
    if set(defaults) != enum_values:
        raise SourceCardLibraryError("source policy catalog does not match SourceKind enum")
    return defaults


class SourceCardLibrary:
    """Read-only source-card builder over WorkbenchAsset spine records."""

    def __init__(self, spine: WorkbenchSpine | None = None) -> None:
        self._spine = spine

    def list_cards(
        self,
        *,
        project_id: str,
        kind: SourceKind | None = None,
        fresh_only: bool = False,
        now_utc: datetime | None = None,
    ) -> tuple[SourceCard, ...]:
        """Execute the list cards operation.

        Returns:
            Collection of cards values.
        """
        canonical_project_id = _canonicalize_project_id(project_id)
        spine = self._get_or_init_spine()
        policies = load_source_policies()
        cards = [
            _apply_policy_to_card(asset, policies, canonical_project_id)
            for asset in spine.list_assets()
            if asset.provenance.get("source_card_kind") and asset.provenance.get("project_id") == canonical_project_id
        ]
        if kind is not None:
            cards = [card for card in cards if card.kind is kind]
        if fresh_only:
            cards = [card for card in cards if evaluate_freshness(card, now_utc=now_utc).passed]
        return tuple(sorted(cards, key=lambda card: card.created_at_utc, reverse=True))

    def get_card(self, *, project_id: str, source_card_id: str) -> SourceCard | None:
        """Execute the get card operation.

        Returns:
            Resolved card value.
        """
        canonical_project_id = _canonicalize_project_id(project_id)
        spine = self._get_or_init_spine()
        policies = load_source_policies()
        asset = _lookup_source_card_asset(spine, source_card_id)
        if asset is not None and asset.provenance.get("project_id") == canonical_project_id:
            return _apply_policy_to_card(asset, policies, canonical_project_id)
        return None

    def _get_or_init_spine(self) -> WorkbenchSpine:
        if self._spine is not None:
            return self._spine
        try:
            self._spine = get_workbench_spine()
        except WorkbenchSpineCorrupt as exc:
            raise SourceCardLibraryError("workbench spine unavailable") from exc
        return self._spine


def _apply_policy_to_card(
    asset: WorkbenchAsset,
    policies: dict[str, SourcePolicyDefaults],
    project_id: str,
) -> SourceCard:
    provenance = dict(asset.provenance)
    kind = SourceKind(provenance["source_card_kind"])
    defaults = policies[kind.value]
    max_age = int(provenance.get("freshness_window_seconds", defaults.default_freshness_seconds))
    staleness = StalenessAction(provenance.get("staleness_action", defaults.default_staleness_action.value))
    return SourceCard(
        source_card_id=provenance.get("source_card_id", asset.asset_id),
        kind=kind,
        name=asset.name,
        project_id=project_id,
        created_at_utc=asset.created_at_utc,
        observed_at_utc=provenance.get("observed_at_utc") or None,
        can_answer=_provenance_tuple(provenance, "can_answer"),
        cannot_answer=_provenance_tuple(provenance, "cannot_answer"),
        freshness_policy=FreshnessPolicy(max_age_seconds=max_age, staleness_action=staleness),
        cache_policy=provenance.get("cache_policy", "default"),
        rate_limit_per_minute=int(provenance.get("rate_limit_per_minute", defaults.default_rate_limit_per_minute)),
        cite_required=_parse_bool(provenance.get("cite_required"), defaults.cite_required_default),
        caveats=_provenance_tuple(provenance, "caveats") or defaults.typical_caveats,
        credential_exposure=provenance.get("credential_exposure", "none"),
        good_use_examples=_provenance_tuple(provenance, "good_use_examples"),
        bad_use_examples=_provenance_tuple(provenance, "bad_use_examples"),
        provenance=_redacted_provenance_pairs(provenance),
    )


def _redacted_provenance_pairs(provenance: dict[str, str]) -> tuple[tuple[str, str], ...]:
    redacted = redact_route_payload(provenance)
    if not isinstance(redacted, dict):
        raise SourceCardLibraryError("redaction returned unexpected provenance shape")
    return tuple(sorted((str(key), str(value)) for key, value in redacted.items() if str(value).strip()))


def _lookup_source_card_asset(spine: WorkbenchSpine, source_card_id: str) -> WorkbenchAsset | None:
    if hasattr(spine, "get_asset"):
        asset = spine.get_asset(source_card_id)
        if isinstance(asset, WorkbenchAsset):
            return asset
    require_conn = getattr(spine, "_require_conn", None)
    record_from_row = getattr(spine, "_record_from_row", None)
    if callable(require_conn) and callable(record_from_row):
        row = (
            require_conn()
            .execute(
                "SELECT kind, record_id, payload FROM records WHERE kind = ? AND record_id = ? LIMIT 1",
                ("asset", source_card_id),
            )
            .fetchone()
        )
        if row is not None:
            asset = record_from_row(row)
            if isinstance(asset, WorkbenchAsset):
                return asset
    for asset in spine.list_assets():
        if asset.asset_id == source_card_id or asset.provenance.get("source_card_id") == source_card_id:
            return asset
    return None


def _provenance_tuple(provenance: dict[str, str], key: str) -> tuple[str, ...]:
    return _string_tuple(provenance.get(key, ()))


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
    with_json = _parse_json_list(value)
    if with_json is not None:
        return with_json
    separator = "|" if "|" in value else ","
    return tuple(part.strip() for part in value.split(separator) if part.strip())


def _parse_json_list(value: str) -> tuple[str, ...] | None:
    if not value.strip().startswith("["):
        return None
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        logger.warning("Handled recoverable failure before fallback.", exc_info=True)
        return None
    if not isinstance(parsed, list):
        return None
    return tuple(str(item) for item in parsed if str(item).strip())


def _parse_bool(value: str | None, default: bool) -> bool:
    if value is None or value == "":
        return default
    lowered = value.lower()
    if lowered in {"1", "true", "yes"}:
        return True
    if lowered in {"0", "false", "no"}:
        return False
    raise SourceCardLibraryError(f"invalid boolean value {value!r}")


__all__ = [
    "FreshnessPolicy",
    "FreshnessVerdict",
    "SourceCard",
    "SourceCardLibrary",
    "SourceCardLibraryError",
    "SourceCardsProjectIdRejected",
    "SourceKind",
    "StalenessAction",
    "evaluate_freshness",
    "load_source_policies",
]
