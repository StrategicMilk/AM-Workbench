"""Channel command authorization and activity records."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from vetinari.workbench.approval_chain import ApprovalChainOutcome
from vetinari.workbench.remote_control.contracts import RemoteIntent
from vetinari.workbench.remote_control.service import RemoteControlService

from .approvals import route_channel_approval_request
from .contracts import (
    SCHEMA_VERSION,
    ChannelBlockedReason,
    ChannelCapability,
    ChannelCommandPolicy,
    ChannelDeliveryRequest,
    ChannelDeliveryResult,
    ChannelDeliveryState,
    ChannelHubConfig,
    ChannelRedactionState,
)
from .delivery import build_channel_delivery_envelope
from .registry import resolve_channel_definition


def authorize_channel_command(
    request: ChannelDeliveryRequest,
    *,
    config: ChannelHubConfig | str | None = None,
    remote_intent: RemoteIntent | object | None = None,
    remote_service: RemoteControlService | Any | None = None,
    approval_resolver_provider: Any = None,
) -> dict[str, Any]:
    """Execute the authorize channel command operation.

    Returns:
        dict[str, Any] value produced by authorize_channel_command().
    """
    resolution = resolve_channel_definition(config, request.channel_id)
    if not resolution.allowed or resolution.definition is None:
        return _auth(False, resolution.blocked_reason or ChannelBlockedReason.CHANNEL_UNKNOWN, resolution.summary)
    if ChannelCapability.COMMAND not in resolution.definition.capabilities:
        return _auth(False, ChannelBlockedReason.DELIVERY_UNAUTHORIZED, "channel does not declare command capability")
    policy = resolution.definition.command_authorization_policy
    remote_decision = None
    approval = None
    if policy in {ChannelCommandPolicy.REMOTE_INTENT_REQUIRED, ChannelCommandPolicy.REMOTE_INTENT_AND_APPROVAL_CHAIN}:
        if remote_intent is None:
            return _auth(False, ChannelBlockedReason.REMOTE_INTENT_MISSING, "remote intent evidence is required")
        service = remote_service or RemoteControlService()
        remote_decision = service.evaluate_intent(remote_intent)
        if not remote_decision.allowed:
            return _auth(False, ChannelBlockedReason.REMOTE_INTENT_DENIED, remote_decision.summary)
    if policy in {ChannelCommandPolicy.APPROVAL_CHAIN_REQUIRED, ChannelCommandPolicy.REMOTE_INTENT_AND_APPROVAL_CHAIN}:
        kwargs: dict[str, Any] = {}
        if approval_resolver_provider is not None:
            kwargs["resolver_provider"] = approval_resolver_provider
        approval = route_channel_approval_request(
            channel_id=request.channel_id,
            run_id=request.run_id,
            actor_id=request.actor_id,
            action_id=request.action_id,
            action_type=request.action_type,
            action_fingerprint=request.action_fingerprint,
            summary=request.summary,
            **kwargs,
        )
        if approval.get("outcome") != ApprovalChainOutcome.ALLOW.value or not approval.get("allowed"):
            reason = (
                ChannelBlockedReason.APPROVAL_REQUIRED
                if approval.get("human_approval_required")
                else ChannelBlockedReason.APPROVAL_DENIED
            )
            return _auth(False, reason, "approval-chain decision did not allow command", approval=approval)
    remote_payload = None
    if remote_decision is not None:
        remote_payload = {"value": remote_decision.value.value, "summary": remote_decision.summary}
    return _auth(True, "", "command authorized", approval=approval, remote_decision=remote_payload)


def route_channel_command(
    request: ChannelDeliveryRequest,
    *,
    config: ChannelHubConfig | str | None = None,
    remote_intent: RemoteIntent | object | None = None,
    remote_service: RemoteControlService | Any | None = None,
    approval_resolver_provider: Any = None,
) -> ChannelDeliveryResult:
    """Execute the route channel command operation.

    Returns:
        Outcome produced by route_channel_command().
    """
    authorization = authorize_channel_command(
        request,
        config=config,
        remote_intent=remote_intent,
        remote_service=remote_service,
        approval_resolver_provider=approval_resolver_provider,
    )
    if not authorization["authorized"]:
        return ChannelDeliveryResult(
            schema_version=SCHEMA_VERSION,
            channel_id=request.channel_id,
            state=ChannelDeliveryState.BLOCKED,
            delivered=False,
            blocked_reason=authorization["blocked_reason"],
            redaction_applied=ChannelRedactionState.NONE,
            envelope={
                "schema_version": SCHEMA_VERSION,
                "channel_id": request.channel_id,
                "authorization": authorization,
            },
            activity=build_channel_activity_record(
                request=request, state=ChannelDeliveryState.BLOCKED, authorization=authorization
            ),
        )
    delivery_kwargs: dict[str, Any] = {}
    if approval_resolver_provider is not None:
        delivery_kwargs["approval_resolver_provider"] = approval_resolver_provider
    result = build_channel_delivery_envelope(request, config=config, **delivery_kwargs)
    return ChannelDeliveryResult(
        schema_version=result.schema_version,
        channel_id=result.channel_id,
        state=result.state,
        delivered=result.delivered,
        blocked_reason=result.blocked_reason,
        redaction_applied=result.redaction_applied,
        envelope={**result.envelope, "authorization": authorization},
        activity=build_channel_activity_record(request=request, result=result, authorization=authorization),
    )


def build_channel_activity_record(
    *,
    request: ChannelDeliveryRequest,
    result: ChannelDeliveryResult | None = None,
    state: ChannelDeliveryState | str | None = None,
    authorization: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Execute the build channel activity record operation.

    Returns:
        Newly constructed channel activity record value.
    """
    selected_state = state or (result.state if result is not None else ChannelDeliveryState.BLOCKED)
    state_value = selected_state.value if hasattr(selected_state, "value") else str(selected_state)
    blocked_reason = ""
    redaction = ChannelRedactionState.NONE.value
    delivered = False
    if result is not None:
        blocked_reason = getattr(result.blocked_reason, "value", result.blocked_reason) or ""
        redaction = getattr(result.redaction_applied, "value", result.redaction_applied)
        delivered = result.delivered
    if authorization and authorization.get("blocked_reason"):
        blocked_reason = str(authorization["blocked_reason"])
    return {
        "schema_version": SCHEMA_VERSION,
        "activity_id": f"channel:{request.channel_id}:{request.run_id}:{request.action_id}",
        "channel_id": request.channel_id,
        "run_id": request.run_id,
        "actor_id": request.actor_id,
        "action_id": request.action_id,
        "state": state_value,
        "delivered": delivered,
        "blocked_reason": blocked_reason,
        "redaction_applied": redaction,
        "approval_required": blocked_reason == ChannelBlockedReason.APPROVAL_REQUIRED.value,
        "recorded_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "authorization": authorization or {},
    }


def _auth(authorized: bool, reason: ChannelBlockedReason | str, summary: str, **extra: Any) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "authorized": authorized,
        "blocked_reason": getattr(reason, "value", reason),
        "summary": summary,
        **{key: value for key, value in extra.items() if value is not None},
    }
