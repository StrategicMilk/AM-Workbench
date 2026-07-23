"""JSON-safe contracts for Workbench update safety."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

SCHEMA_VERSION = "1.0"


class UpdateSafetyError(RuntimeError):
    """Raised when update safety inputs cannot be trusted."""


class UpdateChannel(StrEnum):
    """Supported Workbench update channels."""

    STABLE = "stable"
    BETA = "beta"


class UpdateReadinessState(StrEnum):
    """Install-readiness state returned to API and UI callers."""

    READY = "ready"
    BLOCKED = "blocked"
    CURRENT = "current"
    SKIPPED = "skipped"
    APPROVAL_REQUIRED = "approval_required"


class UpdateIntegrityState(StrEnum):
    """Integrity verdict states for update artifacts."""

    VERIFIED = "verified"
    BLOCKED = "blocked"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class PublicExportProvenance:
    """Reference tying a release to public export provenance without mutating export tooling."""

    export_ref: str
    source_commit: str
    generated_at_utc: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "export_ref": self.export_ref,
            "source_commit": self.source_commit,
            "generated_at_utc": self.generated_at_utc,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> PublicExportProvenance:
        return cls(
            export_ref=_required_string(payload, "export_ref"),
            source_commit=_required_string(payload, "source_commit"),
            generated_at_utc=_required_string(payload, "generated_at_utc"),
        )


@dataclass(frozen=True, slots=True)
class UpdateArtifact:
    """One platform artifact described by an update manifest."""

    platform: str
    url: str
    digest: str
    size_bytes: int
    local_path: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "url": self.url,
            "digest": self.digest,
            "size_bytes": self.size_bytes,
            "local_path": self.local_path,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> UpdateArtifact:
        return cls(
            platform=_required_string(payload, "platform"),
            url=_required_string(payload, "url"),
            digest=_required_string(payload, "digest"),
            size_bytes=int(payload.get("size_bytes", -1)),
            local_path=str(payload.get("local_path", "")),
        )

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"UpdateArtifact(platform={self.platform!r}, url={self.url!r}, digest={self.digest!r})"


@dataclass(frozen=True, slots=True)
class UpdateIntegrityPolicy:
    """Manifest-declared integrity policy."""

    checksum_algorithm: str = "sha256"
    require_signature: bool = True
    signature_evidence: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "checksum_algorithm": self.checksum_algorithm,
            "require_signature": self.require_signature,
            "signature_evidence": self.signature_evidence,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> UpdateIntegrityPolicy:
        return cls(
            checksum_algorithm=str(payload.get("checksum_algorithm", "sha256")),
            require_signature=bool(payload.get("require_signature", True)),
            signature_evidence=str(payload.get("signature_evidence", "")),
        )


@dataclass(frozen=True, slots=True)
class UpdateManifest:
    """Release manifest consumed by the update safety gate."""

    schema_version: str
    version: str
    channel: UpdateChannel
    release_notes: str
    public_export: PublicExportProvenance
    artifacts: tuple[UpdateArtifact, ...]
    integrity: UpdateIntegrityPolicy
    published_at_utc: str
    rollback_from_version: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "version": self.version,
            "channel": self.channel.value,
            "release_notes": self.release_notes,
            "public_export": self.public_export.to_dict(),
            "artifacts": [artifact.to_dict() for artifact in self.artifacts],
            "integrity": self.integrity.to_dict(),
            "published_at_utc": self.published_at_utc,
            "rollback_from_version": self.rollback_from_version,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> UpdateManifest:
        """Execute the from dict operation.

        Returns:
            UpdateManifest value produced by from_dict().

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        artifacts = payload.get("artifacts")
        if not isinstance(artifacts, list) or not artifacts:
            raise UpdateSafetyError("manifest.artifacts must be a non-empty list")
        return cls(
            schema_version=_required_string(payload, "schema_version"),
            version=_required_string(payload, "version"),
            channel=UpdateChannel(_required_string(payload, "channel")),
            release_notes=_required_string(payload, "release_notes"),
            public_export=PublicExportProvenance.from_dict(_required_mapping(payload, "public_export")),
            artifacts=tuple(UpdateArtifact.from_dict(_ensure_mapping(row, "artifacts[]")) for row in artifacts),
            integrity=UpdateIntegrityPolicy.from_dict(_required_mapping(payload, "integrity")),
            published_at_utc=_required_string(payload, "published_at_utc"),
            rollback_from_version=str(payload.get("rollback_from_version", "")),
        )

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"UpdateManifest(schema_version={self.schema_version!r}, version={self.version!r}, channel={self.channel!r})"


@dataclass(frozen=True, slots=True)
class CurrentInstall:
    """Facts about the currently running Workbench install."""

    version: str
    installed_release: bool
    platform: str = "windows-x64"
    project_id: str = "default"
    install_root: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "installed_release": self.installed_release,
            "platform": self.platform,
            "project_id": self.project_id,
            "install_root": self.install_root,
        }

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"CurrentInstall(version={self.version!r}, installed_release={self.installed_release!r}, platform={self.platform!r})"


@dataclass(frozen=True, slots=True)
class UpdateIntegrityVerdict:
    """Integrity evaluation result for a manifest artifact set."""

    state: UpdateIntegrityState
    reasons: tuple[str, ...]
    artifact_digests: tuple[str, ...] = ()
    signature_evidence: str = ""

    @property
    def passed(self) -> bool:
        return self.state is UpdateIntegrityState.VERIFIED

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "passed": self.passed,
            "reasons": list(self.reasons),
            "artifact_digests": list(self.artifact_digests),
            "signature_evidence": self.signature_evidence,
        }

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"UpdateIntegrityVerdict(state={self.state!r}, reasons={self.reasons!r}, artifact_digests={self.artifact_digests!r})"


@dataclass(frozen=True, slots=True)
class UpdateReadiness:
    """Fail-closed update readiness payload."""

    state: UpdateReadinessState
    channel: UpdateChannel | str
    current_version: str
    candidate_version: str
    reasons: tuple[str, ...]
    integrity: UpdateIntegrityVerdict
    manifest: UpdateManifest | None = None
    skipped_versions: tuple[str, ...] = ()
    no_auto_install: bool = True
    approval_required: bool = False
    install_plan: None = None

    def to_dict(self) -> dict[str, Any]:
        """Execute the to dict operation.

        Returns:
            dict[str, Any] value produced by to_dict().
        """
        channel = self.channel.value if isinstance(self.channel, UpdateChannel) else str(self.channel)
        return {
            "schema_version": SCHEMA_VERSION,
            "state": self.state.value,
            "channel": channel,
            "current_version": self.current_version,
            "candidate_version": self.candidate_version,
            "reasons": list(self.reasons),
            "integrity": self.integrity.to_dict(),
            "manifest": self.manifest.to_dict() if self.manifest else None,
            "release_notes": self.manifest.release_notes if self.manifest else "",
            "public_export_ref": self.manifest.public_export.export_ref if self.manifest else "",
            "skipped_versions": list(self.skipped_versions),
            "no_auto_install": self.no_auto_install,
            "approval_required": self.approval_required,
            "install_plan": self.install_plan,
        }

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return (
            f"UpdateReadiness(state={self.state!r}, channel={self.channel!r}, current_version={self.current_version!r})"
        )


@dataclass(frozen=True, slots=True)
class UpdateSafetyRollbackPlan:
    """Non-destructive rollback guidance."""

    state: UpdateReadinessState
    prior_version: str
    artifact_digest: str
    requires_user_approval: bool
    support_guidance: tuple[str, ...]
    reasons: tuple[str, ...] = ()
    release_notes_ref: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "prior_version": self.prior_version,
            "artifact_digest": self.artifact_digest,
            "requires_user_approval": self.requires_user_approval,
            "support_guidance": list(self.support_guidance),
            "reasons": list(self.reasons),
            "release_notes_ref": self.release_notes_ref,
            "destructive_operation": False,
        }

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"UpdateSafetyRollbackPlan(state={self.state!r}, prior_version={self.prior_version!r}, artifact_digest={self.artifact_digest!r})"


@dataclass(frozen=True, slots=True)
class SkippedVersionRecord:
    """One persisted skipped update decision."""

    version: str
    channel: str
    skipped_at_utc: str
    approval_decision_id: str
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "channel": self.channel,
            "skipped_at_utc": self.skipped_at_utc,
            "approval_decision_id": self.approval_decision_id,
            "reason": self.reason,
        }

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"SkippedVersionRecord(version={self.version!r}, channel={self.channel!r}, skipped_at_utc={self.skipped_at_utc!r})"


@dataclass(frozen=True, slots=True)
class SupportBundleBuildResult:
    """Result of a redaction-first support bundle build."""

    state: UpdateReadinessState
    bundle_path: str
    included_files: tuple[str, ...] = ()
    redacted_files: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "bundle_path": self.bundle_path,
            "included_files": list(self.included_files),
            "redacted_files": list(self.redacted_files),
            "reasons": list(self.reasons),
            "metadata": self.metadata,
        }

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"SupportBundleBuildResult(state={self.state!r}, bundle_path={self.bundle_path!r}, included_files={self.included_files!r})"


def _required_string(payload: dict[str, Any], field_name: str) -> str:
    value = payload.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise UpdateSafetyError(f"{field_name} must be a non-empty string")
    return value.strip()


def _required_mapping(payload: dict[str, Any], field_name: str) -> dict[str, Any]:
    return _ensure_mapping(payload.get(field_name), field_name)


def _ensure_mapping(value: Any, field_name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise UpdateSafetyError(f"{field_name} must be an object")
    return value


__all__ = [
    "SCHEMA_VERSION",
    "CurrentInstall",
    "PublicExportProvenance",
    "SkippedVersionRecord",
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
]
