"""Pure-Python local OCI-like layout descriptors for AI bundles."""

from __future__ import annotations

import json
import os
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path

from vetinari.workbench.packaging.manifest import (
    AIBundleManifest,
    BundleIntegrityError,
    manifest_digest,
    manifest_json_bytes,
    raw_sha256_digest,
)

_OCI_LAYOUT_MEDIA_TYPE = "application/vnd.oci.layout.header.v1+json"
_AI_BUNDLE_MEDIA_TYPE = "application/vnd.vetinari.workbench.ai-bundle.manifest.v1+json"


@dataclass(frozen=True, slots=True)
class OCIDescriptor:
    """Local descriptor row compatible with OCI descriptor vocabulary."""

    media_type: str
    digest: str
    size: int
    artifact_type: str
    annotations: dict[str, str]

    def to_dict(self) -> dict[str, object]:
        return {
            "mediaType": self.media_type,
            "digest": self.digest,
            "size": self.size,
            "artifactType": self.artifact_type,
            "annotations": dict(sorted(self.annotations.items())),
        }

    def to_manifest_row(self) -> dict[str, str]:
        return {
            "media_type": self.media_type,
            "digest": self.digest,
            "size": str(self.size),
            "artifact_type": self.artifact_type,
            **{f"annotation:{key}": value for key, value in sorted(self.annotations.items())},
        }

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"OCIDescriptor(media_type={self.media_type!r}, digest={self.digest!r}, size={self.size!r})"


def write_oci_layout(
    *,
    manifest: AIBundleManifest,
    blobs: dict[str, bytes],
    destination: Path | str,
) -> AIBundleManifest:
    """Write a local OCI-like layout atomically and return the persisted manifest.

    Returns:
        AIBundleManifest value produced by write_oci_layout().

    Raises:
        Exception: Propagates validation or runtime failures from the underlying operation.
    """
    destination_path = Path(destination).resolve()
    if destination_path.exists():
        raise BundleIntegrityError(f"bundle destination already exists: {destination_path}")
    if not blobs:
        raise BundleIntegrityError("bundle layout requires component blobs")
    tmp_path = destination_path.parent / f".{destination_path.name}.tmp-{uuid.uuid4().hex}"
    if tmp_path.exists():
        raise BundleIntegrityError(f"temporary bundle destination already exists: {tmp_path}")
    try:
        blobs_root = tmp_path / "blobs" / "sha256"
        blobs_root.mkdir(parents=True)
        descriptor_rows: list[dict[str, str]] = []
        for component in manifest.components:
            blob = blobs.get(component.name)
            if blob is None:
                raise BundleIntegrityError(f"missing blob content for component {component.name!r}")
            digest = raw_sha256_digest(blob)
            if digest != component.digest:
                raise BundleIntegrityError(f"component {component.name!r} digest mismatch before write")
            blob_path = tmp_path / component.blob_path
            if blob_path.resolve().parent != blobs_root.resolve():
                raise BundleIntegrityError(f"component {component.name!r} blob path escapes blobs root")
            _write_bytes(blob_path, blob)
            if raw_sha256_digest(blob_path.read_bytes()) != component.digest:
                raise BundleIntegrityError(f"component {component.name!r} digest mismatch after write")
            descriptor_rows.append(
                OCIDescriptor(
                    media_type=component.media_type,
                    digest=component.digest,
                    size=component.size_bytes,
                    artifact_type=f"application/vnd.vetinari.workbench.ai-bundle.{component.kind.value}.v1",
                    annotations={"org.opencontainers.image.title": component.name},
                ).to_manifest_row()
            )

        manifest_with_descriptors = manifest.with_oci_descriptors(tuple(descriptor_rows))
        canonical_digest = manifest_digest(manifest_with_descriptors)
        manifest_with_tamper = manifest_with_descriptors.with_tamper_evidence(
            canonical_manifest_sha256=canonical_digest,
            descriptor_authority="local-vetinari-manifest",
        )
        manifest_bytes = manifest_json_bytes(manifest_with_tamper)
        manifest_blob_digest = raw_sha256_digest(manifest_bytes)
        manifest_blob_path = tmp_path / "blobs" / "sha256" / manifest_blob_digest.removeprefix("sha256:")
        _write_bytes(manifest_blob_path, manifest_bytes)
        _write_bytes(tmp_path / "ai-bundle-manifest.json", manifest_bytes)
        _write_json(tmp_path / "oci-layout", {"imageLayoutVersion": "1.0.0", "mediaType": _OCI_LAYOUT_MEDIA_TYPE})
        manifest_descriptor = OCIDescriptor(
            media_type=_AI_BUNDLE_MEDIA_TYPE,
            digest=manifest_blob_digest,
            size=len(manifest_bytes),
            artifact_type=_AI_BUNDLE_MEDIA_TYPE,
            annotations={
                "org.opencontainers.image.title": manifest_with_tamper.bundle_id,
                "org.vetinari.bundle.kind": manifest_with_tamper.kind.value,
            },
        )
        _write_json(tmp_path / "index.json", {"schemaVersion": 2, "manifests": [manifest_descriptor.to_dict()]})
        _fsync_directory(tmp_path)
        tmp_path.rename(destination_path)
        return manifest_with_tamper
    except Exception:
        if tmp_path.exists():
            shutil.rmtree(tmp_path)
        raise


def _write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())


def _write_json(path: Path, payload: dict[str, object]) -> None:
    _write_bytes(path, json.dumps(payload, sort_keys=True, indent=2).encode("utf-8") + b"\n")


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


__all__ = ["OCIDescriptor", "write_oci_layout"]
