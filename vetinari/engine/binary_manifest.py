"""Integrity contract for first-party AM Engine release manifests.

This module owns the immutable release identity and validation of untrusted
manifest data.  Provisioning and network I/O remain in :mod:`binary`, while
this boundary is deliberately side-effect free apart from reading and hashing
the explicitly supplied files.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from pathlib import Path, PurePosixPath

from vetinari import __version__ as PACKAGE_VERSION
from vetinari.exceptions import EngineBinaryCorruptError, EngineBinaryMissingError, EngineVersionMismatchError

ENGINE_RELEASE_REPOSITORY = "StrategicMilk/AM-Workbench"
PINNED_RELEASE = "v0.1.0"
PINNED_COMMIT = "86a9c79f866799eb0e7e89c03578ccfbcc5d808e"

_HASH_CHUNK_SIZE = 1024 * 1024
_MANIFEST_FIELDS = frozenset({"engine_version", "libllama_rev", "min_pkg_version", "artifacts", "provenance"})
_ARTIFACT_FIELDS = frozenset({"platform", "accel", "file", "sha256", "size_bytes"})
_PROVENANCE_FIELDS = frozenset({
    "repository",
    "source_commit",
    "source_ref",
    "workflow",
    "run_id",
    "toolchain",
    "deterministic_flags",
    "rebuild_inputs",
})
_TOOLCHAIN_FIELDS = frozenset({"rust", "cuda"})
_REBUILD_INPUT_FIELDS = frozenset({
    "source_tree",
    "cargo_lock_sha256",
    "workspace_manifest_sha256",
    "engine_manifest_sha256",
    "engine_build_sha256",
    "vendor_tree_sha256",
    "vendor_license_sha256",
    "converter_requirements_sha256",
    "native_fixture_model_url",
    "native_fixture_model_sha256",
    "native_fixture_license_url",
    "native_fixture_license_sha256",
})
_VERSION_PATTERN = re.compile(r"^(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")
_NATIVE_FIXTURE_MODEL_URL = (
    "https://huggingface.co/tensorblock/tinyllama-15M-stories-GGUF/resolve/"
    "51b755181aac158c3ee689c0bd86f49a8291d1da/tinyllama-15M-stories-Q2_K.gguf"
)
_NATIVE_FIXTURE_MODEL_SHA256 = "f7e39dc9f26f3d39bf59e885349c6eec65880f685322d591f53e6cdb46ceb2e9"
_NATIVE_FIXTURE_LICENSE_URL = (
    "https://huggingface.co/tensorblock/tinyllama-15M-stories-GGUF/resolve/"
    "51b755181aac158c3ee689c0bd86f49a8291d1da/README.md"
)
_NATIVE_FIXTURE_LICENSE_SHA256 = "c8434895da38a8720e24712d2d79a0b4dfba77c94a5307ac974f44c194ad0af7"


def sha256_file(path: Path) -> str:
    """Hash a file in bounded chunks.

    Returns:
        Lowercase hexadecimal SHA-256 digest.
    """
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_HASH_CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


def verify_file(path: Path, expected_sha256: str) -> None:
    """Fail closed when a file is absent or differs from its trusted digest.

    Args:
        path: File to verify.
        expected_sha256: Trusted lowercase SHA-256 digest.

    Raises:
        EngineBinaryMissingError: If the file does not exist.
        EngineBinaryCorruptError: If the digest does not match.
    """
    if not path.is_file():
        raise EngineBinaryMissingError("AM Engine binary is missing", path=str(path))
    actual = sha256_file(path)
    if not hmac.compare_digest(actual, expected_sha256.lower()):
        raise EngineBinaryCorruptError(
            "AM Engine binary failed SHA-256 verification",
            path=str(path),
            expected_sha256=expected_sha256.lower(),
            actual_sha256=actual,
        )


def _version_tuple(value: object, *, field: str) -> tuple[int, int, int]:
    if not isinstance(value, str) or (match := _VERSION_PATTERN.fullmatch(value)) is None:
        raise EngineBinaryCorruptError("AM Engine manifest contains an invalid package version", field=field)
    return tuple(int(match.group(name)) for name in ("major", "minor", "patch"))


def verify_release_manifest(
    manifest_path: Path,
    *,
    artifact_path: Path | None = None,
    installed_version: str = PACKAGE_VERSION,
    expected_repository: str = ENGINE_RELEASE_REPOSITORY,
    expected_release: str = PINNED_RELEASE,
    expected_source_commit: str | None = None,
) -> dict[str, object]:
    """Validate an IS3.7 release manifest and its selected artifact.

    The manifest is untrusted input. Missing fields, unknown fields, malformed
    versions, unsafe artifact names, and ambiguous artifact rows all fail
    closed before an executable is accepted.

    Args:
        manifest_path: Standalone release-asset manifest.
        artifact_path: Downloaded bundle to verify, when available.
        installed_version: Vetinari version used for compatibility comparison.
        expected_repository: First-party GitHub repository identity.
        expected_release: Immutable release tag selected by this package.
        expected_source_commit: Independently distributed peeled release commit.

    Returns:
        Parsed manifest after schema, compatibility, and optional artifact checks.

    Raises:
        EngineBinaryCorruptError: If the manifest or selected artifact is invalid.
        EngineVersionMismatchError: If this Vetinari version is too old.
    """
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise EngineBinaryCorruptError(
            "AM Engine release manifest is missing or unreadable", path=str(manifest_path)
        ) from exc
    if not isinstance(payload, dict):
        raise EngineBinaryCorruptError("AM Engine release manifest must be a JSON object")
    fields = set(payload)
    if fields != _MANIFEST_FIELDS:
        raise EngineBinaryCorruptError(
            "AM Engine release manifest fields do not match IS3.7",
            missing=sorted(_MANIFEST_FIELDS - fields),
            unknown=sorted(fields - _MANIFEST_FIELDS),
        )
    expected_version = expected_release.removeprefix("v")
    if payload["engine_version"] != expected_version:
        raise EngineBinaryCorruptError(
            "AM Engine release manifest has an unexpected engine version",
            expected=expected_version,
            observed=payload["engine_version"],
        )
    if payload["libllama_rev"] != PINNED_COMMIT:
        raise EngineBinaryCorruptError(
            "AM Engine release manifest has an unexpected llama.cpp revision",
            expected=PINNED_COMMIT,
            observed=payload["libllama_rev"],
        )
    provenance = payload["provenance"]
    if not isinstance(provenance, dict) or set(provenance) != _PROVENANCE_FIELDS:
        raise EngineBinaryCorruptError("AM Engine release manifest has missing or invalid provenance")
    if provenance["repository"] != expected_repository:
        raise EngineBinaryCorruptError("AM Engine release provenance has an unexpected repository identity")
    source_ref = f"refs/tags/{expected_release}"
    if provenance["source_ref"] != source_ref:
        raise EngineBinaryCorruptError("AM Engine release provenance has an unexpected source ref")
    expected_workflow = f"{expected_repository}/.github/workflows/engine.yml@{source_ref}"
    if provenance["workflow"] != expected_workflow:
        raise EngineBinaryCorruptError("AM Engine release provenance has an unexpected workflow identity")
    if not isinstance(provenance["run_id"], str) or re.fullmatch(r"[1-9]\d*", provenance["run_id"]) is None:
        raise EngineBinaryCorruptError("AM Engine release provenance has an invalid workflow run id")
    if (
        not isinstance(provenance["source_commit"], str)
        or _COMMIT_PATTERN.fullmatch(provenance["source_commit"]) is None
    ):
        raise EngineBinaryCorruptError("AM Engine release provenance has an invalid source commit")
    if expected_source_commit is not None and provenance["source_commit"] != expected_source_commit:
        raise EngineBinaryCorruptError("AM Engine release provenance disagrees with the pinned source commit")
    toolchain = provenance["toolchain"]
    if (
        not isinstance(toolchain, dict)
        or set(toolchain) != _TOOLCHAIN_FIELDS
        or toolchain["rust"] != "1.88.0"
        or toolchain["cuda"] != "12.4.1"
    ):
        raise EngineBinaryCorruptError("AM Engine release provenance has an invalid toolchain identity")
    required = _version_tuple(payload["min_pkg_version"], field="min_pkg_version")
    installed = _version_tuple(installed_version, field="installed_version")
    if installed < required:
        raise EngineVersionMismatchError(
            "installed Vetinari package is too old for this AM Engine release",
            expected=str(payload["min_pkg_version"]),
            observed=installed_version,
        )
    artifacts = payload["artifacts"]
    if not isinstance(artifacts, list) or not artifacts:
        raise EngineBinaryCorruptError("AM Engine release manifest has no artifacts")
    validated: list[dict[str, object]] = []
    selectors: set[tuple[str, str]] = set()
    filenames: set[str] = set()
    for row in artifacts:
        if not isinstance(row, dict) or set(row) != _ARTIFACT_FIELDS:
            raise EngineBinaryCorruptError("AM Engine release manifest contains an invalid artifact row")
        filename = row["file"]
        if (
            not isinstance(filename, str)
            or not filename
            or PurePosixPath(filename).name != filename
            or "\\" in filename
        ):
            raise EngineBinaryCorruptError("AM Engine release manifest contains an unsafe artifact name")
        digest = row["sha256"]
        if not isinstance(digest, str) or _SHA256_PATTERN.fullmatch(digest) is None:
            raise EngineBinaryCorruptError("AM Engine release manifest contains an invalid artifact digest")
        size = row["size_bytes"]
        if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
            raise EngineBinaryCorruptError("AM Engine release manifest contains an invalid artifact size")
        if not all(isinstance(row[key], str) and row[key] for key in ("platform", "accel")):
            raise EngineBinaryCorruptError("AM Engine release manifest contains an invalid platform selector")
        platform_name = str(row["platform"])
        accelerator_name = str(row["accel"])
        if platform_name not in {"windows", "linux"} or accelerator_name not in {"cpu", "cuda"}:
            raise EngineBinaryCorruptError("AM Engine release manifest contains an unsupported platform selector")
        if filename != f"amw-engine-{platform_name}-{accelerator_name}.zip":
            raise EngineBinaryCorruptError("AM Engine release manifest artifact name disagrees with its selector")
        selector = (platform_name, accelerator_name)
        if selector in selectors or filename in filenames:
            raise EngineBinaryCorruptError("AM Engine release manifest contains duplicate artifact identity")
        selectors.add(selector)
        filenames.add(filename)
        validated.append(row)
    expected_selectors = {
        (platform_name, accelerator_name)
        for platform_name in ("windows", "linux")
        for accelerator_name in ("cpu", "cuda")
    }
    if selectors != expected_selectors:
        raise EngineBinaryCorruptError(
            "AM Engine release manifest does not contain the complete release matrix",
            missing=sorted(expected_selectors - selectors),
            unexpected=sorted(selectors - expected_selectors),
        )
    deterministic_flags = provenance["deterministic_flags"]
    if not isinstance(deterministic_flags, dict) or set(deterministic_flags) != filenames:
        raise EngineBinaryCorruptError("AM Engine release provenance has invalid per-artifact deterministic_flags")
    for filename, values in deterministic_flags.items():
        if (
            not isinstance(values, list)
            or not values
            or not all(isinstance(value, str) and value for value in values)
            or len(values) != len(set(values))
        ):
            raise EngineBinaryCorruptError(
                "AM Engine release provenance has malformed deterministic_flags",
                artifact=filename,
            )
    rebuild_inputs = provenance["rebuild_inputs"]
    if not isinstance(rebuild_inputs, dict) or set(rebuild_inputs) != filenames:
        raise EngineBinaryCorruptError("AM Engine release provenance has invalid per-artifact rebuild_inputs")
    observed_materials: set[str] = set()
    for filename, materials in rebuild_inputs.items():
        if not isinstance(materials, dict) or set(materials) != _REBUILD_INPUT_FIELDS:
            raise EngineBinaryCorruptError(
                "AM Engine release provenance has malformed rebuild_inputs",
                artifact=filename,
            )
        source_tree = materials["source_tree"]
        if not isinstance(source_tree, str) or _COMMIT_PATTERN.fullmatch(source_tree) is None:
            raise EngineBinaryCorruptError("AM Engine release provenance has an invalid source tree", artifact=filename)
        for field in _REBUILD_INPUT_FIELDS - {
            "source_tree",
            "native_fixture_model_url",
            "native_fixture_license_url",
        }:
            digest = materials[field]
            if not isinstance(digest, str) or _SHA256_PATTERN.fullmatch(digest) is None:
                raise EngineBinaryCorruptError(
                    "AM Engine release provenance has an invalid material digest",
                    artifact=filename,
                    field=field,
                )
        if materials["native_fixture_model_url"] != _NATIVE_FIXTURE_MODEL_URL:
            raise EngineBinaryCorruptError("AM Engine release provenance has an invalid fixture model URL")
        if materials["native_fixture_model_sha256"] != _NATIVE_FIXTURE_MODEL_SHA256:
            raise EngineBinaryCorruptError("AM Engine release provenance has an invalid fixture model digest")
        if materials["native_fixture_license_url"] != _NATIVE_FIXTURE_LICENSE_URL:
            raise EngineBinaryCorruptError("AM Engine release provenance has an invalid fixture license URL")
        if materials["native_fixture_license_sha256"] != _NATIVE_FIXTURE_LICENSE_SHA256:
            raise EngineBinaryCorruptError("AM Engine release provenance has an invalid fixture license digest")
        observed_materials.add(json.dumps(materials, sort_keys=True, separators=(",", ":")))
    if len(observed_materials) != 1:
        raise EngineBinaryCorruptError("AM Engine release provenance legs disagree on measured rebuild inputs")
    if artifact_path is not None:
        matches = [row for row in validated if row["file"] == artifact_path.name]
        if len(matches) != 1:
            raise EngineBinaryCorruptError(
                "AM Engine release manifest does not select exactly one downloaded artifact",
                artifact=artifact_path.name,
            )
        selected = matches[0]
        if not artifact_path.is_file() or artifact_path.stat().st_size != selected["size_bytes"]:
            raise EngineBinaryCorruptError("AM Engine artifact size does not match the release manifest")
        verify_file(artifact_path, str(selected["sha256"]))
    return payload


__all__ = [
    "ENGINE_RELEASE_REPOSITORY",
    "PINNED_COMMIT",
    "PINNED_RELEASE",
    "sha256_file",
    "verify_file",
    "verify_release_manifest",
]
