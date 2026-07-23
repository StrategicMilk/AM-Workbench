"""Canonical extension manifest signatures for local admission checks."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from collections.abc import Mapping
from typing import Any

from vetinari.workbench.extensions.contracts import ExtensionContractError, ExtensionManifest

SIGNATURE_PREFIX = "sha256:extension-manifest:v1:"

logger = logging.getLogger(__name__)


class ExtensionSignatureError(ValueError):
    """Raised when an extension signature payload cannot be trusted."""


def canonical_extension_payload(payload: ExtensionManifest | Mapping[str, Any]) -> str:
    """Return the canonical JSON payload used for extension signatures.

    Returns:
        Deterministic JSON string used as the signature input.

    Raises:
        ExtensionSignatureError: If the payload cannot be parsed as an extension manifest.
    """
    try:
        manifest = payload if isinstance(payload, ExtensionManifest) else ExtensionManifest.from_mapping(payload)
    except (ExtensionContractError, ValueError, TypeError) as exc:
        raise ExtensionSignatureError(f"extension manifest is not signable: {exc}") from exc
    return json.dumps(manifest.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def extension_manifest_signature(payload: ExtensionManifest | Mapping[str, Any]) -> str:
    """Return a deterministic signature for the canonical extension payload.

    Returns:
        Versioned SHA-256 signature string for the manifest payload.
    """
    canonical = canonical_extension_payload(payload)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"{SIGNATURE_PREFIX}{digest}"


def verify_extension_manifest_signature(
    payload: ExtensionManifest | Mapping[str, Any],
    signature: str | None,
) -> bool:
    """Verify an extension signature, failing closed on missing or malformed input.

    Args:
        payload: Extension manifest object or mapping to verify.
        signature: Claimed versioned signature string.

    Returns:
        True when the signature matches the canonical payload; False otherwise.
    """
    if not isinstance(signature, str) or not signature.startswith(SIGNATURE_PREFIX):
        return False
    try:
        expected = extension_manifest_signature(payload)
    except ExtensionSignatureError:
        logger.warning("Extension manifest signature verification failed closed.", exc_info=True)
        return False
    return hmac.compare_digest(expected, signature)


__all__ = [
    "SIGNATURE_PREFIX",
    "ExtensionSignatureError",
    "canonical_extension_payload",
    "extension_manifest_signature",
    "verify_extension_manifest_signature",
]
