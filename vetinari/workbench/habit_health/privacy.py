"""Fail-closed privacy policy for habit-health records and downstream use."""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any

from vetinari.workbench.habit_health.contracts import HabitHealthScope

logger = logging.getLogger(__name__)


class HabitHealthUse(str, Enum):
    """Allowed downstream-use vocabulary for habit-health signals."""

    STORE = "store"
    REVIEW = "review"
    EXPORT = "export"
    SCHEDULING = "scheduling"
    PERSONALIZATION = "personalization"
    RESOURCE_PLANNING = "resource_planning"
    MEMORY_CONTEXT = "memory_context"


@dataclass(frozen=True, slots=True)
class HabitHealthScopePolicy:
    """Consent and provenance requirements for one habit-health request."""

    user_id: str
    allowed_scopes: tuple[HabitHealthScope | str, ...]
    consent_refs: tuple[str, ...]
    provenance_ref: str
    source_context: str
    allowed_downstream_uses: tuple[HabitHealthUse | str, ...] = (HabitHealthUse.STORE, HabitHealthUse.REVIEW)
    downstream_contract_refs: tuple[str, ...] = ()
    memory_scope: str = ""
    retention_days: int = 90
    local_only: bool = True
    sensitive: bool = False

    def normalized_scopes(self) -> tuple[HabitHealthScope, ...]:
        return tuple(_coerce_scope(scope) for scope in self.allowed_scopes)

    def normalized_uses(self) -> tuple[HabitHealthUse, ...]:
        """Execute the normalized uses operation.

        Returns:
            tuple[HabitHealthUse, ...] value produced by normalized_uses().
        """
        uses: list[HabitHealthUse] = []
        for item in self.allowed_downstream_uses:
            try:
                uses.append(item if isinstance(item, HabitHealthUse) else HabitHealthUse(str(item)))
            except ValueError:
                logger.warning("Handled recoverable failure before fallback.", exc_info=True)
                continue
        return tuple(uses)

    def to_dict(self) -> dict[str, Any]:
        """Execute the to dict operation.

        Returns:
            dict[str, Any] value produced by to_dict().
        """
        payload = asdict(self)
        payload["allowed_scopes"] = [
            scope.value if isinstance(scope, HabitHealthScope) else str(scope) for scope in self.allowed_scopes
        ]
        payload["allowed_downstream_uses"] = [
            use.value if isinstance(use, HabitHealthUse) else str(use) for use in self.allowed_downstream_uses
        ]
        return payload

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"HabitHealthScopePolicy(user_id={self.user_id!r}, allowed_scopes={self.allowed_scopes!r}, consent_refs={self.consent_refs!r})"


@dataclass(frozen=True, slots=True)
class HabitHealthScopeVerdict:
    """Typed privacy verdict consumed by runtime, API, and tests."""

    allowed: bool
    use: HabitHealthUse
    scope: HabitHealthScope | None
    reasons: tuple[str, ...]
    review_visible: bool = True
    export_visible: bool = True
    delete_visible: bool = True

    def to_dict(self) -> dict[str, Any]:
        """Execute the to dict operation.

        Returns:
            dict[str, Any] value produced by to_dict().
        """
        payload = asdict(self)
        payload["use"] = self.use.value
        payload["scope"] = self.scope.value if self.scope else None
        return payload

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"HabitHealthScopeVerdict(allowed={self.allowed!r}, use={self.use!r}, scope={self.scope!r})"


_DOWNSTREAM_USES = {
    HabitHealthUse.SCHEDULING,
    HabitHealthUse.PERSONALIZATION,
    HabitHealthUse.RESOURCE_PLANNING,
    HabitHealthUse.MEMORY_CONTEXT,
}


def evaluate_habit_health_scope(
    policy: HabitHealthScopePolicy | None,
    requested_use: HabitHealthUse | str,
    *,
    requested_scope: HabitHealthScope | str | None = None,
    source_context: str | None = None,
    consent_refs: tuple[str, ...] | list[str] | None = None,
    provenance_ref: str | None = None,
    downstream_contract_ref: str = "",
) -> HabitHealthScopeVerdict:
    """Return a fail-closed verdict for a habit-health use request.

    Args:
        policy: Policy value consumed by evaluate_habit_health_scope().
        requested_use: Request object sent through the operation.
        requested_scope: Request object sent through the operation.
        source_context: Source object or text processed by the operation.
        consent_refs: Consent refs value consumed by evaluate_habit_health_scope().
        provenance_ref: Provenance ref value consumed by evaluate_habit_health_scope().
        downstream_contract_ref: Downstream contract ref value consumed by evaluate_habit_health_scope().

    Returns:
        HabitHealthScopeVerdict value produced by evaluate_habit_health_scope().
    """
    use = _coerce_use(requested_use)
    if use is None:
        return HabitHealthScopeVerdict(False, HabitHealthUse.REVIEW, None, ("use-unknown",))
    if policy is None:
        return _deny(use, None, "policy-missing")

    reasons: list[str] = []
    scope = _coerce_scope(requested_scope) if requested_scope is not None else None
    if scope is None:
        scopes = policy.normalized_scopes()
        scope = scopes[0] if len(scopes) == 1 else None
    if scope is None or scope is HabitHealthScope.UNKNOWN:
        reasons.append("scope-unknown")
    elif scope not in policy.normalized_scopes():
        reasons.append("scope-not-allowed")

    effective_consent = tuple(consent_refs if consent_refs is not None else policy.consent_refs)
    effective_source = source_context if source_context is not None else policy.source_context
    effective_provenance = provenance_ref if provenance_ref is not None else policy.provenance_ref
    if not tuple(ref for ref in effective_consent if str(ref).strip()):
        reasons.append("consent-missing")
    if not str(effective_source or "").strip():
        reasons.append("source-context-missing")
    if not str(effective_provenance or "").strip():
        reasons.append("provenance-missing")
    if use not in policy.normalized_uses():
        reasons.append("use-not-allowed")
    if use in _DOWNSTREAM_USES:
        contracts = tuple(ref for ref in policy.downstream_contract_refs if str(ref).strip())
        if downstream_contract_ref.strip():
            contracts = (*contracts, downstream_contract_ref)
        if not contracts:
            reasons.append("downstream-contract-missing")
    if use is HabitHealthUse.MEMORY_CONTEXT and not policy.memory_scope.strip():
        reasons.append("memory-scope-unknown")
    if policy.retention_days <= 0:
        reasons.append("retention-invalid")
    if policy.sensitive and not policy.local_only:
        reasons.append("sensitive-scope-not-local")

    unique_reasons = tuple(dict.fromkeys(reasons))
    if unique_reasons:
        return HabitHealthScopeVerdict(False, use, scope, unique_reasons)
    return HabitHealthScopeVerdict(True, use, scope, ("allowed",))


def policy_from_payload(payload: dict[str, Any]) -> HabitHealthScopePolicy:
    """Build a policy from an API/runtime payload without trusting omitted fields."""
    return HabitHealthScopePolicy(
        user_id=str(payload.get("user_id", "")),
        allowed_scopes=tuple(
            str(item) for item in payload.get("allowed_scopes", (payload.get("scope", ""),)) if str(item).strip()
        ),
        consent_refs=tuple(str(item) for item in payload.get("consent_refs", ()) if str(item).strip()),
        provenance_ref=str(payload.get("provenance_ref", "")),
        source_context=str(payload.get("source_context", "")),
        allowed_downstream_uses=tuple(
            str(item) for item in payload.get("allowed_downstream_uses", (HabitHealthUse.STORE.value,))
        ),
        downstream_contract_refs=tuple(
            str(item) for item in payload.get("downstream_contract_refs", ()) if str(item).strip()
        ),
        memory_scope=str(payload.get("memory_scope", "")),
        retention_days=int(payload.get("retention_days", 90)),
        local_only=bool(payload.get("local_only", True)),
        sensitive=bool(payload.get("sensitive")),
    )


def _coerce_scope(value: HabitHealthScope | str | None) -> HabitHealthScope:
    raw_value = value.value if isinstance(value, Enum) else value
    try:
        return value if isinstance(value, HabitHealthScope) else HabitHealthScope(raw_value)
    except ValueError:
        logger.warning("Handled recoverable failure before fallback.", exc_info=True)
        return HabitHealthScope.UNKNOWN


def _coerce_use(value: HabitHealthUse | str) -> HabitHealthUse | None:
    raw_value = value.value if isinstance(value, Enum) else value
    try:
        return value if isinstance(value, HabitHealthUse) else HabitHealthUse(raw_value)
    except ValueError:
        logger.warning("Handled recoverable failure before fallback.", exc_info=True)
        return None


def _deny(use: HabitHealthUse, scope: HabitHealthScope | None, reason: str) -> HabitHealthScopeVerdict:
    return HabitHealthScopeVerdict(False, use, scope, (reason,))


__all__ = [
    "HabitHealthScopePolicy",
    "HabitHealthScopeVerdict",
    "HabitHealthUse",
    "evaluate_habit_health_scope",
    "policy_from_payload",
]
