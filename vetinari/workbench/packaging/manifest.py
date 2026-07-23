"""Tamper-evident local AI bundle manifest contract."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any

_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_PROJECT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_SHA256_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_SCHEMA_VERSION = 1


class BundleIntegrityError(Exception):
    """Raised when a bundle manifest or descriptor cannot be trusted."""


class AIBundleKind(str, Enum):
    """Supported local AI bundle layout kinds."""

    OCI_BUNDLE = "oci_bundle"


class AIBundleComponentKind(str, Enum):
    """Component groups required for a reusable AI bundle."""

    MODEL = "model"
    DATASET_SNAPSHOT = "dataset_snapshot"
    CODE = "code"
    PROMPT = "prompt"
    GUARDRAIL = "guardrail"
    POLICY = "policy"
    EVAL_SUITE = "eval_suite"
    CONFIG = "config"
    DOCUMENTATION = "documentation"
    RUNTIME_FACT = "runtime_fact"
    MANIFEST_RECORD = "manifest_record"

    @classmethod
    def required_values(cls) -> frozenset[str]:
        return frozenset(kind.value for kind in cls)


@dataclass(frozen=True, slots=True)
class AIBundleComponent:
    """One content-addressed component in a local AI bundle."""

    name: str
    kind: AIBundleComponentKind
    media_type: str
    digest: str
    size_bytes: int
    blob_path: str
    source: str
    unpack_path: str
    metadata: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_identifier(self.name, "component.name")
        if not isinstance(self.kind, AIBundleComponentKind):
            raise BundleIntegrityError("component.kind must be AIBundleComponentKind")
        _require_non_empty(self.media_type, "component.media_type")
        _require_sha256_digest(self.digest, "component.digest")
        if self.size_bytes < 0:
            raise BundleIntegrityError("component.size_bytes must be non-negative")
        _require_blob_path(self.blob_path, "component.blob_path")
        _require_non_empty(self.source, "component.source")
        _require_relative_unpack_path(self.unpack_path, "component.unpack_path")
        _require_string_map(self.metadata, "component.metadata")

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind.value,
            "media_type": self.media_type,
            "digest": self.digest,
            "size_bytes": self.size_bytes,
            "blob_path": self.blob_path,
            "source": self.source,
            "unpack_path": self.unpack_path,
            "metadata": dict(sorted(self.metadata.items())),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> AIBundleComponent:
        """Execute the from dict operation.

        Returns:
            AIBundleComponent value produced by from_dict().
        """
        _reject_unknown_keys(
            payload,
            {
                "name",
                "kind",
                "media_type",
                "digest",
                "size_bytes",
                "blob_path",
                "source",
                "unpack_path",
                "metadata",
            },
            "component",
        )
        return cls(
            name=_string(payload.get("name"), "component.name"),
            kind=AIBundleComponentKind(_string(payload.get("kind"), "component.kind")),
            media_type=_string(payload.get("media_type"), "component.media_type"),
            digest=_string(payload.get("digest"), "component.digest"),
            size_bytes=int(payload.get("size_bytes", -1)),
            blob_path=_string(payload.get("blob_path"), "component.blob_path"),
            source=_string(payload.get("source"), "component.source"),
            unpack_path=_string(payload.get("unpack_path"), "component.unpack_path"),
            metadata=_string_dict(payload.get("metadata", {}), "component.metadata"),
        )

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"AIBundleComponent(name={self.name!r}, kind={self.kind!r}, media_type={self.media_type!r})"


@dataclass(frozen=True, slots=True)
class AIBundleManifest:
    """Canonical manifest for a locally stored AI bundle."""

    schema_version: int
    bundle_id: str
    project_id: str
    kind: AIBundleKind
    components: tuple[AIBundleComponent, ...]
    dependency_refs: tuple[str, ...]
    provenance_refs: tuple[str, ...]
    runtime_facts: dict[str, str]
    oci_descriptors: tuple[dict[str, str], ...] = ()
    tamper_evidence: dict[str, str] = field(default_factory=dict)
    selective_unpack: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.schema_version != _SCHEMA_VERSION:
            raise BundleIntegrityError("AI bundle schema_version must be 1")
        _require_identifier(self.bundle_id, "bundle_id")
        _require_project_id(self.project_id)
        if not isinstance(self.kind, AIBundleKind):
            raise BundleIntegrityError("kind must be AIBundleKind")
        if not self.components:
            raise BundleIntegrityError("bundle requires components")
        names = [component.name for component in self.components]
        if len(names) != len(set(names)):
            raise BundleIntegrityError("component names must be unique")
        present = {component.kind.value for component in self.components}
        missing = sorted(AIBundleComponentKind.required_values() - present)
        if missing:
            raise BundleIntegrityError(f"bundle missing required component groups: {', '.join(missing)}")
        for ref in self.dependency_refs:
            _require_non_empty(ref, "dependency_refs[]")
        for ref in self.provenance_refs:
            _require_non_empty(ref, "provenance_refs[]")
        if not self.dependency_refs:
            raise BundleIntegrityError("bundle requires dependency_refs")
        if not self.provenance_refs:
            raise BundleIntegrityError("bundle requires provenance_refs")
        _require_string_map(self.runtime_facts, "runtime_facts")
        if not self.runtime_facts:
            raise BundleIntegrityError("bundle requires runtime_facts")
        _require_descriptor_rows(self.oci_descriptors)
        _require_string_map(self.tamper_evidence, "tamper_evidence")
        _require_string_map(self.selective_unpack, "selective_unpack")
        if "root" not in self.selective_unpack:
            raise BundleIntegrityError("selective_unpack.root is required")

    def to_dict(self, *, include_tamper_evidence: bool = True) -> dict[str, Any]:
        """Execute the to dict operation.

        Returns:
            dict[str, Any] value produced by to_dict().
        """
        payload: dict[str, Any] = {
            "schema_version": self.schema_version,
            "bundle_id": self.bundle_id,
            "project_id": self.project_id,
            "kind": self.kind.value,
            "components": [component.to_dict() for component in self.components],
            "dependency_refs": list(self.dependency_refs),
            "provenance_refs": list(self.provenance_refs),
            "runtime_facts": dict(sorted(self.runtime_facts.items())),
            "oci_descriptors": [dict(sorted(row.items())) for row in self.oci_descriptors],
            "selective_unpack": dict(sorted(self.selective_unpack.items())),
        }
        if include_tamper_evidence:
            payload["tamper_evidence"] = dict(sorted(self.tamper_evidence.items()))
        return payload

    def with_tamper_evidence(self, **values: str) -> AIBundleManifest:
        """Execute the with tamper evidence operation.

        Returns:
            AIBundleManifest value produced by with_tamper_evidence().
        """
        merged = dict(self.tamper_evidence)
        merged.update(values)
        return replace(self, tamper_evidence=merged)

    def with_oci_descriptors(self, descriptors: tuple[dict[str, str], ...]) -> AIBundleManifest:
        return replace(self, oci_descriptors=descriptors)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> AIBundleManifest:
        """Execute the from dict operation.

        Returns:
            AIBundleManifest value produced by from_dict().

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        _reject_unknown_keys(
            payload,
            {
                "schema_version",
                "bundle_id",
                "project_id",
                "kind",
                "components",
                "dependency_refs",
                "provenance_refs",
                "runtime_facts",
                "oci_descriptors",
                "tamper_evidence",
                "selective_unpack",
            },
            "manifest",
        )
        components = payload.get("components")
        if not isinstance(components, list):
            raise BundleIntegrityError("manifest.components must be a list")
        return cls(
            schema_version=int(payload.get("schema_version", -1)),
            bundle_id=_string(payload.get("bundle_id"), "bundle_id"),
            project_id=_string(payload.get("project_id"), "project_id"),
            kind=AIBundleKind(_string(payload.get("kind"), "kind")),
            components=tuple(AIBundleComponent.from_dict(row) for row in components),
            dependency_refs=tuple(_string(row, "dependency_refs[]") for row in payload.get("dependency_refs", ())),
            provenance_refs=tuple(_string(row, "provenance_refs[]") for row in payload.get("provenance_refs", ())),
            runtime_facts=_string_dict(payload.get("runtime_facts"), "runtime_facts"),
            oci_descriptors=tuple(_string_dict(row, "oci_descriptors[]") for row in payload.get("oci_descriptors", ())),
            tamper_evidence=_string_dict(payload.get("tamper_evidence", {}), "tamper_evidence"),
            selective_unpack=_string_dict(payload.get("selective_unpack"), "selective_unpack"),
        )

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"AIBundleManifest(schema_version={self.schema_version!r}, bundle_id={self.bundle_id!r}, project_id={self.project_id!r})"


def canonical_manifest_bytes(manifest: AIBundleManifest) -> bytes:
    """Return canonical JSON bytes used for stable manifest hashing.

    Returns:
        bytes value produced by canonical_manifest_bytes().
    """
    payload = manifest.to_dict(include_tamper_evidence=False)
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def manifest_digest(manifest: AIBundleManifest) -> str:
    """Return the SHA-256 digest for the canonical manifest payload."""
    return f"sha256:{hashlib.sha256(canonical_manifest_bytes(manifest)).hexdigest()}"


def manifest_json_bytes(manifest: AIBundleManifest) -> bytes:
    """Return the persisted manifest JSON bytes."""
    return json.dumps(manifest.to_dict(), sort_keys=True, indent=2).encode("utf-8") + b"\n"


def raw_sha256_digest(data: bytes) -> str:
    """Return a prefixed SHA-256 digest for raw bytes."""
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def _require_non_empty(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise BundleIntegrityError(f"{field_name} must be non-empty")


def _require_identifier(value: str, field_name: str) -> None:
    _require_non_empty(value, field_name)
    if _IDENTIFIER_RE.fullmatch(value) is None:
        raise BundleIntegrityError(f"{field_name} must match {_IDENTIFIER_RE.pattern}")


def _require_project_id(value: str) -> None:
    _require_non_empty(value, "project_id")
    if _PROJECT_ID_RE.fullmatch(value) is None:
        raise BundleIntegrityError("project_id fails path-safe identifier validation")


def _require_sha256_digest(value: str, field_name: str) -> None:
    if _SHA256_DIGEST_RE.fullmatch(value) is None:
        raise BundleIntegrityError(f"{field_name} must be a sha256:<64 lowercase hex> digest")


def _require_blob_path(value: str, field_name: str) -> None:
    _require_non_empty(value, field_name)
    if not value.startswith("blobs/sha256/"):
        raise BundleIntegrityError(f"{field_name} must live under blobs/sha256/")
    if "\\" in value or ".." in value:
        raise BundleIntegrityError(f"{field_name} must be a normalized relative blob path")


def _require_relative_unpack_path(value: str, field_name: str) -> None:
    _require_non_empty(value, field_name)
    if "\\" in value or "\x00" in value:
        raise BundleIntegrityError(f"{field_name} must use normalized POSIX separators")
    if value.startswith(("/", "../")) or "/../" in value or value == "..":
        raise BundleIntegrityError(f"{field_name} must stay relative to the unpack root")
    if ":" in value.split("/", 1)[0]:
        raise BundleIntegrityError(f"{field_name} must not contain drive-prefixed paths")


def _require_string_map(value: dict[str, str], field_name: str) -> None:
    if not isinstance(value, dict):
        raise BundleIntegrityError(f"{field_name} must be a string map")
    for key, item in value.items():
        _require_non_empty(str(key), f"{field_name}.key")
        _require_non_empty(str(item), f"{field_name}.{key}")


def _require_descriptor_rows(rows: tuple[dict[str, str], ...]) -> None:
    for row in rows:
        _require_string_map(row, "oci_descriptors[]")
        if "digest" in row:
            _require_sha256_digest(row["digest"], "oci_descriptors[].digest")


def _string(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise BundleIntegrityError(f"{field_name} must be a string")
    _require_non_empty(value, field_name)
    return value


def _string_dict(value: Any, field_name: str) -> dict[str, str]:
    if not isinstance(value, dict):
        raise BundleIntegrityError(f"{field_name} must be a string map")
    return {str(key): _string(item, f"{field_name}.{key}") for key, item in value.items()}


def _reject_unknown_keys(payload: dict[str, Any], allowed: set[str], label: str) -> None:
    if not isinstance(payload, dict):
        raise BundleIntegrityError(f"{label} must be an object")
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise BundleIntegrityError(f"{label} has unknown fields: {', '.join(unknown)}")


__all__ = [
    "AIBundleComponent",
    "AIBundleComponentKind",
    "AIBundleKind",
    "AIBundleManifest",
    "BundleIntegrityError",
    "canonical_manifest_bytes",
    "manifest_digest",
    "manifest_json_bytes",
    "raw_sha256_digest",
]
