"""Non-destructive rollback guidance for Workbench updates."""

from __future__ import annotations

from vetinari.workbench.update_safety.contracts import UpdateManifest, UpdateReadinessState, UpdateSafetyRollbackPlan


def build_rollback_plan(manifest: UpdateManifest | None, *, current_version: str = "") -> UpdateSafetyRollbackPlan:
    """Build an explanatory rollback plan without mutating files.

    Returns:
        Newly constructed rollback plan value.
    """
    if manifest is None:
        return UpdateSafetyRollbackPlan(
            state=UpdateReadinessState.BLOCKED,
            prior_version="",
            artifact_digest="",
            requires_user_approval=True,
            support_guidance=("Create a support bundle before attempting rollback.",),
            reasons=("rollback_manifest_missing",),
        )
    prior = manifest.rollback_from_version or current_version
    if not prior:
        return UpdateSafetyRollbackPlan(
            state=UpdateReadinessState.BLOCKED,
            prior_version="",
            artifact_digest="",
            requires_user_approval=True,
            support_guidance=("Prior version evidence is required before rollback planning.",),
            reasons=("rollback_prior_version_missing",),
            release_notes_ref=manifest.public_export.export_ref,
        )
    digest = manifest.artifacts[0].digest if manifest.artifacts else ""
    if not digest:
        return UpdateSafetyRollbackPlan(
            state=UpdateReadinessState.BLOCKED,
            prior_version=prior,
            artifact_digest="",
            requires_user_approval=True,
            support_guidance=("Prior artifact digest is required before rollback planning.",),
            reasons=("rollback_artifact_digest_missing",),
            release_notes_ref=manifest.public_export.export_ref,
        )
    return UpdateSafetyRollbackPlan(
        state=UpdateReadinessState.APPROVAL_REQUIRED,
        prior_version=prior,
        artifact_digest=digest,
        requires_user_approval=True,
        support_guidance=(
            "Create an update support bundle before rollback.",
            "Confirm the prior artifact digest and release notes.",
            "Require Approval Chain confirmation before any later installer pack performs work.",
        ),
        reasons=("rollback_plan_ready_no_action_taken",),
        release_notes_ref=manifest.public_export.export_ref,
    )


__all__ = ["build_rollback_plan"]
