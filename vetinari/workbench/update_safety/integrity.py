"""Checksum and signature-policy adapters for update manifests."""

from __future__ import annotations

import hashlib
import logging
import re
from pathlib import Path
from typing import Protocol

from vetinari.workbench.update_safety.contracts import (
    UpdateChannel,
    UpdateIntegrityState,
    UpdateIntegrityVerdict,
    UpdateManifest,
)

logger = logging.getLogger(__name__)


_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


class SignatureVerifier(Protocol):
    """Deterministic signature verifier protocol."""

    def __call__(self, manifest: UpdateManifest) -> bool: ...


def verify_update_integrity(
    manifest: UpdateManifest,
    *,
    artifact_root: str | Path = ".",
    signature_verifier: SignatureVerifier | None = None,
) -> UpdateIntegrityVerdict:
    """Verify manifest checksums and signature evidence without adding crypto dependencies.

    Returns:
        UpdateIntegrityVerdict value produced by verify_update_integrity().
    """
    reasons: list[str] = []
    digests: list[str] = []
    if manifest.integrity.checksum_algorithm.lower() != "sha256":
        reasons.append("checksum_algorithm_unsupported")
    require_signature = manifest.channel is UpdateChannel.STABLE or manifest.integrity.require_signature
    if require_signature and not manifest.integrity.signature_evidence.strip():
        reasons.append("signature_evidence_missing")
    if require_signature and signature_verifier is None:
        reasons.append("signature_verifier_unavailable")
    root = Path(artifact_root).resolve()
    for artifact in manifest.artifacts:
        digest = artifact.digest.strip()
        digests.append(digest)
        if _SHA256_RE.fullmatch(digest) is None:
            reasons.append(f"artifact_digest_malformed:{artifact.platform}")
            continue
        if not artifact.local_path:
            reasons.append(f"artifact_evidence_missing:{artifact.platform}")
            continue
        path = (root / artifact.local_path).resolve()
        if not path.is_relative_to(root):
            reasons.append(f"artifact_path_escapes_root:{artifact.platform}")
            continue
        try:
            actual = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            logger.warning("Handled recoverable failure before fallback.", exc_info=True)
            reasons.append(f"artifact_unreadable:{artifact.platform}")
            continue
        if actual != digest:
            reasons.append(f"artifact_checksum_mismatch:{artifact.platform}")
    if require_signature and signature_verifier is not None:
        try:
            if not signature_verifier(manifest):
                reasons.append("signature_verification_failed")
        except Exception as exc:
            reasons.append(f"signature_verifier_error:{type(exc).__name__}")
    if reasons:
        return UpdateIntegrityVerdict(
            state=UpdateIntegrityState.BLOCKED,
            reasons=tuple(reasons),
            artifact_digests=tuple(digests),
            signature_evidence=manifest.integrity.signature_evidence,
        )
    return UpdateIntegrityVerdict(
        state=UpdateIntegrityState.VERIFIED,
        reasons=("integrity_verified",),
        artifact_digests=tuple(digests),
        signature_evidence=manifest.integrity.signature_evidence,
    )


__all__ = ["SignatureVerifier", "verify_update_integrity"]
