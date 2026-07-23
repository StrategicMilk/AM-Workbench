"""Workbench update safety public API."""

from __future__ import annotations

from typing import Any

from vetinari.workbench.update_safety.channels import load_update_channel_config
from vetinari.workbench.update_safety.contracts import (
    CurrentInstall,
    PublicExportProvenance,
    SkippedVersionRecord,
    SupportBundleBuildResult,
    UpdateArtifact,
    UpdateChannel,
    UpdateIntegrityPolicy,
    UpdateIntegrityState,
    UpdateIntegrityVerdict,
    UpdateManifest,
    UpdateReadiness,
    UpdateReadinessState,
    UpdateSafetyError,
    UpdateSafetyRollbackPlan,
)
from vetinari.workbench.update_safety.integrity import verify_update_integrity
from vetinari.workbench.update_safety.manifest import parse_update_manifest

build_rollback_plan: Any
evaluate_update_readiness: Any
SkippedVersionStore: Any
build_update_status_dependency_snapshot: Any
UpdateSupportBundleBuilder: Any

try:
    from vetinari.workbench.update_safety.rollback import build_rollback_plan as _build_rollback_plan
    from vetinari.workbench.update_safety.service import evaluate_update_readiness as _evaluate_update_readiness
    from vetinari.workbench.update_safety.state import SkippedVersionStore as _SkippedVersionStore
    from vetinari.workbench.update_safety.status_adapter import (
        build_update_status_dependency_snapshot as _build_update_status_dependency_snapshot,
    )
    from vetinari.workbench.update_safety.support_bundle import (
        UpdateSupportBundleBuilder as _UpdateSupportBundleBuilder,
    )

    build_rollback_plan = _build_rollback_plan
    evaluate_update_readiness = _evaluate_update_readiness
    SkippedVersionStore = _SkippedVersionStore
    build_update_status_dependency_snapshot = _build_update_status_dependency_snapshot
    UpdateSupportBundleBuilder = _UpdateSupportBundleBuilder
except ImportError:
    build_rollback_plan = None
    evaluate_update_readiness = None
    SkippedVersionStore = None
    build_update_status_dependency_snapshot = None
    UpdateSupportBundleBuilder = None


__all__ = [
    "CurrentInstall",
    "PublicExportProvenance",
    "SkippedVersionRecord",
    "SkippedVersionStore",
    "SupportBundleBuildResult",
    "UpdateArtifact",
    "UpdateChannel",
    "UpdateIntegrityPolicy",
    "UpdateIntegrityState",
    "UpdateIntegrityVerdict",
    "UpdateManifest",
    "UpdateReadiness",
    "UpdateReadinessState",
    "UpdateSafetyError",
    "UpdateSafetyRollbackPlan",
    "UpdateSupportBundleBuilder",
    "build_rollback_plan",
    "build_update_status_dependency_snapshot",
    "evaluate_update_readiness",
    "load_update_channel_config",
    "parse_update_manifest",
    "verify_update_integrity",
]
