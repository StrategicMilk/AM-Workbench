"""Archive extraction and bundle-content verification for AM Engine releases."""

from __future__ import annotations

import json
import re
import shutil
import stat
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Protocol

from vetinari.engine.binary_manifest import verify_file
from vetinari.engine.release_contract import (
    CARGO_IDENTITY_COUNT,
    CARGO_IDENTITY_SHA256,
    CONVERTER_IDENTITY_COUNT_BY_PLATFORM,
    CONVERTER_IDENTITY_SHA256_BY_PLATFORM,
    CUDA_DYNAMIC_EXTERNAL_LIBRARIES,
    CUDA_EULA_SHA256,
    CUDA_EULA_URL,
    CUDA_VERSION,
    EXPORT_NATIVE_TOOLS,
    dependency_identity_digest,
    export_tool_member,
    export_tool_members,
)
from vetinari.exceptions import EngineBinaryCorruptError, EngineBinaryMissingError


class BundleAsset(Protocol):
    """Release-asset fields required by bundle verification."""

    platform: str | None
    accelerator: str | None


_INNER_MANIFEST_FIELDS = frozenset({"engine_version", "libllama_rev", "min_pkg_version", "artifacts"})
_ARTIFACT_FIELDS = frozenset({"platform", "accel", "file", "sha256", "size_bytes"})
_LICENSE_INDEX_FIELDS = frozenset({
    "schema_version",
    "platform",
    "accelerator",
    "cargo_identity_sha256",
    "converter_identity_sha256",
    "cargo_packages",
    "converter_packages",
    "native_components",
})
_CARGO_LICENSE_FIELDS = frozenset({
    "name",
    "version",
    "license",
    "authors",
    "repository",
    "source",
    "license_files",
    "supplemental_license_files",
    "metadata_file",
})
_CONVERTER_LICENSE_FIELDS = frozenset({
    "name",
    "version",
    "license",
    "marker",
    "license_files",
    "supplemental_license_files",
})
_NATIVE_LICENSE_FIELDS = frozenset({
    "name",
    "version",
    "license",
    "linkage",
    "bundled",
    "evidence_purpose",
    "license_files",
})
_LICENSE_FILE_FIELDS = frozenset({"file", "sha256", "size_bytes"})
_PROVENANCED_LICENSE_FILE_FIELDS = _LICENSE_FILE_FIELDS | {"source_url", "source_revision"}


def _safe_member_path(destination: Path, member_name: str) -> Path:
    member = PurePosixPath(member_name.replace("\\", "/"))
    if member.is_absolute() or ".." in member.parts:
        raise EngineBinaryCorruptError("release archive contains an unsafe path", member=member_name)
    target = (destination / Path(*member.parts)).resolve()
    if destination.resolve() not in target.parents and target != destination.resolve():
        raise EngineBinaryCorruptError("release archive escapes the install directory", member=member_name)
    return target


def _extract_archive(archive: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    if not zipfile.is_zipfile(archive):
        raise EngineBinaryCorruptError("first-party release bundle is not a zip file")
    with zipfile.ZipFile(archive) as bundle:
        for info in bundle.infolist():
            target = _safe_member_path(destination, info.filename)
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            mode = info.external_attr >> 16
            if stat.S_IFMT(mode) and not stat.S_ISREG(mode):
                raise EngineBinaryCorruptError("release archive contains a non-file member", member=info.filename)
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                source = bundle.open(info)
            except (KeyError, RuntimeError) as exc:
                raise EngineBinaryCorruptError("release archive member is unreadable", member=info.filename) from exc
            with source, target.open("wb") as sink:
                shutil.copyfileobj(source, sink)
            archived_mode = info.external_attr >> 16
            if archived_mode:
                target.chmod(stat.S_IMODE(archived_mode))


def _validate_corpus_file_record(
    extracted: Path,
    record: object,
    *,
    require_provenance: bool,
) -> str:
    expected_fields = _PROVENANCED_LICENSE_FILE_FIELDS if require_provenance else _LICENSE_FILE_FIELDS
    if not isinstance(record, dict) or set(record) != expected_fields:
        raise EngineBinaryCorruptError("release bundle license corpus contains an invalid file record")
    filename = record["file"]
    size = record["size_bytes"]
    digest = record["sha256"]
    if not isinstance(filename, str):
        raise EngineBinaryCorruptError("release bundle license corpus contains an invalid file name")
    relative = PurePosixPath(filename)
    if relative.is_absolute() or not relative.parts or ".." in relative.parts or relative.parts[0] == "ENGINE_LICENSES":
        raise EngineBinaryCorruptError("release bundle license corpus contains an unsafe file name")
    if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
        raise EngineBinaryCorruptError("release bundle license corpus contains an invalid file size")
    if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise EngineBinaryCorruptError("release bundle license corpus contains an invalid file digest")
    if require_provenance and (
        not isinstance(record["source_url"], str)
        or not record["source_url"].startswith("https://")
        or not isinstance(record["source_revision"], str)
        or not record["source_revision"]
    ):
        raise EngineBinaryCorruptError("release bundle license corpus contains invalid source provenance")
    corpus_name = f"ENGINE_LICENSES/{relative.as_posix()}"
    corpus_path = extracted / Path(*PurePosixPath(corpus_name).parts)
    if not corpus_path.is_file() or corpus_path.stat().st_size != size:
        raise EngineBinaryCorruptError("release bundle license corpus file is missing or has the wrong size")
    verify_file(corpus_path, digest)
    return corpus_name


def _validate_license_corpus(
    extracted: Path,
    selected: BundleAsset,
    license_files: set[str],
) -> None:
    index_path = extracted / "ENGINE_LICENSES" / "INDEX.json"
    try:
        index = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise EngineBinaryCorruptError("release bundle license corpus index is unreadable") from exc
    if not isinstance(index, dict) or set(index) != _LICENSE_INDEX_FIELDS or index["schema_version"] != 1:
        raise EngineBinaryCorruptError("release bundle license corpus index fields are invalid")
    if index["platform"] != selected.platform:
        raise EngineBinaryCorruptError("release bundle license corpus platform is invalid")
    if index["accelerator"] != selected.accelerator:
        raise EngineBinaryCorruptError("release bundle license corpus accelerator is invalid")
    cargo_rows = index["cargo_packages"]
    converter_rows = index["converter_packages"]
    native_rows = index["native_components"]
    if not isinstance(cargo_rows, list) or len(cargo_rows) != CARGO_IDENTITY_COUNT:
        raise EngineBinaryCorruptError("release bundle license corpus has incomplete Cargo coverage")
    expected_converter_count = CONVERTER_IDENTITY_COUNT_BY_PLATFORM[selected.platform]
    if not isinstance(converter_rows, list) or len(converter_rows) != expected_converter_count:
        raise EngineBinaryCorruptError("release bundle license corpus has incomplete converter coverage")
    if not isinstance(native_rows, list):
        raise EngineBinaryCorruptError("release bundle license corpus native coverage is invalid")
    indexed_files = {"ENGINE_LICENSES/INDEX.json"}
    identities: set[tuple[str, str]] = set()
    for row in cargo_rows:
        if not isinstance(row, dict) or set(row) != _CARGO_LICENSE_FIELDS:
            raise EngineBinaryCorruptError("release bundle license corpus contains an invalid Cargo row")
        identity = row["name"], row["version"]
        if not all(isinstance(value, str) and value for value in identity) or identity in identities:
            raise EngineBinaryCorruptError("release bundle license corpus contains an invalid Cargo identity")
        identities.add(identity)
        if (
            not isinstance(row["license"], str)
            or not row["license"]
            or not isinstance(row["source"], str)
            or not row["source"]
            or not isinstance(row["authors"], list)
            or not all(isinstance(author, str) for author in row["authors"])
            or (row["repository"] is not None and not isinstance(row["repository"], str))
        ):
            raise EngineBinaryCorruptError("release bundle license corpus contains invalid Cargo metadata")
        own_files = row["license_files"]
        supplemental_files = row["supplemental_license_files"]
        if (
            not isinstance(own_files, list)
            or not isinstance(supplemental_files, list)
            or not (own_files or supplemental_files)
        ):
            raise EngineBinaryCorruptError("release bundle license corpus has an unattributed Cargo package")
        indexed_files.update(
            _validate_corpus_file_record(extracted, record, require_provenance=False) for record in own_files
        )
        indexed_files.update(
            _validate_corpus_file_record(extracted, record, require_provenance=True) for record in supplemental_files
        )
        metadata_name = _validate_corpus_file_record(extracted, row["metadata_file"], require_provenance=False)
        indexed_files.add(metadata_name)
        try:
            metadata = json.loads((extracted / Path(*PurePosixPath(metadata_name).parts)).read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise EngineBinaryCorruptError("release bundle Cargo package metadata is unreadable") from exc
        metadata_fields = {"name", "version", "license", "authors", "repository", "source"}
        if (
            not isinstance(metadata, dict)
            or set(metadata) != metadata_fields
            or any(metadata[field] != row[field] for field in metadata_fields)
        ):
            raise EngineBinaryCorruptError("release bundle Cargo package metadata disagrees with its index row")
    cargo_identity_sha256 = dependency_identity_digest(identities)
    if index["cargo_identity_sha256"] != cargo_identity_sha256 or cargo_identity_sha256 != CARGO_IDENTITY_SHA256:
        raise EngineBinaryCorruptError("release bundle Cargo dependency identities are not canonical")
    identities.clear()
    for row in converter_rows:
        if not isinstance(row, dict) or set(row) != _CONVERTER_LICENSE_FIELDS:
            raise EngineBinaryCorruptError("release bundle license corpus contains an invalid converter row")
        identity = row["name"], row["version"]
        files = row["license_files"]
        supplemental_files = row["supplemental_license_files"]
        if (
            not all(isinstance(value, str) and value for value in identity)
            or identity in identities
            or not isinstance(files, list)
            or not isinstance(supplemental_files, list)
            or not (files or supplemental_files)
        ):
            raise EngineBinaryCorruptError("release bundle license corpus contains invalid converter coverage")
        identities.add(identity)
        indexed_files.update(
            _validate_corpus_file_record(extracted, record, require_provenance=False) for record in files
        )
        indexed_files.update(
            _validate_corpus_file_record(extracted, record, require_provenance=True) for record in supplemental_files
        )
    converter_identity_sha256 = dependency_identity_digest(identities)
    if (
        index["converter_identity_sha256"] != converter_identity_sha256
        or converter_identity_sha256 != CONVERTER_IDENTITY_SHA256_BY_PLATFORM[selected.platform]
    ):
        raise EngineBinaryCorruptError("release bundle converter dependency identities are not canonical")
    observed_native: set[str] = set()
    for row in native_rows:
        if not isinstance(row, dict) or set(row) != _NATIVE_LICENSE_FIELDS:
            raise EngineBinaryCorruptError("release bundle license corpus contains an invalid native row")
        name = row["name"]
        files = row["license_files"]
        if not isinstance(name, str) or name in observed_native or not isinstance(files, list) or not files:
            raise EngineBinaryCorruptError("release bundle license corpus contains invalid native coverage")
        observed_native.add(name)
        if selected.accelerator == "cuda" and (
            row["version"] != CUDA_VERSION
            or row["license"] != "LicenseRef-NVIDIA-CUDA-Toolkit-EULA-12.4"
            or row["linkage"] != "dynamic_external"
            or row["bundled"] is not False
            or row["evidence_purpose"] != "external_runtime_prerequisite_terms_and_third_party_notices"
            or any(
                not isinstance(record, dict)
                or record.get("source_url") != CUDA_EULA_URL
                or record.get("sha256") != CUDA_EULA_SHA256
                for record in files
            )
        ):
            raise EngineBinaryCorruptError("release bundle CUDA license evidence is invalid")
        indexed_files.update(
            _validate_corpus_file_record(extracted, record, require_provenance=True) for record in files
        )
    expected_native = frozenset(CUDA_DYNAMIC_EXTERNAL_LIBRARIES) if selected.accelerator == "cuda" else frozenset()
    if observed_native != expected_native:
        raise EngineBinaryCorruptError("release bundle license corpus has incomplete native coverage")
    if indexed_files != license_files:
        raise EngineBinaryCorruptError(
            "release bundle license corpus files do not match its index",
            missing=sorted(indexed_files - license_files),
            unexpected=sorted(license_files - indexed_files),
        )


def _verify_extracted_bundle(
    extracted: Path,
    selected: BundleAsset,
    release_manifest: dict[str, object],
) -> Path:
    """Verify the exact inner bundle manifest and return its server path."""
    suffix = ".exe" if selected.platform == "windows" else ""
    expected_files = {
        f"amw-engine-server{suffix}",
        "requirements-convert_lora_to_gguf.txt",
        "LICENSE.llama.cpp",
        "NOTICE",
        "ENGINE_THIRD_PARTY_LICENSES.md",
    } | set(export_tool_members(platform=selected.platform))
    if any(path.is_symlink() for path in extracted.rglob("*")):
        raise EngineBinaryCorruptError("release bundle contains a symbolic link")
    actual_files = {path.relative_to(extracted).as_posix() for path in extracted.rglob("*") if path.is_file()}
    converter_files = {
        filename
        for filename in actual_files
        if (filename.startswith("conversion/") and filename.endswith(".py"))
        or (filename.startswith("gguf-py/gguf/") and (filename.endswith(".py") or filename == "gguf-py/gguf/py.typed"))
    }
    required_converter_files = {
        "conversion/__init__.py",
        "conversion/base.py",
        "gguf-py/gguf/__init__.py",
        "gguf-py/gguf/constants.py",
        "gguf-py/gguf/py.typed",
    }
    license_files = {filename for filename in actual_files if filename.startswith("ENGINE_LICENSES/")}
    contracted_files = expected_files | required_converter_files | converter_files | license_files | {"manifest.json"}
    if actual_files != contracted_files:
        raise EngineBinaryCorruptError(
            "release bundle content does not match the first-party contract",
            missing=sorted(contracted_files - actual_files),
            unexpected=sorted(actual_files - contracted_files),
        )
    if "ENGINE_LICENSES/INDEX.json" not in license_files:
        raise EngineBinaryCorruptError("release bundle has no substantive third-party license corpus")
    _validate_license_corpus(extracted, selected, license_files)
    manifest_path = extracted / "manifest.json"
    try:
        inner = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise EngineBinaryCorruptError("release bundle inner manifest is unreadable") from exc
    if not isinstance(inner, dict) or set(inner) != _INNER_MANIFEST_FIELDS:
        raise EngineBinaryCorruptError("release bundle inner manifest fields are invalid")
    for field in ("engine_version", "libllama_rev", "min_pkg_version"):
        if inner[field] != release_manifest[field]:
            raise EngineBinaryCorruptError("release bundle identity disagrees with the release manifest", field=field)
    rows = inner["artifacts"]
    manifested_files = actual_files - {"manifest.json"}
    if not isinstance(rows, list) or len(rows) != len(manifested_files):
        raise EngineBinaryCorruptError("release bundle inner manifest has an invalid artifact set")
    observed_files: set[str] = set()
    for row in rows:
        if not isinstance(row, dict) or set(row) != _ARTIFACT_FIELDS:
            raise EngineBinaryCorruptError("release bundle inner manifest contains an invalid artifact row")
        filename = row["file"]
        if not isinstance(filename, str) or filename not in manifested_files or filename in observed_files:
            raise EngineBinaryCorruptError("release bundle inner manifest contains an invalid artifact name")
        if row["platform"] != selected.platform or row["accel"] != selected.accelerator:
            raise EngineBinaryCorruptError("release bundle inner manifest contains an invalid platform selector")
        size = row["size_bytes"]
        digest = row["sha256"]
        if (
            isinstance(size, bool)
            or not isinstance(size, int)
            or size < 0
            or (size == 0 and filename != "gguf-py/gguf/py.typed")
        ):
            raise EngineBinaryCorruptError("release bundle inner manifest contains an invalid artifact size")
        if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise EngineBinaryCorruptError("release bundle inner manifest contains an invalid artifact digest")
        artifact_path = extracted / filename
        if artifact_path.stat().st_size != size:
            raise EngineBinaryCorruptError("release bundle artifact size does not match its inner manifest")
        verify_file(artifact_path, digest)
        observed_files.add(filename)
    if observed_files != manifested_files:
        raise EngineBinaryCorruptError("release bundle inner manifest is incomplete")
    if selected.platform == "linux":
        native_members = {f"amw-engine-server{suffix}"} | {
            export_tool_member(tool, platform=selected.platform) for tool in EXPORT_NATIVE_TOOLS
        }
        for member in actual_files:
            mode = stat.S_IMODE(extracted.joinpath(member).stat().st_mode)
            expected_mode = 0o755 if member in native_members else 0o644
            if mode != expected_mode:
                raise EngineBinaryCorruptError(
                    "release bundle member mode does not match the exact release contract",
                    member=member,
                    mode=f"{mode:o}",
                    expected_mode=f"{expected_mode:o}",
                )
    return extracted / f"amw-engine-server{suffix}"


@dataclass(frozen=True)
class _InstalledBundleIdentity:
    platform: str
    accelerator: str


def resolve_bundle_tool(tool: str, *, user_dir: Path | None = None) -> Path:
    """Resolve a tool only from a completely verified canonical installation.

    No environment, ``PATH``, checkout, or vendored-source fallback is
    consulted.  The exact installed tree, inner manifest, license corpus,
    digests, and native modes are revalidated before returning a path.

    Raises:
        EngineBinaryMissingError: If the canonical installation or member is
            absent.
        EngineBinaryCorruptError: If the install is altered or uncontracted.
    """
    platform = "windows" if __import__("os").name == "nt" else "linux"
    try:
        member = export_tool_member(tool, platform=platform)
    except ValueError as exc:
        raise EngineBinaryCorruptError("unknown AM Engine export tool", tool=tool) from exc

    # Local import avoids a module cycle: binary.py owns canonical discovery and
    # imports this module for archive verification.
    from vetinari.engine import binary as binary_module

    _release_commit, _release_manifest_sha256, inner_manifest_digests = binary_module._require_release_authority()
    canonical_root = binary_module.canonical_binary_path(user_dir).parent
    if not canonical_root.is_dir() or canonical_root.is_symlink():
        raise EngineBinaryMissingError("AM Engine canonical installation is missing", path=str(canonical_root))
    manifest_path = canonical_root / "manifest.json"
    try:
        inner = json.loads(manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise EngineBinaryMissingError("AM Engine canonical installation manifest is missing") from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise EngineBinaryCorruptError("AM Engine canonical installation manifest is unreadable") from exc
    observed_manifest_sha256 = binary_module.sha256_file(manifest_path)
    matching_bundles = [key for key, digest in inner_manifest_digests.items() if digest == observed_manifest_sha256]
    if len(matching_bundles) != 1:
        raise EngineBinaryCorruptError(
            "AM Engine canonical installation manifest is not bound to the independent release authority",
            observed_sha256=observed_manifest_sha256,
        )
    authoritative_platform, authoritative_accelerator = matching_bundles[0].split("-", 1)
    if not isinstance(inner, dict) or inner.get("engine_version") != binary_module.EXPECTED_ENGINE_VERSION:
        raise EngineBinaryCorruptError(
            "AM Engine canonical installation does not match the pinned release",
            expected=binary_module.EXPECTED_ENGINE_VERSION,
            observed=inner.get("engine_version") if isinstance(inner, dict) else None,
        )
    rows = inner.get("artifacts") if isinstance(inner, dict) else None
    if not isinstance(rows, list) or not rows or not isinstance(rows[0], dict):
        raise EngineBinaryCorruptError("AM Engine canonical installation manifest has no artifact identity")
    accelerator = rows[0].get("accel")
    if accelerator != authoritative_accelerator or platform != authoritative_platform:
        raise EngineBinaryCorruptError("AM Engine canonical installation accelerator identity is invalid")
    candidate = canonical_root / member
    if not candidate.is_file() or candidate.is_symlink():
        raise EngineBinaryMissingError("AM Engine export tool is missing", tool=tool, path=str(candidate))
    _verify_extracted_bundle(canonical_root, _InstalledBundleIdentity(platform, authoritative_accelerator), inner)
    resolved_root = canonical_root.resolve(strict=True)
    resolved = candidate.resolve(strict=True)
    if resolved.parent != resolved_root:
        raise EngineBinaryCorruptError("AM Engine export tool escapes the canonical installation", tool=tool)
    return resolved


def resolve_bootstrap_bundle_tool(
    tool: str,
    *,
    bundle_root: Path,
    platform: str,
    accelerator: str,
    expected_inner_manifest_sha256: str,
) -> Path:
    """Resolve a tool from an explicitly release-bootstrap-validated tree.

    This path is only for producing the first immutable release. Its caller
    supplies the digest measured from the already validated release artifact;
    it never represents a post-release canonical consumer authority.
    """
    try:
        member = export_tool_member(tool, platform=platform)
    except ValueError as exc:
        raise EngineBinaryCorruptError("unknown AM Engine export tool", tool=tool) from exc
    root = bundle_root.resolve(strict=True)
    if not root.is_dir() or root.is_symlink():
        raise EngineBinaryMissingError("AM Engine bootstrap bundle is missing", path=str(root))
    manifest_path = root / "manifest.json"
    if re.fullmatch(r"[0-9a-f]{64}", expected_inner_manifest_sha256) is None:
        raise EngineBinaryCorruptError("AM Engine bootstrap manifest digest is invalid")
    verify_file(manifest_path, expected_inner_manifest_sha256)
    try:
        inner = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise EngineBinaryCorruptError("AM Engine bootstrap manifest is unreadable") from exc
    _verify_extracted_bundle(root, _InstalledBundleIdentity(platform, accelerator), inner)
    candidate = root / member
    if not candidate.is_file() or candidate.is_symlink() or candidate.resolve(strict=True).parent != root:
        raise EngineBinaryMissingError("AM Engine bootstrap export tool is missing", tool=tool)
    return candidate.resolve(strict=True)
