"""Bounded, permission-aware network measurement interfaces."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

from vetinari.workbench.network.contracts import NetworkEvidenceStatus, NetworkObservation, NetworkSignalKind
from vetinari.workbench.network.redaction import assert_redacted, redact_network_evidence


class NetworkProbe(Protocol):
    """Read-only probe interface. Implementations must not mutate host networking."""

    def collect(self) -> tuple[NetworkObservation, ...]:
        """Collect bounded observations."""


@dataclass(frozen=True, slots=True)
class StaticNetworkProbe:
    """Deterministic probe used by tests and offline Workbench panels."""

    observations: tuple[NetworkObservation, ...] = ()
    permission_granted: bool = True

    def collect(self) -> tuple[NetworkObservation, ...]:
        """Execute the collect operation.

        Returns:
            tuple[NetworkObservation, ...] value produced by collect().
        """
        if not self.permission_granted:
            return (
                NetworkObservation(
                    kind=NetworkSignalKind.CONNECTION_CLASS,
                    status=NetworkEvidenceStatus.BLOCKED,
                    value="permission-denied",
                    unit="state",
                    evidence_id="network-probe-permission-denied",
                    measured_at_utc=_now(),
                ),
            )
        redacted = []
        for observation in self.observations:
            payload = redact_network_evidence(observation.to_dict())
            assert_redacted(payload)
            redacted.append(NetworkObservation(**payload))
        return tuple(redacted)


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


__all__ = ["NetworkProbe", "StaticNetworkProbe"]
