"""Fail-closed Workbench policy explainability service.

No I/O occurs at import time. Explanation calls may read upstream workbench
policy, capability-pack, source-card, and tool-card state. Unknown or
unreadable policy state returns denied explanations instead of default allow.
The service explains permission only; it never executes tools, models, MCP
servers, exports, runtimes, or other requested actions.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

from vetinari.workbench.capability_packs import CapabilityPackError, CapabilityPackService
from vetinari.workbench.gateway_policy import GatewayPolicyError, WorkbenchGatewayPolicy, get_workbench_gateway_policy
from vetinari.workbench.source_cards import SourceCardLibrary, SourceCardLibraryError, evaluate_freshness
from vetinari.workbench.tool_cards import ToolCardLibrary, ToolCardLibraryError

logger = logging.getLogger(__name__)


_ID_RE = re.compile(r"[A-Za-z0-9_.:-]{1,128}")
_TRAVERSAL_MARKERS = ("/", "\\", "..", "\x00")


class PolicyExplainabilityError(RuntimeError):
    """Raised when callers request exception semantics for corrupt inputs."""


@dataclass(frozen=True, slots=True)
class ActionExplainabilityRequest:
    """Request to explain whether a Workbench action is allowed before use."""

    project_id: str
    action_kind: str
    subject_id: str
    policy_profile_id: str
    tool_card_id: str | None = None
    source_card_id: str | None = None
    capability_pack_id: str | None = None
    trace_id: str | None = None
    run_id: str | None = None
    budget_scope: str | None = None
    requested_by: str | None = None

    def __post_init__(self) -> None:
        for field_name in ("project_id", "action_kind", "subject_id", "policy_profile_id"):
            _validate_identifier(getattr(self, field_name), field_name)
        for field_name in (
            "tool_card_id",
            "source_card_id",
            "capability_pack_id",
            "trace_id",
            "run_id",
            "budget_scope",
            "requested_by",
        ):
            value = getattr(self, field_name)
            if value is not None:
                _validate_identifier(value, field_name)

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"ActionExplainabilityRequest(project_id={self.project_id!r}, action_kind={self.action_kind!r}, subject_id={self.subject_id!r})"


@dataclass(frozen=True, slots=True)
class ExposureSummary:
    """Explicit exposure fields shown before an action is used."""

    secret_exposure: str
    file_exposure: str
    network_exposure: str
    credential_posture: str
    locality: str
    caveats: tuple[str, ...] = ()

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"ExposureSummary(secret_exposure={self.secret_exposure!r}, file_exposure={self.file_exposure!r}, network_exposure={self.network_exposure!r})"


@dataclass(frozen=True, slots=True)
class BudgetSummary:
    """Budget policy state relevant to the action."""

    scope: str
    policy_name: str
    limit: str
    remaining: str
    failure_behavior: str

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"BudgetSummary(scope={self.scope!r}, policy_name={self.policy_name!r}, limit={self.limit!r})"


@dataclass(frozen=True, slots=True)
class TraceSummary:
    """Trace and receipt behavior disclosed before action use."""

    trace_id: str | None
    receipt_kind: str
    will_record: bool
    retention_note: str

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"TraceSummary(trace_id={self.trace_id!r}, receipt_kind={self.receipt_kind!r}, will_record={self.will_record!r})"


@dataclass(frozen=True, slots=True)
class PolicyExplanation:
    """Complete policy explanation returned to API and UI consumers."""

    allowed: bool
    policy_id: str
    policy_source: str
    decision_kind: str
    reasons: tuple[str, ...]
    denial_reasons: tuple[str, ...]
    exposures: ExposureSummary
    budget: BudgetSummary
    trace: TraceSummary
    failure_behavior: str
    source_caveats: tuple[str, ...] = ()
    tool_caveats: tuple[str, ...] = ()
    capability_pack_status: str = "not_requested"
    degraded: bool = False

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"PolicyExplanation(allowed={self.allowed!r}, policy_id={self.policy_id!r}, policy_source={self.policy_source!r})"


class PolicyExplainabilityService:
    """Explain Workbench action policy without executing the action."""

    def __init__(
        self,
        *,
        gateway_policy: WorkbenchGatewayPolicy | None,
        capability_service: CapabilityPackService | None,
        source_library: SourceCardLibrary | None,
        tool_library: ToolCardLibrary | None,
    ) -> None:
        self.gateway_policy = gateway_policy
        self.capability_service = capability_service
        self.source_library = source_library
        self.tool_library = tool_library

    def explain_action(self, request: ActionExplainabilityRequest) -> PolicyExplanation:
        """Return a fail-closed explanation for one proposed action.

        Returns:
            PolicyExplanation value produced by explain_action().
        """
        reasons: list[str] = []
        denial_reasons: list[str] = []
        source_caveats: list[str] = []
        tool_caveats: list[str] = []
        capability_status = "not_requested"
        exposure_fields = _default_exposure_fields()
        exposure_caveats: list[str] = []
        degraded = False

        profiles = self._profiles_or_deny(denial_reasons)
        profile = _find_profile(profiles, request.policy_profile_id)
        if profile is None:
            denial_reasons.append(f"gateway policy profile {request.policy_profile_id!r} unavailable")
            degraded = True
        else:
            reasons.append(f"gateway policy profile {request.policy_profile_id!r} permits explanation review")

        budget = self._budget_summary(profile, request, denial_reasons)
        if profile is None or budget.limit == "unavailable":
            degraded = True

        if request.capability_pack_id is not None:
            capability_status, capability_degraded = self._merge_capability_summary(
                request,
                denial_reasons,
                exposure_fields,
                exposure_caveats,
            )
            degraded = degraded or capability_degraded

        if request.source_card_id is not None:
            degraded = (
                self._merge_source_summary(
                    request,
                    denial_reasons,
                    exposure_fields,
                    source_caveats,
                    exposure_caveats,
                )
                or degraded
            )

        if request.tool_card_id is not None:
            degraded = (
                self._merge_tool_summary(
                    request,
                    denial_reasons,
                    exposure_fields,
                    tool_caveats,
                    exposure_caveats,
                )
                or degraded
            )

        if not (request.capability_pack_id or request.source_card_id or request.tool_card_id):
            denial_reasons.append("no capability, source, or tool policy evidence supplied")
            degraded = True

        allowed = not denial_reasons
        if allowed:
            reasons.append("all requested policy evidence was readable before use")

        return _policy_explanation(
            request=request,
            profile_available=profile is not None,
            allowed=allowed,
            reasons=reasons,
            denial_reasons=denial_reasons,
            exposure_fields=exposure_fields,
            exposure_caveats=exposure_caveats,
            budget=budget,
            source_caveats=source_caveats,
            tool_caveats=tool_caveats,
            capability_status=capability_status,
            degraded=degraded,
        )

    def _merge_capability_summary(
        self,
        request: ActionExplainabilityRequest,
        denial_reasons: list[str],
        exposure_fields: dict[str, str],
        exposure_caveats: list[str],
    ) -> tuple[str, bool]:
        capability_status, capability_exposures = self._explain_capability_pack(request, denial_reasons)
        _merge_exposure_fields(exposure_fields, capability_exposures)
        exposure_caveats.extend(capability_exposures.get("caveats", ()))
        return capability_status, capability_status != "trusted"

    def _merge_source_summary(
        self,
        request: ActionExplainabilityRequest,
        denial_reasons: list[str],
        exposure_fields: dict[str, str],
        source_caveats: list[str],
        exposure_caveats: list[str],
    ) -> bool:
        source_summary = self._explain_source_card(request, denial_reasons)
        source_caveats.extend(source_summary.get("caveats", ()))
        _merge_exposure_fields(exposure_fields, source_summary)
        exposure_caveats.extend(source_summary.get("caveats", ()))
        return bool(source_summary.get("degraded"))

    def _merge_tool_summary(
        self,
        request: ActionExplainabilityRequest,
        denial_reasons: list[str],
        exposure_fields: dict[str, str],
        tool_caveats: list[str],
        exposure_caveats: list[str],
    ) -> bool:
        tool_summary = self._explain_tool_card(request, denial_reasons)
        tool_caveats.extend(tool_summary.get("caveats", ()))
        _merge_exposure_fields(exposure_fields, tool_summary)
        exposure_caveats.extend(tool_summary.get("caveats", ()))
        return bool(tool_summary.get("degraded"))

    def _profiles_or_deny(self, denial_reasons: list[str]) -> list[dict[str, Any]]:
        if self.gateway_policy is None:
            denial_reasons.append("gateway policy state unavailable")
            return []
        try:
            profiles = self.gateway_policy.list_active_profiles()
        except (GatewayPolicyError, OSError, RuntimeError, ValueError, AttributeError, TypeError) as exc:
            logger.warning("Handled recoverable failure before fallback.", exc_info=True)
            denial_reasons.append(f"gateway policy state unreadable: {exc}")
            return []
        if not profiles:
            denial_reasons.append("gateway policy profiles unavailable")
        return profiles

    @staticmethod
    def _budget_summary(
        profile: dict[str, Any] | None,
        request: ActionExplainabilityRequest,
        denial_reasons: list[str],
    ) -> BudgetSummary:
        scope = request.budget_scope or "default"
        if profile is None:
            denial_reasons.append("budget policy unavailable because gateway profile is unavailable")
            return BudgetSummary(
                scope=scope,
                policy_name="unavailable",
                limit="unavailable",
                remaining="unknown",
                failure_behavior="deny before use",
            )
        caps = profile.get("budget_caps")
        if not isinstance(caps, dict) or not caps:
            denial_reasons.append("budget policy unavailable")
            return BudgetSummary(
                scope=scope,
                policy_name=str(profile.get("id", "unknown")),
                limit="unavailable",
                remaining="unknown",
                failure_behavior="deny before use",
            )
        limit = caps.get("daily_usd", caps.get(scope, "declared"))
        return BudgetSummary(
            scope=scope,
            policy_name=str(profile.get("id", "unknown")),
            limit=str(limit),
            remaining="not measured by explainability",
            failure_behavior="deny before use when budget is missing, exceeded, or unreadable",
        )

    def _explain_capability_pack(
        self,
        request: ActionExplainabilityRequest,
        denial_reasons: list[str],
    ) -> tuple[str, dict[str, Any]]:
        if self.capability_service is None:
            denial_reasons.append("capability catalog unavailable")
            return "unavailable", {}
        try:
            pack = self.capability_service.get_pack(request.capability_pack_id or "")
            decision = self.capability_service.evaluate_enablement(pack.pack_id)
        except (CapabilityPackError, OSError, RuntimeError, ValueError) as exc:
            logger.warning("Handled recoverable failure before fallback.", exc_info=True)
            denial_reasons.append(f"capability catalog unreadable: {exc}")
            return "unavailable", {}
        if not decision.allowed:
            denial_reasons.extend(f"capability pack denied: {reason}" for reason in decision.reasons)
        return (
            decision.status.value,
            {
                "secret_exposure": pack.credential_posture,
                "file_exposure": ", ".join(pack.permissions or pack.schemas) or "none declared",
                "network_exposure": "remote/API exposure declared"
                if pack.locality in {"remote", "hybrid"}
                else "local only",
                "credential_posture": pack.credential_posture,
                "locality": pack.locality,
                "caveats": tuple(pack.known_limitations),
            },
        )

    def _explain_source_card(self, request: ActionExplainabilityRequest, denial_reasons: list[str]) -> dict[str, Any]:
        if self.source_library is None:
            denial_reasons.append("source-card library unavailable")
            return {"degraded": True}
        try:
            card = self.source_library.get_card(
                project_id=request.project_id, source_card_id=request.source_card_id or ""
            )
        except (SourceCardLibraryError, OSError, RuntimeError, ValueError) as exc:
            logger.warning("Handled recoverable failure before fallback.", exc_info=True)
            denial_reasons.append(f"source-card state unreadable: {exc}")
            return {"degraded": True}
        if card is None:
            denial_reasons.append(f"source card {request.source_card_id!r} not found")
            return {"degraded": True}
        freshness = evaluate_freshness(card)
        if not freshness.passed:
            denial_reasons.append(f"source freshness failed: {freshness.reason}")
        if not card.provenance:
            denial_reasons.append("source provenance missing")
        return {
            "degraded": not freshness.passed or not bool(card.provenance),
            "credential_posture": card.credential_exposure,
            "caveats": tuple(card.caveats),
        }

    def _explain_tool_card(self, request: ActionExplainabilityRequest, denial_reasons: list[str]) -> dict[str, Any]:
        if self.tool_library is None:
            denial_reasons.append("tool-card library unavailable")
            return {"degraded": True}
        try:
            card = self.tool_library.get_card(project_id=request.project_id, tool_card_id=request.tool_card_id or "")
        except (ToolCardLibraryError, SourceCardLibraryError, OSError, RuntimeError, ValueError) as exc:
            logger.warning("Handled recoverable failure before fallback.", exc_info=True)
            denial_reasons.append(f"tool-card state unreadable: {exc}")
            return {"degraded": True}
        if card is None:
            denial_reasons.append(f"tool card {request.tool_card_id!r} not found")
            return {"degraded": True}
        if not card.source_card_ids:
            denial_reasons.append("tool card has no bound source cards")
        network_exposure = (
            "network/API exposure declared"
            if card.kind.value in {"http_caller", "mcp_invocation", "web_scraper"}
            else "none declared"
        )
        file_exposure = (
            "dataset or local file access declared"
            if card.kind.value in {"dataset_query", "local_function"}
            else "none declared"
        )
        return {
            "degraded": not bool(card.source_card_ids),
            "network_exposure": network_exposure,
            "file_exposure": file_exposure,
            "caveats": tuple(card.safety_caveats),
        }


def _default_exposure_fields() -> dict[str, str]:
    return {
        "secret_exposure": "none declared",
        "file_exposure": "none declared",
        "network_exposure": "none declared",
        "credential_posture": "none declared",
        "locality": "unknown",
    }


def _policy_explanation(
    *,
    request: ActionExplainabilityRequest,
    profile_available: bool,
    allowed: bool,
    reasons: list[str],
    denial_reasons: list[str],
    exposure_fields: dict[str, str],
    exposure_caveats: list[str],
    budget: BudgetSummary,
    source_caveats: list[str],
    tool_caveats: list[str],
    capability_status: str,
    degraded: bool,
) -> PolicyExplanation:
    trace = TraceSummary(
        trace_id=request.trace_id,
        receipt_kind="policy_explanation",
        will_record=True,
        retention_note="downstream execution must record the policy explanation or policy decision receipt",
    )
    return PolicyExplanation(
        allowed=allowed,
        policy_id=request.policy_profile_id if profile_available else "unavailable",
        policy_source="workbench_gateway_policy",
        decision_kind="allow" if allowed else "deny",
        reasons=tuple(reasons),
        denial_reasons=tuple(denial_reasons),
        exposures=ExposureSummary(
            secret_exposure=exposure_fields["secret_exposure"],
            file_exposure=exposure_fields["file_exposure"],
            network_exposure=exposure_fields["network_exposure"],
            credential_posture=exposure_fields["credential_posture"],
            locality=exposure_fields["locality"],
            caveats=tuple(dict.fromkeys(exposure_caveats)),
        ),
        budget=budget,
        trace=trace,
        failure_behavior="deny-before-use; no action is executed by explainability",
        source_caveats=tuple(dict.fromkeys(source_caveats)),
        tool_caveats=tuple(dict.fromkeys(tool_caveats)),
        capability_pack_status=capability_status,
        degraded=degraded,
    )


def get_policy_explainability_service() -> PolicyExplainabilityService:
    """Construct a policy explainability service with live dependencies.

    Returns:
        Resolved policy explainability service value.
    """
    source_library = SourceCardLibrary()
    return PolicyExplainabilityService(
        gateway_policy=get_workbench_gateway_policy(),
        capability_service=CapabilityPackService(),
        source_library=source_library,
        tool_library=ToolCardLibrary(source_library=source_library),
    )


def explain_action_allowed(
    request: ActionExplainabilityRequest,
    service: PolicyExplainabilityService | None = None,
) -> PolicyExplanation:
    """Explain whether an action is allowed using the provided or live service."""
    return (service or get_policy_explainability_service()).explain_action(request)


def _validate_identifier(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be non-empty")
    if len(value) > 128 or _ID_RE.fullmatch(value) is None:
        raise ValueError(f"{field_name} contains forbidden characters")
    if any(marker in value for marker in _TRAVERSAL_MARKERS):
        raise ValueError(f"{field_name} contains traversal markers")


def _find_profile(profiles: list[dict[str, Any]], profile_id: str) -> dict[str, Any] | None:
    for profile in profiles:
        if str(profile.get("id")) == profile_id:
            return profile
    return None


def _merge_exposure_fields(target: dict[str, str], updates: dict[str, Any]) -> None:
    for key in ("secret_exposure", "file_exposure", "network_exposure", "credential_posture", "locality"):
        value = updates.get(key)
        if (
            isinstance(value, str)
            and value
            and value != "none declared"
            and target[key] in {"none declared", "unknown"}
        ):
            target[key] = value


__all__ = [
    "ActionExplainabilityRequest",
    "BudgetSummary",
    "ExposureSummary",
    "PolicyExplainabilityError",
    "PolicyExplainabilityService",
    "PolicyExplanation",
    "TraceSummary",
    "explain_action_allowed",
    "get_policy_explainability_service",
]
