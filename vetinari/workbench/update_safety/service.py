"""Update readiness service for Workbench release channels."""

from __future__ import annotations

import logging
from pathlib import Path

from vetinari.guards import GateError
from vetinari.security.fail_closed import UntrustedInputError, sanitize_untrusted_text
from vetinari.workbench.update_safety.channels import DEFAULT_CHANNEL_CONFIG_PATH, load_update_channel_config
from vetinari.workbench.update_safety.contracts import (
    CurrentInstall,
    UpdateChannel,
    UpdateIntegrityState,
    UpdateIntegrityVerdict,
    UpdateManifest,
    UpdateReadiness,
    UpdateReadinessState,
    UpdateSafetyError,
)
from vetinari.workbench.update_safety.integrity import SignatureVerifier, verify_update_integrity
from vetinari.workbench.update_safety.manifest import parse_update_manifest
from vetinari.workbench.update_safety.state import SkippedVersionStore

logger = logging.getLogger(__name__)


def evaluate_update_readiness(
    *,
    current_install: CurrentInstall | None = None,
    channel: UpdateChannel | str = UpdateChannel.STABLE,
    manifest_path: str | Path | None = None,
    manifest: UpdateManifest | dict | None = None,
    channel_config_path: str | Path = DEFAULT_CHANNEL_CONFIG_PATH,
    artifact_root: str | Path = ".",
    signature_verifier: SignatureVerifier | None = None,
    skipped_store: SkippedVersionStore | None = None,
) -> UpdateReadiness:
    """Evaluate whether an update candidate is safe to present as readiness.

    Returns:
        UpdateReadiness value produced by evaluate_update_readiness().
    """
    install = current_install or CurrentInstall(version="0.0.0-dev", installed_release=False)
    requested_channel = _coerce_channel(channel)
    skipped_versions: tuple[str, ...] = ()
    if not install.installed_release:
        return _blocked(
            requested_channel,
            install.version,
            "",
            ("dev_checkout_not_install_target",),
            skipped_versions=skipped_versions,
        )
    try:
        return _release_update_readiness(
            install=install,
            requested_channel=requested_channel,
            manifest_path=manifest_path,
            manifest=manifest,
            channel_config_path=channel_config_path,
            artifact_root=artifact_root,
            signature_verifier=signature_verifier,
            skipped_store=skipped_store,
        )
    except UpdateSafetyError as exc:
        logger.warning("Handled recoverable failure before fallback.", exc_info=True)
        return _blocked(requested_channel, install.version, "", (str(exc),), skipped_versions=skipped_versions)
    except (OSError, ValueError, TypeError) as exc:
        logger.warning("Handled recoverable failure before fallback.", exc_info=True)
        return _blocked(
            requested_channel,
            install.version,
            "",
            (f"readiness_unavailable:{type(exc).__name__}",),
            skipped_versions=skipped_versions,
        )


def _release_update_readiness(
    *,
    install: CurrentInstall,
    requested_channel: UpdateChannel,
    manifest_path: str | Path | None,
    manifest: UpdateManifest | dict | None,
    channel_config_path: str | Path,
    artifact_root: str | Path,
    signature_verifier: SignatureVerifier | None,
    skipped_store: SkippedVersionStore | None,
) -> UpdateReadiness:
    config = load_update_channel_config(channel_config_path)
    policy = config.policy_for(requested_channel)
    source = manifest if manifest is not None else manifest_path or policy.manifest_path
    parsed = parse_update_manifest(source) if not isinstance(source, UpdateManifest) else source
    if parsed.channel is not requested_channel:
        return _blocked(
            requested_channel, install.version, parsed.version, ("manifest_channel_mismatch",), manifest=parsed
        )
    if parsed.version == install.version:
        return _current_readiness(install, requested_channel, parsed)
    skipped_versions = ()
    if skipped_store is not None:
        skipped_versions = skipped_store.load().versions()
        if parsed.version in skipped_versions:
            integrity = verify_update_integrity(
                parsed,
                artifact_root=artifact_root,
                signature_verifier=signature_verifier,
            )
            return _candidate_readiness(
                UpdateReadinessState.SKIPPED,
                install,
                requested_channel,
                parsed,
                integrity,
                skipped_versions,
                reasons=("version_skipped_by_user",),
            )
    signature_required = policy.require_signature or parsed.integrity.require_signature
    if signature_required and signature_verifier is None:
        raise GateError("update_signature", "signature verifier unavailable for signature-required channel")
    verifier = signature_verifier
    integrity = verify_update_integrity(parsed, artifact_root=artifact_root, signature_verifier=verifier)
    if not integrity.passed:
        return _candidate_readiness(
            UpdateReadinessState.BLOCKED,
            install,
            requested_channel,
            parsed,
            integrity,
            skipped_versions,
            reasons=integrity.reasons,
        )
    return _candidate_readiness(
        UpdateReadinessState.READY,
        install,
        requested_channel,
        parsed,
        integrity,
        skipped_versions,
        reasons=("ready_no_auto_install",),
        approval_required=True,
    )


def _current_readiness(
    install: CurrentInstall,
    requested_channel: UpdateChannel,
    parsed: UpdateManifest,
) -> UpdateReadiness:
    integrity = UpdateIntegrityVerdict(UpdateIntegrityState.UNAVAILABLE, ("already_current",))
    return _candidate_readiness(
        UpdateReadinessState.CURRENT,
        install,
        requested_channel,
        parsed,
        integrity,
        (),
        reasons=("already_current",),
    )


def _candidate_readiness(
    state: UpdateReadinessState,
    install: CurrentInstall,
    channel: UpdateChannel,
    parsed: UpdateManifest,
    integrity: UpdateIntegrityVerdict,
    skipped_versions: tuple[str, ...],
    *,
    reasons: tuple[str, ...],
    approval_required: bool = False,
) -> UpdateReadiness:
    return UpdateReadiness(
        state=state,
        channel=channel,
        current_version=install.version,
        candidate_version=parsed.version,
        reasons=reasons,
        integrity=integrity,
        manifest=parsed,
        skipped_versions=skipped_versions,
        approval_required=approval_required,
    )


def _blocked(
    channel: UpdateChannel,
    current_version: str,
    candidate_version: str,
    reasons: tuple[str, ...],
    *,
    manifest: UpdateManifest | None = None,
    skipped_versions: tuple[str, ...] = (),
) -> UpdateReadiness:
    return UpdateReadiness(
        state=UpdateReadinessState.BLOCKED,
        channel=channel,
        current_version=current_version,
        candidate_version=candidate_version,
        reasons=reasons,
        integrity=UpdateIntegrityVerdict(UpdateIntegrityState.BLOCKED, reasons),
        manifest=manifest,
        skipped_versions=skipped_versions,
    )


def _coerce_channel(channel: UpdateChannel | str) -> UpdateChannel:
    try:
        if isinstance(channel, UpdateChannel):
            return channel
        return UpdateChannel(sanitize_untrusted_text(channel, max_length=64))
    except (UntrustedInputError, ValueError) as exc:
        raise UpdateSafetyError(f"channel_unknown:{channel}") from exc


__all__ = ["evaluate_update_readiness"]
