"""Bridge adaptive-tuning friction evidence into kaizen observations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from vetinari.security.redaction import redact_value
from vetinari.workbench.adaptive_tuning.contracts import NormalizedEvidence


def as_kaizen_session_correction(signal: NormalizedEvidence) -> dict[str, Any]:
    """Convert an adaptive_tuning FrictionSignal into a Kaizen session correction.

    Returns:
        dict[str, Any] value produced by as_kaizen_session_correction().
    """
    return {
        "kind": "adaptive_tuning_friction_signal",
        "source": "adaptive_tuning",
        "correction_type": "kaizen_session_correction",
        "signal_id": signal.evidence_id,
        "signal_kind": signal.kind.value,
        "summary": redact_value({"summary": signal.summary})["summary"],
        "scope": signal.scope.to_dict() if signal.scope is not None else None,
        "evidence_refs": redact_value({"evidence_refs": list(signal.evidence_refs)})["evidence_refs"],
        "provenance_ref": redact_value({"provenance_ref": signal.provenance_ref})["provenance_ref"],
        "confidence": signal.confidence,
    }


@dataclass(frozen=True, slots=True)
class KaizenSignalBridge:
    """Forward normalized friction signals to a kaizen aggregator."""

    aggregator: Any

    def emit(self, signal: Any) -> Any:
        """Run emit.

        Returns:
            Any value produced by emit().

        Raises:
            RuntimeError: If validation cannot complete.
        """
        if isinstance(signal, NormalizedEvidence):
            signal = as_kaizen_session_correction(signal)
        signal = redact_value(signal)
        if hasattr(self.aggregator, "record_external_signal"):
            return self.aggregator.record_external_signal(signal)
        if hasattr(self.aggregator, "signals"):
            self.aggregator.signals.append(signal)
            return signal
        raise TypeError("aggregator cannot record external signals")


def forward_friction_signal(signal: Any, aggregator: Any) -> Any:
    """Forward one friction signal through the kaizen bridge.

    Args:
        signal: Signal value consumed by forward_friction_signal().
        aggregator: Aggregator value consumed by forward_friction_signal().

    Returns:
        Any value produced by forward_friction_signal().
    """
    return KaizenSignalBridge(aggregator).emit(signal)


__all__ = ["KaizenSignalBridge", "as_kaizen_session_correction", "forward_friction_signal"]
