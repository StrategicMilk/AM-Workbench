"""Channel delivery envelopes and media redaction."""

from __future__ import annotations

import copy
import logging
import re
from collections.abc import Callable
from typing import Any

from vetinari.security.redaction import REDACTED_PATH, redact_text
from vetinari.workbench.approval_chain import get_workbench_approval_chain

from .contracts import (
    SCHEMA_VERSION,
    ChannelApprovalMode,
    ChannelBlockedReason,
    ChannelCapability,
    ChannelDeliveryRequest,
    ChannelDeliveryResult,
    ChannelDeliveryState,
    ChannelHubConfig,
    ChannelRedactionPolicy,
    ChannelRedactionState,
)
from .registry import resolve_channel_definition

ApprovalResolverProvider = Callable[[], Any]

logger = logging.getLogger(__name__)

_SECRET_KEY_PARTS = (
    "secret",
    "token",
    "password",
    "passwd",
    "api_key",
    "apikey",
    "credential",
    "private_key",
    "authorization",
    "api-key",
    "private-key",
)
_SECRET_VALUE_RE = re.compile(
    r"(?i)(bearer\s+[A-Za-z0-9._~+/=-]{8,}|sk-[A-Za-z0-9]{8,}|gh[pousr]_[A-Za-z0-9_]{8,}|AKIA[0-9A-Z]{12,})"
)


def redact_channel_media(
    payload: dict[str, Any],
    media: tuple[Any, ...] = (),
    *,
    policy: ChannelRedactionPolicy | None = None,
) -> tuple[dict[str, Any], tuple[dict[str, Any], ...], bool]:
    """Execute the redact channel media operation.

    Args:
        payload: Payload data validated or transformed by the operation.
        media: Media value consumed by redact_channel_media().
        policy: Policy value consumed by redact_channel_media().

    Returns:
        tuple[dict[str, Any], tuple[dict[str, Any], ...], bool] value produced by redact_channel_media().
    """
    active_policy = policy or ChannelRedactionPolicy()
    redacted_payload, payload_changed = _redact_value(copy.deepcopy(payload), active_policy, key_hint="")
    redacted_media: list[dict[str, Any]] = []
    media_changed = False
    for item in media:
        item_dict = item.to_dict() if hasattr(item, "to_dict") else dict(item)
        metadata = dict(item_dict.get("metadata", {}))
        clean_metadata = {
            key: value for key, value in metadata.items() if key in active_policy.allowed_attachment_fields
        }
        media_changed = media_changed or clean_metadata != metadata
        clean_payload, payload_was_changed = _redact_value(item_dict.get("payload"), active_policy, key_hint="payload")
        media_changed = media_changed or payload_was_changed
        redacted_media.append({
            "media_id": str(item_dict.get("media_id", "")),
            "media_type": str(item_dict.get("media_type", "")),
            "payload": clean_payload,
            "metadata": clean_metadata,
        })
    return redacted_payload, tuple(redacted_media), payload_changed or media_changed


def build_channel_delivery_envelope(
    request: ChannelDeliveryRequest,
    *,
    config: ChannelHubConfig | str | None = None,
    approval_resolver_provider: ApprovalResolverProvider = get_workbench_approval_chain,
) -> ChannelDeliveryResult:
    """Execute the build channel delivery envelope operation.

    Returns:
        Newly constructed channel delivery envelope value.
    """
    resolution = resolve_channel_definition(config, request.channel_id)
    if not resolution.allowed or resolution.definition is None:
        return _blocked_result(
            request, resolution.blocked_reason or ChannelBlockedReason.CHANNEL_UNKNOWN, resolution.summary
        )
    if ChannelCapability.DELIVERY not in resolution.definition.capabilities:
        return _blocked_result(
            request,
            ChannelBlockedReason.DELIVERY_UNAUTHORIZED,
            "channel does not declare delivery capability",
        )
    if (
        resolution.definition.approval_policy is ChannelApprovalMode.REQUIRED
        and not request.approval_decision_id.strip()
    ):
        return _blocked_result(
            request, ChannelBlockedReason.APPROVAL_REQUIRED, "approval decision is required for channel delivery"
        )
    if resolution.definition.approval_policy is ChannelApprovalMode.REQUIRED:
        approval_check = _validate_approval_decision(request, approval_resolver_provider)
        if approval_check is not None:
            return approval_check
    redacted_payload, redacted_media, changed = redact_channel_media(
        request.payload,
        request.media,
        policy=resolution.definition.redaction_policy,
    )
    envelope = {
        "schema_version": SCHEMA_VERSION,
        "channel_id": resolution.definition.channel_id,
        "channel_type": resolution.definition.channel_type.value,
        "target": resolution.definition.default_target,
        "run_id": request.run_id,
        "actor_id": request.actor_id,
        "action_id": request.action_id,
        "action_type": request.action_type,
        "summary": request.summary,
        "payload": redacted_payload,
        "media": list(redacted_media),
        "approval_decision_id": request.approval_decision_id,
        "action_fingerprint": request.action_fingerprint,
    }
    return ChannelDeliveryResult(
        schema_version=SCHEMA_VERSION,
        channel_id=resolution.definition.channel_id,
        state=ChannelDeliveryState.REDACTED if changed else ChannelDeliveryState.DELIVERED,
        delivered=True,
        blocked_reason=None,
        redaction_applied=ChannelRedactionState.APPLIED if changed else ChannelRedactionState.NONE,
        envelope=envelope,
    )


def _validate_approval_decision(
    request: ChannelDeliveryRequest,
    approval_resolver_provider: ApprovalResolverProvider,
) -> ChannelDeliveryResult | None:
    try:
        resolver = approval_resolver_provider()
        decision = (
            resolver.lookup_decision(request.approval_decision_id) if hasattr(resolver, "lookup_decision") else None
        )
    except Exception:
        logger.warning(
            "Approval decision lookup failed for channel delivery.",
            extra={"channel_id": request.channel_id, "decision_id": request.approval_decision_id},
            exc_info=True,
        )
        return _blocked_result(
            request,
            ChannelBlockedReason.APPROVAL_UNAVAILABLE,
            "approval decision could not be read for channel delivery",
        )
    if decision is None:
        return _blocked_result(
            request,
            ChannelBlockedReason.APPROVAL_UNAVAILABLE,
            "approval decision is not available for channel delivery",
        )
    payload = decision.to_dict() if hasattr(decision, "to_dict") else {}
    if not payload.get("allowed", False):
        return _blocked_result(
            request,
            ChannelBlockedReason.APPROVAL_DENIED,
            "approval decision does not allow channel delivery",
        )
    if request.action_fingerprint and str(payload.get("action_fingerprint", "")) != request.action_fingerprint:
        return _blocked_result(
            request,
            ChannelBlockedReason.APPROVAL_DENIED,
            "approval decision fingerprint does not match channel delivery request",
        )
    if str(payload.get("run_id", "")) and str(payload.get("run_id", "")) != request.run_id:
        return _blocked_result(
            request,
            ChannelBlockedReason.APPROVAL_DENIED,
            "approval decision run_id does not match channel delivery request",
        )
    return None


def _blocked_result(
    request: ChannelDeliveryRequest, reason: ChannelBlockedReason | str, summary: str
) -> ChannelDeliveryResult:
    return ChannelDeliveryResult(
        schema_version=SCHEMA_VERSION,
        channel_id=request.channel_id,
        state=ChannelDeliveryState.BLOCKED,
        delivered=False,
        blocked_reason=reason,
        redaction_applied=ChannelRedactionState.NONE,
        envelope={
            "schema_version": SCHEMA_VERSION,
            "channel_id": request.channel_id,
            "run_id": request.run_id,
            "actor_id": request.actor_id,
            "action_id": request.action_id,
            "blocked_summary": summary,
        },
    )


def _redact_value(value: Any, policy: ChannelRedactionPolicy, *, key_hint: str) -> tuple[Any, bool]:
    normalized_key = key_hint.lower().replace("-", "_").replace(".", "_")
    if isinstance(value, bytes):
        return ("<redacted:binary>", True) if policy.redact_binary_values else (value, False)
    if isinstance(value, str):
        if policy.redact_secret_values and any(part in normalized_key for part in _SECRET_KEY_PARTS):
            return "<redacted:secret>", True
        if policy.redact_secret_values and _SECRET_VALUE_RE.search(value):
            return _SECRET_VALUE_RE.sub("<redacted:secret>", value), True
        if policy.redact_local_paths and REDACTED_PATH in redact_text(value):
            return "<redacted:local_path>", True
        return value, False
    if isinstance(value, dict):
        changed = False
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            clean, item_changed = _redact_value(item, policy, key_hint=str(key))
            redacted[str(key)] = clean
            changed = changed or item_changed
        return redacted, changed
    if isinstance(value, (list, tuple)):
        changed = False
        redacted_items = []
        for item in value:
            clean, item_changed = _redact_value(item, policy, key_hint=key_hint)
            redacted_items.append(clean)
            changed = changed or item_changed
        return redacted_items, changed
    return value, False
