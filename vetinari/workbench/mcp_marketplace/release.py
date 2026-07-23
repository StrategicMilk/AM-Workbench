"""Release-smoke admission and rollback receipts for extension packages."""

from __future__ import annotations

import logging
import re as _re
import shutil
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from python.amw_extension_sdk import ManifestValidationError, load_manifest

from vetinari.exceptions import SigningUnavailableError

logger = logging.getLogger(__name__)


# MCP Registry naming convention: tool names use lowercase ASCII letters,
# digits, underscores, and hyphens; an optional namespace/tool-name form is
# permitted with a single slash separator. Names with spaces, uppercase, or
# other punctuation are not portable across MCP clients.

_MCP_TOOL_NAME_RE = _re.compile(r"^[a-z0-9_\-]+(/[a-z0-9_\-]+)?$")


def normalize_tool_name(name: str) -> str:
    """Normalize an MCP tool name to the MCP Registry naming convention.

    Lowercases, replaces whitespace with hyphens, strips characters outside
    ``[a-z0-9_-]`` (and the single ``/`` namespace separator), and collapses
    duplicate separators. The returned name MUST match
    ``[a-z0-9_-]+(/[a-z0-9_-]+)?`` so it is portable across MCP clients.

    Args:
        name: Raw tool name from a marketplace registration record.

    Returns:
        Normalized tool name suitable for MCP Registry publication.
    """
    lowered = name.lower().replace(" ", "-")
    # Preserve a single slash namespace separator; strip everything else.
    # Collapse repeated hyphens and strip leading/trailing hyphens, then collapse duplicate slashes.
    return _re.sub(
        r"/{2,}",
        "/",
        _re.sub(r"-{2,}", "-", _re.sub(r"[^a-z0-9_/\-]", "-", lowered)).strip("-"),
    )


def is_portable_tool_name(name: str) -> bool:
    """Return True when ``name`` already matches the MCP Registry convention."""
    return bool(_MCP_TOOL_NAME_RE.fullmatch(name))


def sign_release_artifact(
    artifact_path: Path,
    *,
    key_id: str,
    signing_required: bool = True,
    bypass_reason: str = "",
) -> Path | None:
    """Produce a detached GPG signature next to a release artifact.

    Runs ``gpg --detach-sign --armor --local-user <key_id> --output
    <artifact>.sig --status-fd 2 <artifact>`` and returns the signature path
    when GPG reports ``[GNUPG:] SIG_CREATED``. Fails closed via
    :class:`SigningUnavailableError` when ``gpg`` is not on PATH unless the
    caller opts out with ``signing_required=False`` AND a non-empty
    ``bypass_reason`` — a silent skip is not allowed.

    Args:
        artifact_path: Path of the artifact (LoRA adapter, release bundle)
            to sign. Must exist on disk.
        key_id: GPG key identifier passed to ``--local-user``.
        signing_required: When True (default) absent GPG raises
            :class:`SigningUnavailableError`. When False, the caller must
            supply ``bypass_reason`` to record why signing was skipped.
        bypass_reason: Required when ``signing_required=False``. Logged at
            WARNING so the operator can audit unsigned releases.

    Returns:
        Path to the produced ``<artifact>.sig`` file, or ``None`` when
        signing was explicitly bypassed.

    Raises:
        SigningUnavailableError: GPG not installed and signing was required.
        FileNotFoundError: ``artifact_path`` does not exist on disk.
        RuntimeError: GPG ran but did not report SIG_CREATED.
    """
    if not artifact_path.exists():
        raise FileNotFoundError(f"release artifact not found: {artifact_path}")
    gpg_path = shutil.which("gpg")
    if gpg_path is None:
        if signing_required:
            raise SigningUnavailableError(
                "gpg binary not found on PATH; cannot produce detached signature "
                f"for {artifact_path}. Install GPG or pass signing_required=False "
                "with an explicit bypass_reason to skip."
            )
        if not bypass_reason.strip():
            raise SigningUnavailableError(
                "signing_required=False but bypass_reason is empty; silent signing skip is not allowed"
            )
        logger.warning(
            "Release signing bypassed for %s — gpg unavailable, reason=%s",
            artifact_path,
            bypass_reason,
        )
        return None
    signature_path = artifact_path.with_suffix(artifact_path.suffix + ".sig")
    completed = subprocess.run(  # noqa: S603 - gpg_path is resolved via shutil.which
        [
            gpg_path,
            "--detach-sign",
            "--armor",
            "--local-user",
            key_id,
            "--output",
            str(signature_path),
            "--status-fd",
            "2",
            str(artifact_path),
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if completed.returncode != 0 or "[GNUPG:] SIG_CREATED" not in completed.stderr:
        raise RuntimeError(
            f"gpg did not report SIG_CREATED for {artifact_path}: "
            f"returncode={completed.returncode} stderr={completed.stderr!r}"
        )
    return signature_path


@dataclass(frozen=True, slots=True)
class ReleaseDecision:
    """Admission result for an extension marketplace release package."""

    admitted: bool
    code: str
    support: dict[str, str]
    receipts: tuple[dict[str, Any], ...] = field(default_factory=tuple)

    def __repr__(self) -> str:
        return f"ReleaseDecision(admitted={self.admitted!r}, code={self.code!r}, receipts={len(self.receipts)!r})"


class ExtensionReleaseService:
    """Validate package manifests before marketplace admission."""

    def admit(self, package: Mapping[str, Any]) -> ReleaseDecision:
        """Validate a package and emit admission or rollback receipts.

        Returns:
            Release admission decision with receipts.
        """
        manifest_payload = package.get("manifest")
        try:
            manifest = load_manifest(manifest_payload if isinstance(manifest_payload, Mapping) else {})
        except ManifestValidationError as exc:
            logger.warning("Extension package manifest rejected during release admission: %s", exc.envelope.code)
            return ReleaseDecision(
                admitted=False,
                code=exc.envelope.code,
                support=exc.envelope.to_dict(),
                receipts=(self._receipt("admission", "failed_closed"), self._receipt("rollback", "completed")),
            )
        # Validate tool-name portability per MCP Registry naming convention.
        raw_tool_name: str = getattr(manifest, "tool_name", "") or package.get("tool_name", "")
        if raw_tool_name:
            normalized = normalize_tool_name(raw_tool_name)
            if not is_portable_tool_name(normalized):
                logger.warning(
                    "Extension tool name %r normalized to %r which is not portable across MCP clients — "
                    "admission continues but the name should be corrected before marketplace publication",
                    raw_tool_name,
                    normalized,
                )
        if package.get("default_on") is not True:
            return ReleaseDecision(
                admitted=False,
                code="EXT_DEFAULT_ON",
                support={
                    "code": "EXT_DEFAULT_ON",
                    "message": "marketplace entry is not default-on through the product surface",
                    "recovery": "register the extension in the default-on marketplace surface",
                },
                receipts=(self._receipt("admission", "failed_closed"), self._receipt("rollback", "completed")),
            )
        receipt = {
            "receipt_id": f"release:{manifest.extension_id}:admission",
            "caller": "vetinari.workbench.mcp_marketplace.release.ExtensionReleaseService",
            "read_path": "python.amw_extension_sdk.manifest",
            "write_path": manifest.receipt_path,
            "permission": ",".join(manifest.permissions),
            "status": "accepted",
        }
        return ReleaseDecision(admitted=True, code="accepted", support={}, receipts=(receipt,))

    @staticmethod
    def sign_release(
        artifact_path: Path,
        *,
        key_id: str,
        signing_required: bool = True,
        bypass_reason: str = "",
    ) -> Path | None:
        """Wrap :func:`sign_release_artifact` for callers that already hold a service instance.

        Args:
            artifact_path: Release artifact (LoRA adapter, bundle) to sign.
            key_id: GPG key identifier.
            signing_required: Fail-closed when GPG missing if True.
            bypass_reason: Required when signing_required=False.

        Returns:
            Detached signature path, or None when explicitly bypassed.
        """
        return sign_release_artifact(
            artifact_path,
            key_id=key_id,
            signing_required=signing_required,
            bypass_reason=bypass_reason,
        )

    @staticmethod
    def _receipt(action: str, status: str) -> dict[str, str]:
        return {
            "receipt_id": f"release:{action}:{status}",
            "action": action,
            "status": status,
            "message": "sanitized",
        }
