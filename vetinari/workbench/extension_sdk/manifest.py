"""Fail-closed manifest loading for AM Workbench extensions."""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from vetinari.security.fail_closed import SchemaOpenError, assert_closed_schema
from vetinari.security.path_confinement import (
    PathConfinementError,
    validate_receipt_path,
    validate_scoped_permission,
)

SCHEMA_VERSION = "ide-extension.v1"
ALLOWED_KEYS = {
    "extension_id",
    "schema_version",
    "signature",
    "permissions",
    "retention_days",
    "privacy",
    "receipt_path",
}
ALLOWED_PERMISSION_PREFIXES = ("tool:", "resource:", "fs_read:")
TRUSTED_LOCAL_FIXTURE_SIGNATURE = "signed:local-fixture"
logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ManifestSupportEnvelope:
    """User-facing support envelope for manifest validation failures."""

    code: str
    message: str
    recovery: str

    def to_dict(self) -> dict[str, str]:
        """Return the envelope as JSON-serializable fields.

        Returns:
            dict[str, str] value produced by to_dict().
        """
        return {"code": self.code, "message": self.message, "recovery": self.recovery}


@dataclass(frozen=True, slots=True)
class ExtensionSdkManifest:
    """Validated AM Workbench extension manifest."""

    extension_id: str
    schema_version: str
    signature: str
    permissions: tuple[str, ...]
    retention_days: int
    receipt_path: str

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return (
            f"ExtensionManifest(extension_id={self.extension_id!r}, "
            f"schema_version={self.schema_version!r}, permissions={len(self.permissions)!r})"
        )

    def to_dict(self) -> dict[str, Any]:
        """Return the manifest as JSON-serializable fields.

        Returns:
            dict[str, Any] value produced by to_dict().
        """
        return {
            "extension_id": self.extension_id,
            "schema_version": self.schema_version,
            "signature": self.signature,
            "permissions": list(self.permissions),
            "retention_days": self.retention_days,
            "receipt_path": self.receipt_path,
        }


class ManifestValidationError(ValueError):
    """Raised when callers request exception-style manifest validation."""

    def __init__(self, envelope: ManifestSupportEnvelope) -> None:
        """Run __init__."""
        super().__init__(envelope.message)
        self.envelope = envelope


ExtensionManifest = ExtensionSdkManifest


def load_manifest(source: str | Path | Mapping[str, Any]) -> ExtensionSdkManifest:
    """Load and validate an extension manifest from a path or mapping.

    Returns:
        Validated extension manifest.

    Raises:
        ManifestValidationError: If the manifest is unreadable or invalid.
    """
    if isinstance(source, Mapping):
        payload = dict(source)
    else:
        try:
            payload = json.loads(Path(source).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ManifestValidationError(
                ManifestSupportEnvelope("EXT_MANIFEST_READ", "manifest could not be read", "provide readable JSON")
            ) from exc
    manifest, envelope = validate_manifest(payload)
    if envelope is not None:
        raise ManifestValidationError(envelope)
    return manifest


def validate_manifest(payload: Mapping[str, Any]) -> tuple[ExtensionSdkManifest | None, ManifestSupportEnvelope | None]:
    """Validate manifest payload fields and return either manifest or support envelope.

    Returns:
        A validated manifest, or a support envelope explaining the failure.
    """
    try:
        assert_closed_schema(payload, allowed_keys=ALLOWED_KEYS, required_keys=ALLOWED_KEYS)
    except SchemaOpenError as exc:
        logger.warning("Rejected extension manifest schema: %s", exc)
        return None, ManifestSupportEnvelope(
            "EXT_SCHEMA_CLOSED", "manifest schema is incomplete or contains unknown fields", "match ide-extension.v1"
        )
    if payload.get("schema_version") != SCHEMA_VERSION:
        return None, ManifestSupportEnvelope("EXT_SCHEMA", "manifest schema is unsupported", "use ide-extension.v1")
    extension_id = payload.get("extension_id")
    if not isinstance(extension_id, str) or not extension_id.strip():
        return None, ManifestSupportEnvelope("EXT_ID", "manifest extension_id is required", "set extension_id")
    signature = payload.get("signature")
    if signature != TRUSTED_LOCAL_FIXTURE_SIGNATURE:
        return None, ManifestSupportEnvelope(
            "EXT_SIGNATURE",
            "manifest signature is not trusted",
            "sign the package with a trusted key",
        )
    permissions_raw = payload.get("permissions")
    if not isinstance(permissions_raw, list) or not permissions_raw:
        return None, ManifestSupportEnvelope(
            "EXT_PERMISSION_MISSING", "manifest permissions are required", "declare permissions"
        )
    permissions: list[str] = []
    for permission in permissions_raw:
        try:
            permission = validate_scoped_permission(permission, allowed_prefixes=ALLOWED_PERMISSION_PREFIXES)
        except PathConfinementError:
            logger.warning("Extension manifest permission failed confinement.", exc_info=True)
            return None, ManifestSupportEnvelope(
                "EXT_PERMISSION", "manifest permission escapes its scope", "declare a scoped permission"
            )
        permissions.append(permission)
    if len(permissions) != len(set(permissions)):
        return None, ManifestSupportEnvelope(
            "EXT_PERMISSION_DUP", "manifest permissions contain duplicates", "remove duplicates"
        )
    retention_days = payload.get("retention_days")
    if not isinstance(retention_days, int) or retention_days < 0 or retention_days > 30:
        return None, ManifestSupportEnvelope("EXT_RETENTION", "manifest retention is outside policy", "use 0-30 days")
    privacy = payload.get("privacy")
    if (
        not isinstance(privacy, Mapping)
        or privacy.get("redaction") != "secret-bearing-fields"
        or not privacy.get("deletion")
    ):
        return None, ManifestSupportEnvelope(
            "EXT_PRIVACY", "manifest privacy lifecycle is incomplete", "declare redaction policy"
        )
    receipt_path = payload.get("receipt_path")
    try:
        receipt_path = validate_receipt_path(receipt_path)
        _assert_receipt_path_confined(receipt_path)
    except PathConfinementError:
        logger.warning("Extension manifest receipt path failed confinement.", exc_info=True)
        return None, ManifestSupportEnvelope(
            "EXT_RECEIPT", "manifest receipt path is missing", "write receipts under receipts/"
        )

    return (
        ExtensionSdkManifest(
            extension_id=extension_id.strip(),
            schema_version=SCHEMA_VERSION,
            signature=signature,
            permissions=tuple(permissions),
            retention_days=retention_days,
            receipt_path=receipt_path,
        ),
        None,
    )


def _assert_receipt_path_confined(receipt_path: str) -> None:
    normalized = str(receipt_path).replace("\\", "/")
    parts = PurePosixPath(normalized).parts
    if not parts or parts[0] != "receipts" or any(part in {"", ".", ".."} for part in parts):
        raise PathConfinementError("receipt path must stay under receipts/")
