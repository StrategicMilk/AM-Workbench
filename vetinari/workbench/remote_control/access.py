"""Tailnet-first access gates for mobile companion control."""

from __future__ import annotations

from typing import Any

from vetinari.workbench.private_ai_appliance import RuntimeCockpitSnapshot, SupportMatrixStatus

from .contracts import (
    RemoteAccessMode,
    RemoteControlDecision,
    RemoteControlDecisionValue,
    RemoteControlFailureReason,
    RemoteIntent,
)


def evaluate_remote_access(
    intent: RemoteIntent | None,
    *,
    request_context: dict[str, Any] | None = None,
    cockpit_snapshot: RuntimeCockpitSnapshot | None = None,
    side_effect_executor: object | None = None,
) -> RemoteControlDecision:
    """Evaluate remote access without executing any desktop side effect.

    Returns:
        RemoteControlDecision value produced by evaluate_remote_access().
    """
    if intent is None:
        return _block(RemoteControlFailureReason.MISSING_IDENTITY, "remote intent is required")
    if side_effect_executor is not None:
        return _block(RemoteControlFailureReason.INJECTED_EXECUTOR_REJECTED, "mobile access cannot receive executors")
    context = request_context or {}
    binding = intent.service_binding
    mode = RemoteAccessMode(binding.access_mode)

    readiness = summarize_remote_access_readiness(cockpit_snapshot)
    if readiness.value is not RemoteControlDecisionValue.ALLOW:
        return readiness

    identity_headers = bool(context.get("identity_headers") or context.get("x_remote_user"))
    trusted_proxy_path = bool(context.get("trusted_proxy_path"))
    source_path = str(context.get("source_path", "")).strip().lower()
    if identity_headers and (not trusted_proxy_path or source_path == "direct_lan"):
        return _block(RemoteControlFailureReason.UNTRUSTED_IDENTITY_PATH, "identity headers require trusted proxy path")

    if mode is RemoteAccessMode.TAILSCALE_SERVE:
        if not trusted_proxy_path:
            return _block(RemoteControlFailureReason.UNTRUSTED_IDENTITY_PATH, "tailscale serve path must be attested")
        if not binding.is_localhost_origin:
            return _block(
                RemoteControlFailureReason.UNSAFE_SERVICE_BINDING, "tailnet serve must bind desktop localhost"
            )
        return RemoteControlDecision(
            RemoteControlDecisionValue.ALLOW,
            "tailnet access path accepted",
            evidence_refs=intent.evidence_refs,
            policy_version=intent.policy_version,
            payload={"access_mode": mode.value, "default_path": True},
        )

    if mode is RemoteAccessMode.CLOUDFLARE_ACCESS:
        if not (
            trusted_proxy_path
            and binding.public_hostname.strip()
            and binding.cloudflare_access_policy_ref.strip()
            and context.get("cloudflare_access_verified") is True
        ):
            return _block(
                RemoteControlFailureReason.CLOUDFLARE_ACCESS_MISSING, "Cloudflare Access evidence is required"
            )
        return RemoteControlDecision(
            RemoteControlDecisionValue.ALLOW,
            "optional Cloudflare Access path accepted",
            evidence_refs=intent.evidence_refs,
            policy_version=intent.policy_version,
            payload={"access_mode": mode.value, "default_path": False},
        )

    return _block(
        RemoteControlFailureReason.UNSAFE_SERVICE_BINDING, "manual advanced paths are not accepted by default"
    )


def summarize_remote_access_readiness(snapshot: RuntimeCockpitSnapshot | None) -> RemoteControlDecision:
    """Map the private-AI runtime cockpit snapshot into remote-control readiness.

    Returns:
        RemoteControlDecision value produced by summarize_remote_access_readiness().
    """
    if snapshot is None:
        return RemoteControlDecision(
            RemoteControlDecisionValue.BLOCK,
            "runtime cockpit unavailable",
            (RemoteControlFailureReason.RUNTIME_UNKNOWN,),
        )
    status = SupportMatrixStatus(snapshot.overall_status)
    runtime_ready = snapshot.runtime.health_status == "ready" and snapshot.runtime.runtime_present is True
    if snapshot.degradation_reasons or snapshot.runtime.health_status == "unknown":
        return RemoteControlDecision(
            RemoteControlDecisionValue.DEGRADED,
            "desktop-local runtime readiness is degraded",
            (RemoteControlFailureReason.RUNTIME_DEGRADED,),
            payload={"degradation_reasons": list(snapshot.degradation_reasons)},
        )
    if status in {SupportMatrixStatus.VALIDATED, SupportMatrixStatus.PROMOTION_ELIGIBLE} and runtime_ready:
        return RemoteControlDecision(RemoteControlDecisionValue.ALLOW, "desktop-local runtime readiness validated")
    if status in {SupportMatrixStatus.DEGRADED, SupportMatrixStatus.EXPERIMENTAL, SupportMatrixStatus.ACTION_REQUIRED}:
        return RemoteControlDecision(
            RemoteControlDecisionValue.DEGRADED,
            "runtime requires operator action",
            (RemoteControlFailureReason.RUNTIME_DEGRADED,),
        )
    return RemoteControlDecision(
        RemoteControlDecisionValue.BLOCK,
        "runtime is not ready for mobile control",
        (RemoteControlFailureReason.RUNTIME_UNKNOWN,),
    )


def _block(reason: RemoteControlFailureReason, summary: str) -> RemoteControlDecision:
    return RemoteControlDecision(RemoteControlDecisionValue.BLOCK, summary, (reason,))


__all__ = [
    "RemoteAccessMode",
    "evaluate_remote_access",
    "summarize_remote_access_readiness",
]
