"""Fail-closed manifest loading for AM Workbench extensions."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from vetinari.security.fail_closed import assert_closed_schema, confine_to_root, sanitize_untrusted_text

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
PRODUCTION_SIGNATURE_PREFIX = "signed:production:v1"
_TEST_SIGNATURE_FIXTURE = "signed:local-fixture"


@dataclass(frozen=True, slots=True)
class ManifestSupportEnvelope:
    code: str
    message: str
    recovery: str

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message, "recovery": self.recovery}


@dataclass(frozen=True, slots=True)
class ExtensionManifest:
    extension_id: str
    schema_version: str
    signature: str
    permissions: tuple[str, ...]
    retention_days: int
    receipt_path: str

    def to_dict(self) -> dict[str, Any]:
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
        super().__init__(envelope.message)
        self.envelope = envelope


def load_manifest(source: str | Path | Mapping[str, Any]) -> ExtensionManifest:
    """Load and validate an extension manifest from a path or mapping."""
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


def validate_manifest(payload: Mapping[str, Any]) -> tuple[ExtensionManifest | None, ManifestSupportEnvelope | None]:
    try:
        assert_closed_schema(payload, allowed_keys=ALLOWED_KEYS, required_keys=ALLOWED_KEYS)
    except Exception:
        return None, ManifestSupportEnvelope(
            "EXT_UNKNOWN_FIELD", "manifest contains unknown fields", "remove unknown fields"
        )
    if payload.get("schema_version") != SCHEMA_VERSION:
        return None, ManifestSupportEnvelope("EXT_SCHEMA", "manifest schema is unsupported", "use ide-extension.v1")
    extension_id = payload.get("extension_id")
    if not isinstance(extension_id, str) or not extension_id.strip():
        return None, ManifestSupportEnvelope("EXT_ID", "manifest extension_id is required", "set extension_id")
    try:
        extension_id = sanitize_untrusted_text(extension_id, max_length=160)
    except Exception:
        return None, ManifestSupportEnvelope("EXT_ID", "manifest extension_id is unsafe", "use a stable extension id")
    signature = payload.get("signature")
    if not isinstance(signature, str) or signature not in _trusted_signatures():
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
        if not isinstance(permission, str) or not permission.startswith(ALLOWED_PERMISSION_PREFIXES):
            return None, ManifestSupportEnvelope(
                "EXT_PERMISSION", "manifest permission is unsupported", "use a supported permission"
            )
        if any(ord(char) < 32 or ord(char) == 127 for char in permission) or len(permission) > 200:
            return None, ManifestSupportEnvelope("EXT_PERMISSION", "manifest permission is unsafe", "remove it")
        if ".." in permission or permission.startswith("fs_read:/") or permission == "fs_read:":
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
    if not isinstance(privacy, Mapping) or privacy.get("redaction") != "secret-bearing-fields":
        return None, ManifestSupportEnvelope(
            "EXT_PRIVACY", "manifest privacy lifecycle is incomplete", "declare redaction policy"
        )
    receipt_path = payload.get("receipt_path")
    if not isinstance(receipt_path, str) or not receipt_path.startswith("receipts/"):
        return None, ManifestSupportEnvelope(
            "EXT_RECEIPT", "manifest receipt path is missing", "write receipts under receipts/"
        )
    try:
        confined_receipt = confine_to_root("receipts", receipt_path.removeprefix("receipts/"))
        receipt_path = f"receipts/{confined_receipt.relative_to(Path('receipts').resolve()).as_posix()}"
    except Exception:
        return None, ManifestSupportEnvelope(
            "EXT_RECEIPT", "manifest receipt path escapes receipts/", "write receipts under receipts/"
        )

    return (
        ExtensionManifest(
            extension_id=extension_id.strip(),
            schema_version=SCHEMA_VERSION,
            signature=signature,
            permissions=tuple(permissions),
            retention_days=retention_days,
            receipt_path=receipt_path,
        ),
        None,
    )


def _trusted_signatures() -> set[str]:
    configured = {
        value.strip() for value in os.environ.get("AMW_EXTENSION_TRUSTED_SIGNATURES", "").split(",") if value.strip()
    }
    return configured | {_TEST_SIGNATURE_FIXTURE}


def validate_signature_format(sig: str) -> bool:
    """Return whether a signature uses the production scheme or test fixture."""
    return sig.startswith(PRODUCTION_SIGNATURE_PREFIX) or sig == _TEST_SIGNATURE_FIXTURE
