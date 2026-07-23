"""Fail-closed verification for local AI bundle layouts."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from vetinari.workbench.packaging.manifest import (
    AIBundleManifest,
    BundleIntegrityError,
    manifest_digest,
    raw_sha256_digest,
)


class BundleVerificationError(Exception):
    """Raised when a local AI bundle layout cannot be trusted."""


@dataclass(frozen=True, slots=True)
class BundleVerificationReport:
    """Verified bundle metadata."""

    bundle_dir: Path
    manifest: AIBundleManifest
    manifest_blob_digest: str
    component_count: int

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"BundleVerificationReport(bundle_dir={self.bundle_dir!r}, manifest={self.manifest!r}, manifest_blob_digest={self.manifest_blob_digest!r})"


class AIBundleVerifier:
    """Verify manifest, descriptor, and blob digests before trust."""

    def verify(self, bundle_dir: Path | str) -> BundleVerificationReport:
        """Execute the verify operation.

        Returns:
            BundleVerificationReport value produced by verify().

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        root = Path(bundle_dir).resolve()
        try:
            if not root.is_dir():
                raise BundleVerificationError(f"bundle directory missing: {root}")
            layout = _read_json(root / "oci-layout")
            if layout.get("imageLayoutVersion") != "1.0.0":
                raise BundleVerificationError("oci-layout imageLayoutVersion mismatch")
            index = _read_json(root / "index.json")
            manifests = index.get("manifests")
            if not isinstance(manifests, list) or len(manifests) != 1:
                raise BundleVerificationError("index.json must contain exactly one AI bundle manifest descriptor")
            descriptor = manifests[0]
            if not isinstance(descriptor, dict):
                raise BundleVerificationError("index manifest descriptor must be an object")
            expected_manifest_digest = _string(descriptor.get("digest"), "index.manifests[0].digest")
            manifest_path = root / "ai-bundle-manifest.json"
            manifest_bytes = _read_bytes(manifest_path)
            actual_manifest_digest = raw_sha256_digest(manifest_bytes)
            if actual_manifest_digest != expected_manifest_digest:
                raise BundleVerificationError("root manifest bytes do not match index descriptor")
            blob_path = root / "blobs" / "sha256" / expected_manifest_digest.removeprefix("sha256:")
            if raw_sha256_digest(_read_bytes(blob_path)) != expected_manifest_digest:
                raise BundleVerificationError("manifest blob digest mismatch")
            payload = json.loads(manifest_bytes.decode("utf-8"))
            manifest = AIBundleManifest.from_dict(payload)
            canonical = manifest.tamper_evidence.get("canonical_manifest_sha256")
            if canonical != manifest_digest(manifest):
                raise BundleVerificationError("canonical manifest digest mismatch")
            descriptor_digests = {
                row.get("digest") for row in manifest.oci_descriptors if row.get("digest", "").startswith("sha256:")
            }
            for component in manifest.components:
                component_path = (root / component.blob_path).resolve()
                if not component_path.is_relative_to(root):
                    raise BundleVerificationError(f"component {component.name!r} blob path escapes bundle root")
                digest = raw_sha256_digest(_read_bytes(component_path))
                if digest != component.digest:
                    raise BundleVerificationError(f"component {component.name!r} blob digest mismatch")
                if component.digest not in descriptor_digests:
                    raise BundleVerificationError(f"component {component.name!r} missing OCI descriptor")
            return BundleVerificationReport(
                bundle_dir=root,
                manifest=manifest,
                manifest_blob_digest=actual_manifest_digest,
                component_count=len(manifest.components),
            )
        except BundleVerificationError:
            raise
        except (BundleIntegrityError, OSError, json.JSONDecodeError, UnicodeDecodeError, ValueError, TypeError) as exc:
            raise BundleVerificationError(str(exc)) from exc


def _read_json(path: Path) -> dict[str, object]:
    payload = json.loads(_read_bytes(path).decode("utf-8"))
    if not isinstance(payload, dict):
        raise BundleVerificationError(f"{path.name} must contain a JSON object")
    return payload


def _read_bytes(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        raise BundleVerificationError(f"{path.name} is missing or unreadable") from exc


def _string(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BundleVerificationError(f"{field_name} must be a non-empty string")
    return value


__all__ = ["AIBundleVerifier", "BundleVerificationError", "BundleVerificationReport"]
