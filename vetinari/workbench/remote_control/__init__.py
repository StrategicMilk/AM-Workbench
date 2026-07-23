"""Mobile companion remote-control backend contract."""

from __future__ import annotations

from .access import evaluate_remote_access, summarize_remote_access_readiness
from .contracts import (
    RemoteAccessMode,
    RemoteApproval,
    RemoteControlDecision,
    RemoteControlDecisionValue,
    RemoteControlError,
    RemoteControlFailureReason,
    RemoteDevicePosture,
    RemoteIdentity,
    RemoteIntent,
    RemoteIntentKind,
    RemoteIntentRiskTier,
    RemoteServiceBinding,
)
from .service import RemoteControlService

__all__ = [
    "RemoteAccessMode",
    "RemoteApproval",
    "RemoteControlDecision",
    "RemoteControlDecisionValue",
    "RemoteControlError",
    "RemoteControlFailureReason",
    "RemoteControlService",
    "RemoteDevicePosture",
    "RemoteIdentity",
    "RemoteIntent",
    "RemoteIntentKind",
    "RemoteIntentRiskTier",
    "RemoteServiceBinding",
    "evaluate_remote_access",
    "summarize_remote_access_readiness",
]
