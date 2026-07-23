"""Pinned first-party AM Engine discovery and explicit provisioning.

Discovery is deliberately side-effect free.  Network access occurs only through
``provision_binary`` so an inference or health-check path can never download an
executable implicitly.
"""

from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from urllib.parse import urlparse

import httpx

from vetinari.constants import get_user_dir
from vetinari.engine import binary_attestation as _attestation
from vetinari.engine import binary_manifest as _manifest
from vetinari.engine import release_contract as _release_contract
from vetinari.engine.binary_bundle import (
    _extract_archive,
    _verify_extracted_bundle,
    resolve_bootstrap_bundle_tool,
    resolve_bundle_tool,
)
from vetinari.engine.binary_manifest import (
    ENGINE_RELEASE_REPOSITORY,
    PINNED_COMMIT,
    PINNED_RELEASE,
    sha256_file,
    verify_file,
    verify_release_manifest,
)
from vetinari.exceptions import EngineBinaryCorruptError, EngineBinaryMissingError, EngineVersionMismatchError

# Preserve the legacy diagnostic/test surface while the manifest module owns
# these release-material identities.
_NATIVE_FIXTURE_LICENSE_SHA256 = _manifest._NATIVE_FIXTURE_LICENSE_SHA256
_NATIVE_FIXTURE_LICENSE_URL = _manifest._NATIVE_FIXTURE_LICENSE_URL
_NATIVE_FIXTURE_MODEL_SHA256 = _manifest._NATIVE_FIXTURE_MODEL_SHA256
_NATIVE_FIXTURE_MODEL_URL = _manifest._NATIVE_FIXTURE_MODEL_URL
_ATTESTATION_PAYLOAD_TYPE = _attestation._ATTESTATION_PAYLOAD_TYPE
_MAX_ATTESTATION_RESPONSE_BYTES = _attestation._MAX_ATTESTATION_RESPONSE_BYTES
_fetch_github_attestation_bundles = _attestation._fetch_github_attestation_bundles
_verify_attestation_bundle = _attestation._verify_attestation_bundle
_verify_github_attestation = _attestation._verify_github_attestation
_verify_slsa_statement = _attestation._verify_slsa_statement
CARGO_IDENTITY_COUNT = _release_contract.CARGO_IDENTITY_COUNT
CARGO_IDENTITY_SHA256 = _release_contract.CARGO_IDENTITY_SHA256
CONVERTER_IDENTITY_COUNT_BY_PLATFORM = _release_contract.CONVERTER_IDENTITY_COUNT_BY_PLATFORM
CONVERTER_IDENTITY_SHA256_BY_PLATFORM = _release_contract.CONVERTER_IDENTITY_SHA256_BY_PLATFORM
CUDA_DYNAMIC_EXTERNAL_LIBRARIES = _release_contract.CUDA_DYNAMIC_EXTERNAL_LIBRARIES
CUDA_EULA_SHA256 = _release_contract.CUDA_EULA_SHA256
CUDA_EULA_URL = _release_contract.CUDA_EULA_URL
CUDA_VERSION = _release_contract.CUDA_VERSION
dependency_identity_digest = _release_contract.dependency_identity_digest

# Filled only after the public immutable tag/release has been produced and its
# peeled commit has been independently recorded in a Vetinari package update.
PINNED_RELEASE_COMMIT: str | None = None
# Filled by the same post-release consumer update as ``PINNED_RELEASE_COMMIT``.
# These package-owned values are intentionally independent from the writable
# installation tree. The outer pin binds the attested standalone manifest; the
# per-bundle pins bind the exact installed inner manifests used at resolution.
PINNED_RELEASE_MANIFEST_SHA256: str | None = None
PINNED_INNER_MANIFEST_SHA256_BY_BUNDLE = MappingProxyType({})
EXPECTED_ENGINE_VERSION = PINNED_RELEASE.removeprefix("v")
ENGINE_INSTALL_SUBDIR = "engine"
ENGINE_BINARY_ENV = "VETINARI_ENGINE_BINARY_PATH"
_RELEASE_BASE_URL = f"https://github.com/{ENGINE_RELEASE_REPOSITORY}/releases/download/{PINNED_RELEASE}"
_RELEASE_API_URL = f"https://api.github.com/repos/{ENGINE_RELEASE_REPOSITORY}/releases/tags/{PINNED_RELEASE}"
_DOWNLOAD_HOST = "github.com"
_API_HOST = "api.github.com"
_GITHUB_API_VERSION = "2026-03-10"
_DOWNLOAD_CHUNK_SIZE = 1024 * 1024
_MAX_RELEASE_RESPONSE_BYTES = 2 * 1024 * 1024
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class BinaryAsset:
    """One immutable first-party release asset."""

    filename: str
    sha256: str
    size_bytes: int | None = None
    download_url: str | None = None
    platform: str | None = None
    accelerator: str | None = None

    def __repr__(self) -> str:
        """Return a diagnostic identity without dumping full digests or URLs."""
        return f"BinaryAsset(filename={self.filename!r}, sha256={self.sha256[:12]}..., size_bytes={self.size_bytes!r})"

    @property
    def url(self) -> str:
        """Return the official release URL for this asset."""
        return self.download_url or f"{_RELEASE_BASE_URL}/{self.filename}"


def _normalise_machine(machine: str) -> str:
    value = machine.lower().replace("amd64", "x86_64")
    return "aarch64" if value in {"arm64", "aarch64"} else value


def select_asset(
    manifest: dict[str, object],
    system: str | None = None,
    machine: str | None = None,
    accelerator: str | None = None,
    *,
    release_assets: dict[str, BinaryAsset] | None = None,
) -> BinaryAsset:
    """Select one first-party bundle from a verified release manifest.

    Args:
        manifest: Parsed and verified standalone release manifest.
        system: Platform system name; defaults to the current system.
        machine: Platform machine name; defaults to the current machine.
        accelerator: Requested accelerator; defaults to CPU.
        release_assets: Immutable GitHub release asset records keyed by name.

    Returns:
        The selected manifest-bound release asset.

    Raises:
        EngineBinaryMissingError: If the platform is unsupported.
        EngineBinaryCorruptError: If the manifest and release catalog disagree.
    """
    system_name = (system or platform.system()).lower()
    machine_name = _normalise_machine(machine or platform.machine())
    accelerator_name = (accelerator or "cpu").lower()
    if system_name not in {"windows", "linux"} or machine_name != "x86_64":
        raise EngineBinaryMissingError(
            "no first-party AM Engine bundle for this platform",
            system=system_name,
            machine=machine_name,
            accelerator=accelerator_name,
        )
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list):
        raise EngineBinaryCorruptError("AM Engine release manifest has no artifacts")
    matches = [
        row
        for row in artifacts
        if isinstance(row, dict) and row.get("platform") == system_name and row.get("accel") == accelerator_name
    ]
    if len(matches) != 1:
        raise EngineBinaryMissingError(
            "AM Engine release does not select exactly one platform bundle",
            system=system_name,
            machine=machine_name,
            accelerator=accelerator_name,
            match_count=len(matches),
        )
    row = matches[0]
    filename = str(row["file"])
    manifest_asset = BinaryAsset(
        filename, str(row["sha256"]), int(row["size_bytes"]), platform=system_name, accelerator=accelerator_name
    )
    if release_assets is None:
        return manifest_asset
    catalog_asset = release_assets.get(filename)
    if catalog_asset is None:
        raise EngineBinaryCorruptError("manifest bundle is absent from the immutable GitHub release", file=filename)
    if catalog_asset.sha256 != manifest_asset.sha256 or catalog_asset.size_bytes != manifest_asset.size_bytes:
        raise EngineBinaryCorruptError(
            "manifest bundle disagrees with the immutable GitHub release digest",
            file=filename,
        )
    return replace(catalog_asset, platform=system_name, accelerator=accelerator_name)


def canonical_binary_path(user_dir: Path | None = None) -> Path:
    """Return the canonical executable location without creating it.

    Returns:
        Path beneath the versioned per-user engine install directory.
    """
    executable = "amw-engine-server.exe" if os.name == "nt" else "amw-engine-server"
    return (user_dir or get_user_dir()) / ENGINE_INSTALL_SUBDIR / PINNED_RELEASE / executable


def _require_release_authority() -> tuple[str, str, dict[str, str]]:
    """Return the independently retained release authority or fail closed."""
    commit = PINNED_RELEASE_COMMIT
    manifest_digest = PINNED_RELEASE_MANIFEST_SHA256
    inner_digests = dict(PINNED_INNER_MANIFEST_SHA256_BY_BUNDLE)
    expected_bundles = {"windows-cpu", "windows-cuda", "linux-cpu", "linux-cuda"}
    if (
        not isinstance(commit, str)
        or re.fullmatch(r"[0-9a-f]{40}", commit) is None
        or not isinstance(manifest_digest, str)
        or _SHA256_PATTERN.fullmatch(manifest_digest) is None
        or set(inner_digests) != expected_bundles
        or any(
            not isinstance(value, str) or _SHA256_PATTERN.fullmatch(value) is None for value in inner_digests.values()
        )
    ):
        raise EngineBinaryMissingError(
            "AM Engine release catalog has no complete independently verified release authority",
            release=PINNED_RELEASE,
        )
    return commit, manifest_digest, inner_digests


def release_authority_receipt() -> dict[str, object]:
    """Return the package-owned release authority for trusted receipts.

    Returns:
        Exact immutable release, commit, outer-manifest, and bundle-manifest
        identities retained independently from the installation.
    """
    commit, manifest_digest, inner_digests = _require_release_authority()
    return {
        "release": PINNED_RELEASE,
        "source_commit": commit,
        "release_manifest_sha256": manifest_digest,
        "inner_manifest_sha256_by_bundle": dict(sorted(inner_digests.items())),
    }


def _trusted_release_assets(timeout_seconds: float) -> dict[str, BinaryAsset]:
    """Load the pinned immutable first-party release catalog from GitHub."""
    parsed_api = urlparse(_RELEASE_API_URL)
    if parsed_api.scheme != "https" or parsed_api.hostname != _API_HOST:
        raise EngineBinaryCorruptError("engine release API URL is not trusted", host=parsed_api.hostname)
    try:
        response = httpx.get(
            _RELEASE_API_URL,
            headers={"Accept": "application/vnd.github+json", "User-Agent": "Vetinari-engine-provisioner"},
            timeout=timeout_seconds,
            follow_redirects=False,
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise EngineBinaryMissingError("unable to read the pinned AM Engine release", url=_RELEASE_API_URL) from exc
    try:
        declared_size = response.headers.get("Content-Length")
        if declared_size is not None:
            try:
                declared_size_bytes = int(declared_size)
            except ValueError as exc:
                raise EngineBinaryCorruptError("GitHub release response has an invalid declared size") from exc
            if declared_size_bytes > _MAX_RELEASE_RESPONSE_BYTES:
                raise EngineBinaryCorruptError("GitHub release response exceeds its declared size budget")
        if len(response.content) > _MAX_RELEASE_RESPONSE_BYTES:
            raise EngineBinaryCorruptError("GitHub release response exceeds its size budget")
        if 'rel="next"' in response.headers.get("Link", ""):
            raise EngineBinaryCorruptError("GitHub release response exceeds one bounded page")
        payload = response.json()
    except EngineBinaryCorruptError:
        raise
    except ValueError as exc:
        raise EngineBinaryCorruptError("GitHub release response is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise EngineBinaryCorruptError("GitHub release metadata is not a JSON object")
    if payload.get("tag_name") != PINNED_RELEASE or payload.get("draft") is not False:
        raise EngineBinaryCorruptError("GitHub release metadata does not identify the pinned published release")
    if payload.get("immutable") is not True:
        raise EngineBinaryCorruptError("pinned AM Engine release is not immutable")
    rows = payload.get("assets")
    if not isinstance(rows, list):
        raise EngineBinaryCorruptError("GitHub release metadata has no assets array")
    assets: dict[str, BinaryAsset] = {}
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise EngineBinaryCorruptError("GitHub release metadata contains a non-object asset", index=index)
        filename = row.get("name")
        digest_value = row.get("digest")
        size = row.get("size")
        download_url = row.get("browser_download_url")
        if not isinstance(filename, str) or not filename or PurePosixPath(filename).name != filename:
            raise EngineBinaryCorruptError("GitHub release metadata contains an unsafe asset name", index=index)
        if filename in assets:
            raise EngineBinaryCorruptError("GitHub release metadata contains duplicate asset names", file=filename)
        if row.get("state") != "uploaded":
            raise EngineBinaryCorruptError("GitHub release asset is not fully uploaded", file=filename)
        if not isinstance(digest_value, str) or re.fullmatch(r"sha256:[0-9a-f]{64}", digest_value) is None:
            raise EngineBinaryCorruptError("GitHub release asset has no immutable SHA-256 digest", file=filename)
        if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
            raise EngineBinaryCorruptError("GitHub release asset has an invalid size", file=filename)
        expected_url = f"{_RELEASE_BASE_URL}/{filename}"
        if download_url != expected_url:
            raise EngineBinaryCorruptError("GitHub release asset has an unexpected download URL", file=filename)
        assets[filename] = BinaryAsset(filename, digest_value.removeprefix("sha256:"), size, download_url)
    if "manifest.json" not in assets:
        raise EngineBinaryCorruptError("immutable AM Engine release has no manifest asset")
    return assets


def _download_asset(asset: BinaryAsset, destination: Path, timeout_seconds: float) -> None:
    """Download one catalog asset and verify its immutable GitHub digest."""
    parsed_url = urlparse(asset.url)
    expected_prefix = f"/{ENGINE_RELEASE_REPOSITORY}/releases/download/{PINNED_RELEASE}/"
    if (
        parsed_url.scheme != "https"
        or parsed_url.hostname != _DOWNLOAD_HOST
        or parsed_url.path != expected_prefix + asset.filename
    ):
        raise EngineBinaryCorruptError("engine release URL is not on the trusted first-party release", url=asset.url)
    try:
        with httpx.stream(
            "GET",
            asset.url,
            headers={"User-Agent": "Vetinari-engine-provisioner"},
            timeout=timeout_seconds,
            follow_redirects=True,
        ) as response:
            response.raise_for_status()
            with destination.open("wb") as sink:
                for chunk in response.iter_bytes(chunk_size=_DOWNLOAD_CHUNK_SIZE):
                    sink.write(chunk)
    except (OSError, httpx.HTTPError) as exc:
        raise EngineBinaryMissingError("unable to download the pinned AM Engine release asset", url=asset.url) from exc
    if asset.size_bytes is not None and destination.stat().st_size != asset.size_bytes:
        raise EngineBinaryCorruptError("AM Engine release asset size disagrees with GitHub", file=asset.filename)
    verify_file(destination, asset.sha256)


def resolve_binary(*, expected_sha256: str | None = None, user_dir: Path | None = None) -> Path:
    """Resolve an override or canonical install; never provision implicitly.

    Returns:
        Existing resolved executable path.

    Raises:
        EngineBinaryMissingError: If the selected executable is absent.
        EngineBinaryCorruptError: If requested verification fails.
    """
    override = os.environ.get(ENGINE_BINARY_ENV)
    path = Path(override).expanduser() if override else canonical_binary_path(user_dir)
    if not path.is_absolute():
        path = path.resolve()
    if not path.is_file():
        raise EngineBinaryMissingError("AM Engine binary is not installed", path=str(path))
    if expected_sha256 is not None:
        verify_file(path, expected_sha256)
    if override is None:
        for tool in _release_contract.EXPORT_TOOL_MEMBERS:
            resolve_bundle_tool(tool, user_dir=user_dir)
    return path


def probe_version(
    binary_path: Path,
    *,
    timeout_seconds: float = 10.0,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> str:
    """Read the binary version through a shell-free bounded subprocess.

    Returns:
        The expected pinned version after a successful probe.

    Raises:
        EngineVersionMismatchError: If execution fails or output lacks the pin.
    """
    path = binary_path.resolve(strict=True)
    run_process = runner or subprocess.run
    completed = run_process(
        [str(path), "--version"],
        cwd=str(path.parent),
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
        shell=False,
    )
    output = f"{completed.stdout}\n{completed.stderr}".strip()
    if completed.returncode != 0:
        raise EngineVersionMismatchError(
            "AM Engine version probe failed", path=str(path), returncode=completed.returncode, output=output[:500]
        )
    match = re.search(rf"(?<![\w.]){re.escape(EXPECTED_ENGINE_VERSION)}(?![\w.])", output)
    if match is None:
        raise EngineVersionMismatchError(
            "AM Engine version does not match the pinned release",
            expected=EXPECTED_ENGINE_VERSION,
            observed=output[:500],
        )
    return EXPECTED_ENGINE_VERSION


def _path_exists(path: Path) -> bool:
    """Return whether a path exists, including a broken symbolic link."""
    return os.path.lexists(path)


def _activate_install_tree(staged: Path, install_directory: Path, executable_name: str) -> None:
    """Activate a probed install tree and restore the previous tree on failure."""
    transaction_directory = staged.parent
    transaction_name = transaction_directory.name.removeprefix(".")
    previous = install_directory.with_name(f".{install_directory.name}-{transaction_name}.previous")
    failed = transaction_directory / "failed"
    had_previous = _path_exists(install_directory)
    if had_previous:
        os.replace(install_directory, previous)
    try:
        os.replace(staged, install_directory)
        probe_version(install_directory / executable_name)
    except Exception:
        try:
            if _path_exists(install_directory):
                os.replace(install_directory, failed)
            if had_previous and _path_exists(previous):
                os.replace(previous, install_directory)
        except OSError as rollback_error:
            raise EngineBinaryMissingError(
                "unable to restore the previous AM Engine installation",
                install_directory=str(install_directory),
                recovery_path=str(previous),
            ) from rollback_error
        raise
    if had_previous:
        with suppress(OSError):
            if previous.is_dir() and not previous.is_symlink():
                shutil.rmtree(previous)
            else:
                previous.unlink()


def provision_binary(
    *,
    user_dir: Path | None = None,
    system: str | None = None,
    machine: str | None = None,
    accelerator: str | None = None,
    timeout_seconds: float = 120.0,
) -> Path:
    """Install a manifest-verified first-party engine bundle atomically.

    Provisioning reads the exact repository/tag through GitHub's release API,
    requires the release to be immutable, verifies GitHub's asset digests, then
    verifies both the standalone and in-bundle manifests before probing or
    activating the executable.

    Args:
        user_dir: Optional per-user data root override.
        system: Platform system selector; defaults to the current platform.
        machine: Machine selector; defaults to the current architecture.
        accelerator: Requested CPU or CUDA bundle; defaults to CPU.
        timeout_seconds: Per-request network timeout.

    Returns:
        Atomically installed executable path.

    Raises:
        EngineBinaryMissingError: If the official release cannot be downloaded.
        EngineBinaryCorruptError: If the archive or its contents are untrusted.
        EngineVersionMismatchError: If the installed executable fails its probe.
    """
    release_commit, release_manifest_sha256, inner_manifest_digests = _require_release_authority()
    install_path = canonical_binary_path(user_dir)
    install_directory = install_path.parent
    install_root = install_directory.parent
    install_root.mkdir(parents=True, exist_ok=True)
    release_assets = _trusted_release_assets(timeout_seconds)
    with tempfile.TemporaryDirectory(prefix=f".{PINNED_RELEASE}-", dir=install_root) as temporary:
        temporary_path = Path(temporary)
        manifest_path = temporary_path / "manifest.json"
        _download_asset(release_assets["manifest.json"], manifest_path, timeout_seconds)
        verify_file(manifest_path, release_manifest_sha256)
        release_manifest = verify_release_manifest(
            manifest_path,
            expected_source_commit=release_commit,
        )
        _verify_github_attestation(
            manifest_path,
            source_commit=release_commit,
            timeout_seconds=timeout_seconds,
        )
        selected = select_asset(
            release_manifest,
            system,
            machine,
            accelerator,
            release_assets=release_assets,
        )
        archive = temporary_path / selected.filename
        _download_asset(selected, archive, timeout_seconds)
        verify_release_manifest(manifest_path, artifact_path=archive)
        _verify_github_attestation(
            archive,
            source_commit=release_commit,
            timeout_seconds=timeout_seconds,
        )
        extracted = temporary_path / "extracted"
        _extract_archive(archive, extracted)
        bundle_key = f"{selected.platform}-{selected.accelerator}"
        source_binary = _verify_extracted_bundle(extracted, selected, release_manifest)
        verify_file(extracted / "manifest.json", inner_manifest_digests[bundle_key])
        staged = temporary_path / "install"
        os.replace(extracted, staged)
        staged_binary = staged / source_binary.name
        if os.name != "nt":
            staged_binary.chmod(staged_binary.stat().st_mode | 0o111)
            for tool in _release_contract.EXPORT_NATIVE_TOOLS:
                native_tool = staged / _release_contract.export_tool_member(tool, platform="linux")
                native_tool.chmod(native_tool.stat().st_mode | 0o111)
        for tool in _release_contract.EXPORT_TOOL_MEMBERS:
            resolve_bootstrap_bundle_tool(
                tool,
                bundle_root=staged,
                platform=selected.platform,
                accelerator=selected.accelerator,
                expected_inner_manifest_sha256=inner_manifest_digests[bundle_key],
            )
        probe_version(staged_binary)
        _activate_install_tree(staged, install_directory, install_path.name)
    return install_path


__all__ = [
    "ENGINE_BINARY_ENV",
    "ENGINE_INSTALL_SUBDIR",
    "ENGINE_RELEASE_REPOSITORY",
    "EXPECTED_ENGINE_VERSION",
    "PINNED_COMMIT",
    "PINNED_INNER_MANIFEST_SHA256_BY_BUNDLE",
    "PINNED_RELEASE",
    "PINNED_RELEASE_COMMIT",
    "PINNED_RELEASE_MANIFEST_SHA256",
    "BinaryAsset",
    "canonical_binary_path",
    "probe_version",
    "provision_binary",
    "release_authority_receipt",
    "resolve_binary",
    "select_asset",
    "sha256_file",
    "verify_file",
    "verify_release_manifest",
]
