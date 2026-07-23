"""Typed request configuration for Workbench shadow undo."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from vetinari.workbench.shadow_undo.contracts import ShadowOperationKind


@dataclass(frozen=True, slots=True)
class ShadowUndoConfig:
    """Typed request for capturing a shadow-undo snapshot."""

    run_id: str
    operation_id: str
    operation_kind: ShadowOperationKind
    summary: str
    risk_domain: str
    policy_verdict_ref: str
    approval_ref: str
    dry_run_evidence_ref: str
    original_run_record_ref: str
    original_run_record_payload: Mapping[str, Any]
    target_path: str | Path = ""
    command_text: str = ""
    cwd_ref: str = ""
    process_ref: str = ""
    automation_shadow_plan_ref: str = ""
    automation_shadow_receipt_ref: str = ""
    shield_decision_ref: str = ""
    manual_recovery_guidance: str = ""
    captured_at_utc: str | None = None

    def __repr__(self) -> str:
        return (
            f"ShadowUndoConfig(run_id={self.run_id!r}, operation_id={self.operation_id!r}, "
            f"operation_kind={self.operation_kind.value!r})"
        )

    @classmethod
    def from_kwargs(cls, values: dict[str, Any]) -> ShadowUndoConfig:
        """Build a config from legacy keyword arguments."""
        return cls(**values)
