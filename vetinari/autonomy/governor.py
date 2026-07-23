"""Autonomy Governor: five-level policy engine for autonomous action gating.

Central policy engine that decides whether an action can proceed autonomously,
needs human approval, or should be blocked. Every autonomous action in the
system routes through ``governor.request_permission()`` before executing.

Levels: L0 (Manual) -> L1 (Suggest) -> L2 (Act and Report) -> L3 (Act and Log)
-> L4 (Full Auto). Policy is loaded from ``config/autonomy_policies.yaml``.

This module preserves the public import surface for the autonomy governor while
the cohesive implementation responsibilities live in ``governor_*`` helpers.
"""

from __future__ import annotations

import threading
from collections import defaultdict
from pathlib import Path

from vetinari.autonomy.governor_models import (
    ActionPolicy,
    PendingPromotion,
    PermissionResult,
    PromotionSuggestion,
    TrustRecord,
)
from vetinari.autonomy.governor_permissions import _GovernorPermissionMixin
from vetinari.autonomy.governor_policy import _DEFAULT_POLICY_PATH, _GovernorPolicyMixin
from vetinari.autonomy.governor_trust import _GovernorTrustMixin
from vetinari.types import AutonomyLevel, AutonomyMode, DomainCareLevel


class AutonomyGovernor(_GovernorPermissionMixin, _GovernorPolicyMixin, _GovernorTrustMixin):
    """Five-level autonomy policy engine with progressive trust.

    Gates every autonomous action through ``request_permission()``, which
    consults the per-action-type policy to decide approve, deny, or defer.

    Side effects in __init__:
      - Loads policy from YAML file at ``policy_path``.
      - Initializes in-memory trust tracking dictionaries.
    """

    def __init__(self, policy_path: Path | None = None) -> None:
        self._lock = threading.Lock()
        self._policy_path = policy_path or _DEFAULT_POLICY_PATH
        self._policies: dict[str, ActionPolicy] = {}
        self._trust_records: dict[str, TrustRecord] = defaultdict(TrustRecord)
        self._default_level = AutonomyLevel.L1_SUGGEST
        self._autonomy_mode = AutonomyMode.BALANCED
        self._domain_care_levels: dict[str, DomainCareLevel] = {}
        self._pending_promotions: dict[str, PendingPromotion] = {}
        self._vetoed_actions: set[str] = set()
        self._load_policies()


# Module singleton is written by get_governor/reset_governor and protected by _governor_lock.
_governor: AutonomyGovernor | None = None
_governor_lock = threading.Lock()


def get_governor(policy_path: Path | None = None) -> AutonomyGovernor:
    """Get or create the singleton AutonomyGovernor.

    Args:
        policy_path: Optional override for policy YAML path, used in tests.

    Returns:
        The singleton AutonomyGovernor instance.
    """
    global _governor
    if _governor is None:
        with _governor_lock:
            if _governor is None:
                _governor = AutonomyGovernor(policy_path=policy_path)
    return _governor


def reset_governor() -> None:
    """Reset the singleton governor for test isolation."""
    global _governor
    with _governor_lock:
        _governor = None


__all__ = [
    "ActionPolicy",
    "AutonomyGovernor",
    "PendingPromotion",
    "PermissionResult",
    "PromotionSuggestion",
    "TrustRecord",
    "get_governor",
    "reset_governor",
]
