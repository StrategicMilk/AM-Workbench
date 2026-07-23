"""Approval-gated backend overlay manifest loading and apply planning."""

from __future__ import annotations

import logging
import shlex
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, cast

import yaml

from vetinari.constants import PROJECT_ROOT

logger = logging.getLogger(__name__)


DEFAULT_MANIFEST_PATH = PROJECT_ROOT / "third_party_overlays" / "overlays.example.yaml"
DEFAULT_PINS_PATH = PROJECT_ROOT / "config" / "backend_pins.yaml"
REQUIRED_MANIFEST_FIELDS = (
    "backend",
    "upstream_version",
    "patch_queue_path",
    "purpose",
    "known_bad_repro_command",
    "known_good_proof_command",
    "benchmark_evidence",
    "rollback_command",
    "approval_status",
    "approval_actor",
    "approval_timestamp",
    "rebase_status",
    "last_checked_upstream_version",
)
APPROVAL_STATUSES = {"approved", "pending", "rejected"}
REBASE_STATUSES = {"clean", "failed", "pending", "unknown"}
_PATH_FORBIDDEN_MARKERS = (";", "&", "|", "<", ">", "`", "$(", "\x00")


class BackendOverlayError(RuntimeError):
    """Base exception for backend overlay validation and planning failures."""


class OverlayManifestError(BackendOverlayError):
    """Raised when an overlay manifest is missing, unreadable, or invalid."""


class OverlayApprovalError(BackendOverlayError):
    """Raised when an apply plan is requested without explicit approval."""


@dataclass(frozen=True, slots=True)
class OverlayFinding:
    """A deterministic manifest validation finding."""

    path: str
    field: str
    message: str


@dataclass(frozen=True, slots=True)
class OverlayManifest:
    """Validated overlay metadata for one backend."""

    backend: str
    upstream_version: str
    patch_queue_path: Path
    purpose: str
    known_bad_repro_command: str
    known_good_proof_command: str
    benchmark_evidence: str
    rollback_command: str
    approval_status: Literal["approved", "pending", "rejected"]
    approval_actor: str
    approval_timestamp: str
    rebase_status: Literal["clean", "failed", "pending", "unknown"]
    last_checked_upstream_version: str
    manifest_path: Path

    def reversible_manifest(self) -> dict[str, str]:
        """Return the operator-facing rollback record for this overlay."""
        return {
            "backend": self.backend,
            "upstream_version": self.upstream_version,
            "patch_queue_path": self.patch_queue_path.as_posix(),
            "known_bad_repro_command": self.known_bad_repro_command,
            "known_good_proof_command": self.known_good_proof_command,
            "benchmark_evidence": self.benchmark_evidence,
            "rollback_command": self.rollback_command,
            "approval_actor": self.approval_actor,
            "approval_timestamp": self.approval_timestamp,
            "rebase_status": self.rebase_status,
            "planned_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"OverlayManifest(backend={self.backend!r}, upstream_version={self.upstream_version!r}, patch_queue_path={self.patch_queue_path!r})"


@dataclass(frozen=True, slots=True)
class OverlayApplyPlan:
    """A mutation-free plan describing what an overlay apply would do."""

    backend: str
    status: Literal["dry-run", "ready", "blocked", "failed-rebase"]
    commands: tuple[str, ...]
    rollback_command: str
    reversible_manifest: dict[str, str]
    diagnostics: tuple[str, ...] = ()

    @property
    def approved(self) -> bool:
        """Whether this plan is explicitly approved to apply."""
        return self.status == "ready"

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"OverlayApplyPlan(backend={self.backend!r}, status={self.status!r}, commands={self.commands!r})"


def _load_yaml_mapping(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise OverlayManifestError(f"{path}: manifest file does not exist")
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise OverlayManifestError(f"{path}: YAML parse failed: {exc}") from exc
    if not isinstance(loaded, dict):
        raise OverlayManifestError(f"{path}: expected YAML mapping at document root")
    return loaded


def load_backend_pins(path: Path = DEFAULT_PINS_PATH) -> dict[str, Any]:
    """Load backend pins, failing closed on missing or malformed state.

    Returns:
        Resolved backend pins value.

    Raises:
        Exception: Propagates validation or runtime failures from the underlying operation.
    """
    data = _load_yaml_mapping(path)
    backends = data.get("backends")
    if not isinstance(backends, dict) or not backends:
        raise OverlayManifestError(f"{path}: missing non-empty backends mapping")
    return data


def overlay_enabled_backends(pins_path: Path = DEFAULT_PINS_PATH) -> dict[str, dict[str, Any]]:
    """Return backend pin entries that explicitly allow overlays.

    Returns:
        dict[str, dict[str, Any]] value produced by overlay_enabled_backends().
    """
    pins = load_backend_pins(pins_path)
    enabled: dict[str, dict[str, Any]] = {}
    for backend, metadata in pins["backends"].items():
        if not isinstance(metadata, dict):
            continue
        overlays = metadata.get("overlays")
        if isinstance(overlays, dict) and overlays.get("allowed") is True:
            enabled[str(backend)] = metadata
    return enabled


def _version_matches_pin(upstream_version: str, pin_version: Any) -> bool:
    pin = str(pin_version).strip()
    if not pin:
        return False
    if pin == upstream_version:
        return True
    return upstream_version in pin


def _validate_timestamp(value: str) -> bool:
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        logger.warning("Handled recoverable failure before fallback.", exc_info=True)
        return False
    return True


def _path_has_shell_metacharacters(value: str) -> bool:
    return any(marker in value for marker in _PATH_FORBIDDEN_MARKERS)


def _path_is_confined_relative(value: str) -> bool:
    path = Path(value)
    return not path.is_absolute() and ".." not in path.parts and not _path_has_shell_metacharacters(value)


def _relative_path_exists(value: str, *, manifest_path: Path) -> bool:
    candidates = [PROJECT_ROOT / value, manifest_path.parent / value]
    return any(candidate.exists() for candidate in candidates)


def validate_manifest_dict(
    raw: dict[str, Any],
    *,
    manifest_path: Path,
    pins_path: Path = DEFAULT_PINS_PATH,
) -> list[OverlayFinding]:
    """Return all validation findings for a raw manifest entry.

    Returns:
        Validation outcome for manifest dict.
    """
    findings: list[OverlayFinding] = []
    enabled_backends = overlay_enabled_backends(pins_path)

    for field in REQUIRED_MANIFEST_FIELDS:
        value = raw.get(field)
        if value is None or str(value).strip() == "":
            findings.append(OverlayFinding(str(manifest_path), field, "required field is missing or blank"))

    backend = str(raw.get("backend", "")).strip()
    if backend and backend not in enabled_backends:
        findings.append(OverlayFinding(str(manifest_path), "backend", f"backend {backend!r} does not allow overlays"))

    approval_status = str(raw.get("approval_status", "")).strip()
    if approval_status and approval_status not in APPROVAL_STATUSES:
        findings.append(
            OverlayFinding(str(manifest_path), "approval_status", f"must be one of {sorted(APPROVAL_STATUSES)}")
        )
    if approval_status != "approved":
        findings.append(OverlayFinding(str(manifest_path), "approval_status", "explicit approved status is required"))

    rebase_status = str(raw.get("rebase_status", "")).strip()
    if rebase_status and rebase_status not in REBASE_STATUSES:
        findings.append(
            OverlayFinding(str(manifest_path), "rebase_status", f"must be one of {sorted(REBASE_STATUSES)}")
        )
    patch_queue_path = str(raw.get("patch_queue_path", "")).strip()
    if patch_queue_path and not _path_is_confined_relative(patch_queue_path):
        findings.append(
            OverlayFinding(
                str(manifest_path),
                "patch_queue_path",
                "must be a relative path without traversal or shell metacharacters",
            )
        )
    benchmark_evidence = str(raw.get("benchmark_evidence", "")).strip()
    if benchmark_evidence:
        if not _path_is_confined_relative(benchmark_evidence):
            findings.append(
                OverlayFinding(
                    str(manifest_path),
                    "benchmark_evidence",
                    "must be a relative evidence path without traversal or shell metacharacters",
                )
            )
        elif not _relative_path_exists(benchmark_evidence, manifest_path=manifest_path):
            findings.append(
                OverlayFinding(
                    str(manifest_path),
                    "benchmark_evidence",
                    "must reference an existing benchmark evidence artifact",
                )
            )
    timestamp = str(raw.get("approval_timestamp", "")).strip()
    if timestamp and not _validate_timestamp(timestamp):
        findings.append(OverlayFinding(str(manifest_path), "approval_timestamp", "must be UTC ISO-8601"))

    if backend in enabled_backends:
        pin_version = enabled_backends[backend].get("version")
        upstream_version = str(raw.get("upstream_version", "")).strip()
        last_checked = str(raw.get("last_checked_upstream_version", "")).strip()
        if upstream_version and not _version_matches_pin(upstream_version, pin_version):
            findings.append(OverlayFinding(str(manifest_path), "upstream_version", "does not match backend pin"))
        if upstream_version and last_checked and upstream_version != last_checked:
            findings.append(
                OverlayFinding(
                    str(manifest_path),
                    "last_checked_upstream_version",
                    "must match upstream_version for this overlay",
                )
            )

    return findings


def _coerce_manifest(raw: dict[str, Any], *, manifest_path: Path, pins_path: Path) -> OverlayManifest:
    findings = validate_manifest_dict(raw, manifest_path=manifest_path, pins_path=pins_path)
    if findings:
        details = "; ".join(f"{finding.field}: {finding.message}" for finding in findings)
        raise OverlayManifestError(f"{manifest_path}: invalid overlay manifest: {details}")
    return OverlayManifest(
        backend=str(raw["backend"]),
        upstream_version=str(raw["upstream_version"]),
        patch_queue_path=Path(str(raw["patch_queue_path"])),
        purpose=str(raw["purpose"]),
        known_bad_repro_command=str(raw["known_bad_repro_command"]),
        known_good_proof_command=str(raw["known_good_proof_command"]),
        benchmark_evidence=str(raw["benchmark_evidence"]),
        rollback_command=str(raw["rollback_command"]),
        approval_status=cast(Literal["approved", "pending", "rejected"], str(raw["approval_status"])),
        approval_actor=str(raw["approval_actor"]),
        approval_timestamp=str(raw["approval_timestamp"]),
        rebase_status=cast(Literal["clean", "failed", "pending", "unknown"], str(raw["rebase_status"])),
        last_checked_upstream_version=str(raw["last_checked_upstream_version"]),
        manifest_path=manifest_path,
    )


def load_overlay_manifests(
    manifest_path: Path = DEFAULT_MANIFEST_PATH,
    *,
    pins_path: Path = DEFAULT_PINS_PATH,
) -> list[OverlayManifest]:
    """Load and validate all overlay entries from a manifest file.

    Returns:
        Resolved overlay manifests value.

    Raises:
        Exception: Propagates validation or runtime failures from the underlying operation.
    """
    data = _load_yaml_mapping(manifest_path)
    overlays = data.get("overlays")
    if not isinstance(overlays, list) or not overlays:
        raise OverlayManifestError(f"{manifest_path}: missing non-empty overlays list")
    manifests: list[OverlayManifest] = []
    for item in overlays:
        if not isinstance(item, dict):
            raise OverlayManifestError(f"{manifest_path}: every overlay entry must be a mapping")
        manifests.append(_coerce_manifest(item, manifest_path=manifest_path, pins_path=pins_path))
    return manifests


def find_overlay_manifest(
    backend: str,
    manifest_path: Path = DEFAULT_MANIFEST_PATH,
    *,
    pins_path: Path = DEFAULT_PINS_PATH,
) -> OverlayManifest:
    """Find one validated overlay manifest by backend name.

    Args:
        backend: Backend value consumed by find_overlay_manifest().
        manifest_path: Filesystem path read or written by the operation.
        pins_path: Filesystem path read or written by the operation.

    Returns:
        Resolved overlay manifest value.

    Raises:
        Exception: Propagates validation or runtime failures from the underlying operation.
    """
    matches = [item for item in load_overlay_manifests(manifest_path, pins_path=pins_path) if item.backend == backend]
    if not matches:
        raise OverlayManifestError(f"{manifest_path}: no overlay manifest for backend {backend!r}")
    if len(matches) > 1:
        raise OverlayManifestError(f"{manifest_path}: duplicate overlay manifest for backend {backend!r}")
    return matches[0]


def dry_run_overlay(manifest: OverlayManifest) -> OverlayApplyPlan:
    """Return the mutation-free command plan for an overlay.

    Returns:
        OverlayApplyPlan value produced by dry_run_overlay().
    """
    patch_path = shlex.quote(manifest.patch_queue_path.as_posix())
    command = f"git apply --check {patch_path}"
    return OverlayApplyPlan(
        backend=manifest.backend,
        status="dry-run",
        commands=(command,),
        rollback_command=manifest.rollback_command,
        reversible_manifest=manifest.reversible_manifest(),
    )


def plan_overlay_apply(
    manifest: OverlayManifest,
    *,
    explicit_approval: bool = False,
) -> OverlayApplyPlan:
    """Return an approval-gated apply plan without mutating backend files.

    Returns:
        OverlayApplyPlan value produced by plan_overlay_apply().
    """
    if manifest.rebase_status == "failed":
        return OverlayApplyPlan(
            backend=manifest.backend,
            status="failed-rebase",
            commands=(),
            rollback_command=manifest.rollback_command,
            reversible_manifest=manifest.reversible_manifest(),
            diagnostics=("patch queue does not rebase cleanly; upstream backend files were not touched",),
        )
    if manifest.rebase_status != "clean":
        return OverlayApplyPlan(
            backend=manifest.backend,
            status="blocked",
            commands=(),
            rollback_command=manifest.rollback_command,
            reversible_manifest=manifest.reversible_manifest(),
            diagnostics=(f"patch queue rebase status is {manifest.rebase_status!r}; apply requires clean",),
        )
    if manifest.approval_status != "approved" or not explicit_approval:
        return OverlayApplyPlan(
            backend=manifest.backend,
            status="blocked",
            commands=(),
            rollback_command=manifest.rollback_command,
            reversible_manifest=manifest.reversible_manifest(),
            diagnostics=(
                f"{OverlayApprovalError.__name__}: explicit approval is required before planning an overlay apply",
            ),
        )
    return OverlayApplyPlan(
        backend=manifest.backend,
        status="ready",
        commands=(f"git apply {shlex.quote(manifest.patch_queue_path.as_posix())}",),
        rollback_command=manifest.rollback_command,
        reversible_manifest=manifest.reversible_manifest(),
    )
