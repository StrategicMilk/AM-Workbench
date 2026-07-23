"""Provider feature, credential, rate-limit, and cost governance contracts."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ProviderGovernanceStatus(str, Enum):
    """Admission outcome for a provider/model route."""

    ALLOWED = "allowed"
    DEGRADED = "degraded"
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True)
class ProviderFeatureProfile:
    """Provider/model capabilities and operating limits."""

    provider: str
    model_id: str
    supported_features: tuple[str, ...]
    credential_profile: str
    credential_revoked: bool = False
    local_private: bool = False
    rate_limit_remaining: int = 1
    cost_remaining_usd: float = 1.0

    def __post_init__(self) -> None:
        for field_name in ("provider", "model_id", "credential_profile"):
            _require_text(getattr(self, field_name), field_name)
        object.__setattr__(self, "supported_features", tuple(str(item) for item in self.supported_features))

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"ProviderFeatureProfile(provider={self.provider!r}, model_id={self.model_id!r}, supported_features={self.supported_features!r})"


@dataclass(frozen=True, slots=True)
class ProviderGovernanceRequest:
    """Feature and budget requirements for one run route."""

    requested_features: tuple[str, ...]
    credential_profile: str
    estimated_cost_usd: float = 0.0
    requires_local_private: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "requested_features", tuple(str(item) for item in self.requested_features))
        _require_text(self.credential_profile, "credential_profile")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"ProviderGovernanceRequest(requested_features={self.requested_features!r}, credential_profile={self.credential_profile!r}, estimated_cost_usd={self.estimated_cost_usd!r})"


@dataclass(frozen=True, slots=True)
class ProviderGovernanceDecision:
    """Run-admission decision for provider governance."""

    status: ProviderGovernanceStatus
    allowed: bool
    provider: str
    model_id: str
    reasons: tuple[str, ...]
    evidence_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", ProviderGovernanceStatus(self.status))
        if self.allowed and self.status is not ProviderGovernanceStatus.ALLOWED:
            raise ValueError("allowed provider decisions must use allowed status")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return (
            f"ProviderGovernanceDecision(status={self.status!r}, allowed={self.allowed!r}, provider={self.provider!r})"
        )


def evaluate_provider_governance(
    profile: ProviderFeatureProfile,
    request: ProviderGovernanceRequest,
) -> ProviderGovernanceDecision:
    """Fail closed on unsupported features, credentials, rate limits, and cost.

    Args:
        profile: File path or file-like value consumed by the operation.
        request: Request object sent through the operation.

    Returns:
        ProviderGovernanceDecision value produced by evaluate_provider_governance().
    """
    reasons: list[str] = []
    missing_features = sorted(set(request.requested_features) - set(profile.supported_features))
    reasons.extend(f"unsupported_feature:{feature}" for feature in missing_features)
    if request.requires_local_private and not profile.local_private:
        reasons.append("privacy_posture_unavailable")
    if profile.credential_profile != request.credential_profile:
        reasons.append("credential_profile_mismatch")
    if profile.credential_revoked:
        reasons.append("credential_revoked")
    if profile.rate_limit_remaining < 1:
        reasons.append("rate_limit_exhausted")
    if request.estimated_cost_usd > profile.cost_remaining_usd:
        reasons.append("cost_budget_exceeded")
    if not reasons:
        return ProviderGovernanceDecision(
            ProviderGovernanceStatus.ALLOWED,
            True,
            profile.provider,
            profile.model_id,
            ("provider-governance-allowed",),
            (f"provider:{profile.provider}:{profile.model_id}", request.credential_profile),
        )
    status = (
        ProviderGovernanceStatus.DEGRADED
        if all(reason.startswith("unsupported_feature:") for reason in reasons)
        else (ProviderGovernanceStatus.BLOCKED)
    )
    return ProviderGovernanceDecision(status, False, profile.provider, profile.model_id, tuple(reasons), ())


def _require_text(value: object, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be non-empty")


__all__ = [
    "ProviderFeatureProfile",
    "ProviderGovernanceDecision",
    "ProviderGovernanceRequest",
    "ProviderGovernanceStatus",
    "evaluate_provider_governance",
]
