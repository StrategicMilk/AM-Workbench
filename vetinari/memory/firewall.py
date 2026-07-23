"""Deterministic fail-closed firewall for governed memory promotion.

The firewall returns immutable decisions only. It does not persist those
decisions and does not create a shared state store.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from vetinari.memory.governance import (
    ApprovalState,
    BoundaryClass,
    ConflictStatus,
    MemoryAuthority,
    MemoryDecisionResult,
    MemoryGovernanceDecision,
    MemoryGovernanceError,
    MemoryGovernanceRecord,
    MemoryLifecycleState,
    PolicyState,
    RetentionClass,
    RollbackStatus,
    SourceTrustTier,
    TaintStatus,
    validate_memory_governance_payload,
)

logger = logging.getLogger(__name__)


_ACTIVE_AUTHORITY = frozenset({
    MemoryAuthority.PROMPT,
    MemoryAuthority.PLANNING,
    MemoryAuthority.ROUTING,
    MemoryAuthority.POLICY,
})


class MemoryFirewallError(MemoryGovernanceError):
    """Typed firewall error for invalid lifecycle transition requests."""


@dataclass(frozen=True, slots=True)
class MemoryFirewallDecision(MemoryGovernanceDecision):
    """Firewall-owned immutable decision record."""


class MemoryFirewall:
    """Evaluate whether memory may become prompt/planning/routing authority."""

    def evaluate(
        self,
        record: MemoryGovernanceRecord | Mapping[str, Any],
        *,
        lineage: Any | None = None,
        promotion_gate: Any | None = None,
        policy_profile: Any | None = None,
    ) -> MemoryFirewallDecision:
        """Return a deterministic fail-closed authority decision.

        Returns:
            MemoryFirewallDecision value produced by evaluate().
        """
        parsed = self._parse_record(record)
        if isinstance(parsed, MemoryFirewallDecision):
            return parsed

        blockers = list(self._record_blockers(parsed))
        blockers.extend(
            self._external_blockers(
                parsed, lineage=lineage, promotion_gate=promotion_gate, policy_profile=policy_profile
            )
        )
        unique_blockers = tuple(dict.fromkeys(blockers))
        if unique_blockers:
            return self._decision(
                parsed,
                result=MemoryDecisionResult.BLOCKED,
                state=parsed.state
                if parsed.state is not MemoryLifecycleState.ACTIVE
                else MemoryLifecycleState.QUARANTINED,
                authority=MemoryAuthority.NONE,
                blockers=unique_blockers,
                reason="memory firewall blocked authority promotion",
            )
        return self._decision(
            parsed,
            result=MemoryDecisionResult.ACCEPTED,
            state=MemoryLifecycleState.ACTIVE,
            authority=MemoryAuthority.PROMPT,
            blockers=(),
            reason="memory firewall accepted active prompt authority",
            prompt_eligible=True,
            planning_eligible=True,
            routing_eligible=True,
        )

    def promote(
        self,
        record: MemoryGovernanceRecord | Mapping[str, Any],
        *,
        lineage: Any | None = None,
        promotion_gate: Any | None = None,
        policy_profile: Any | None = None,
    ) -> MemoryFirewallDecision:
        """Promote through the same firewall path used by evaluate."""
        return self.evaluate(record, lineage=lineage, promotion_gate=promotion_gate, policy_profile=policy_profile)

    def quarantine(self, record: MemoryGovernanceRecord | Mapping[str, Any], *, reason: str) -> MemoryFirewallDecision:
        """Return a non-active quarantine decision.

        Returns:
            MemoryFirewallDecision value produced by quarantine().
        """
        parsed = self._require_record(record)
        return self._decision(
            parsed,
            result=MemoryDecisionResult.QUARANTINED,
            state=MemoryLifecycleState.QUARANTINED,
            authority=MemoryAuthority.NONE,
            blockers=("quarantined",),
            reason=reason,
        )

    def supersede(
        self,
        record: MemoryGovernanceRecord | Mapping[str, Any],
        *,
        superseded_by: str,
        reason: str,
    ) -> MemoryFirewallDecision:
        """Return a non-active supersede decision.

        Returns:
            MemoryFirewallDecision value produced by supersede().
        """
        parsed = self._require_record(record)
        return self._decision(
            parsed,
            result=MemoryDecisionResult.SUPERSEDED,
            state=MemoryLifecycleState.SUPERSEDED,
            authority=MemoryAuthority.NONE,
            blockers=("superseded",),
            reason=reason,
            metadata={"superseded_by": superseded_by},
        )

    def tombstone(self, record: MemoryGovernanceRecord | Mapping[str, Any], *, reason: str) -> MemoryFirewallDecision:
        """Return a non-active tombstone decision.

        Returns:
            MemoryFirewallDecision value produced by tombstone().
        """
        parsed = self._require_record(record)
        return self._decision(
            parsed,
            result=MemoryDecisionResult.TOMBSTONED,
            state=MemoryLifecycleState.TOMBSTONED,
            authority=MemoryAuthority.NONE,
            blockers=("tombstoned",),
            reason=reason,
        )

    def forget(self, record: MemoryGovernanceRecord | Mapping[str, Any], *, reason: str) -> MemoryFirewallDecision:
        """Return a non-active forget decision without deleting or persisting state.

        Returns:
            MemoryFirewallDecision value produced by forget().
        """
        parsed = self._require_record(record)
        return self._decision(
            parsed,
            result=MemoryDecisionResult.FORGOTTEN,
            state=MemoryLifecycleState.TOMBSTONED,
            authority=MemoryAuthority.NONE,
            blockers=("forgotten",),
            reason=reason,
        )

    @staticmethod
    def _parse_record(
        record: MemoryGovernanceRecord | Mapping[str, Any],
    ) -> MemoryGovernanceRecord | MemoryFirewallDecision:
        if isinstance(record, MemoryGovernanceRecord):
            return record
        try:
            return validate_memory_governance_payload(record)
        except MemoryGovernanceError as exc:
            logger.warning("Handled recoverable failure before fallback.", exc_info=True)
            return MemoryFirewallDecision(
                memory_id=str(record.get("memory_id", "unknown")) if isinstance(record, Mapping) else "unknown",
                result=MemoryDecisionResult.BLOCKED,
                state=MemoryLifecycleState.QUARANTINED,
                authority=MemoryAuthority.NONE,
                blockers=("invalid_governance_record",),
                prompt_eligible=False,
                planning_eligible=False,
                routing_eligible=False,
                reason=str(exc),
            )

    def _require_record(self, record: MemoryGovernanceRecord | Mapping[str, Any]) -> MemoryGovernanceRecord:
        parsed = self._parse_record(record)
        if isinstance(parsed, MemoryFirewallDecision):
            raise MemoryFirewallError(parsed.reason)
        return parsed

    @staticmethod
    def _record_blockers(record: MemoryGovernanceRecord) -> tuple[str, ...]:
        blockers: list[str] = []
        if record.state is not MemoryLifecycleState.VALIDATED:
            blockers.append("state_not_validated")
        if record.authority in _ACTIVE_AUTHORITY:
            blockers.append("direct_authority_not_allowed")
        if record.source_trust is not SourceTrustTier.TRUSTED:
            blockers.append("source_not_trusted")
        if record.policy_state is not PolicyState.APPROVED:
            blockers.append("policy_not_approved")
        if record.retention is not RetentionClass.RETAINED:
            blockers.append("retention_not_retained")
        if record.boundary not in (BoundaryClass.PUBLIC, BoundaryClass.PRIVATE):
            blockers.append("boundary_not_compatible")
        if record.conflict is not ConflictStatus.CLEAR:
            blockers.append("conflict_not_clear")
        if record.rollback_status is not RollbackStatus.PRESENT:
            blockers.append("rollback_missing")
        if record.approval_state is not ApprovalState.APPROVED:
            blockers.append("approval_missing")
        if record.taint is not TaintStatus.CLEAN:
            blockers.append("taint_not_clean")
        return tuple(blockers)

    @staticmethod
    def _external_blockers(
        record: MemoryGovernanceRecord,
        *,
        lineage: Any | None,
        promotion_gate: Any | None,
        policy_profile: Any | None,
    ) -> tuple[str, ...]:
        blockers: list[str] = []
        if lineage is None and not record.lineage_refs:
            blockers.append("lineage_missing")
        if lineage is False:
            blockers.append("lineage_unreadable")
        if promotion_gate is None:
            blockers.append("promotion_gate_missing")
        elif not _truthy_gate(promotion_gate):
            blockers.append("promotion_gate_failed")
        if policy_profile is None:
            blockers.append("policy_profile_missing")
        elif not _truthy_policy(policy_profile):
            blockers.append("policy_profile_failed")
        return tuple(blockers)

    def _decision(
        self,
        record: MemoryGovernanceRecord,
        *,
        result: MemoryDecisionResult,
        state: MemoryLifecycleState,
        authority: MemoryAuthority,
        blockers: tuple[str, ...],
        reason: str,
        prompt_eligible: bool = False,
        planning_eligible: bool = False,
        routing_eligible: bool = False,
        metadata: Mapping[str, Any] | None = None,
    ) -> MemoryFirewallDecision:
        decision_metadata = {
            "record_state": record.state.value,
            "record_authority": record.authority.value,
            "provenance": dict(record.provenance),
            "scope": record.scope,
            "lineage_refs": list(record.lineage_refs),
        }
        if metadata:
            decision_metadata.update(metadata)
        return MemoryFirewallDecision(
            memory_id=record.memory_id,
            result=result,
            state=state,
            authority=authority,
            blockers=blockers,
            prompt_eligible=prompt_eligible,
            planning_eligible=planning_eligible,
            routing_eligible=routing_eligible,
            reason=reason,
            metadata=decision_metadata,
        )


def _truthy_gate(gate: Any) -> bool:
    if isinstance(gate, Mapping):
        return bool(gate.get("passed"))
    return bool(getattr(gate, "passed", False))


def _truthy_policy(policy_profile: Any) -> bool:
    if isinstance(policy_profile, Mapping):
        return bool(policy_profile.get("approved", policy_profile.get("passed", False)))
    return bool(getattr(policy_profile, "approved", getattr(policy_profile, "passed", False)))


__all__ = [
    "MemoryFirewall",
    "MemoryFirewallDecision",
    "MemoryFirewallError",
]
