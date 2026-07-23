#!/usr/bin/env python3
"""Stage complete dependency license files and package attribution for AM Engine."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from license_expression import get_spdx_licensing
from packaging.markers import Marker, default_environment
from packaging.utils import canonicalize_name

from vetinari.engine.release_contract import (
    CARGO_IDENTITY_COUNT,
    CARGO_IDENTITY_SHA256,
    CONVERTER_IDENTITY_COUNT_BY_PLATFORM,
    CONVERTER_IDENTITY_SHA256_BY_PLATFORM,
    CUDA_DYNAMIC_EXTERNAL_LIBRARIES,
    CUDA_EULA_SHA256,
    CUDA_EULA_URL,
    CUDA_VERSION,
    dependency_identity_digest,
)

try:
    from scripts.check_converter_lock import DEFAULT_LICENSES, DEFAULT_LOCK, parse_converter_lock
    from scripts.generate_spdx_sbom import ROOT, resolve_cargo_dependency_graph
except ModuleNotFoundError as exc:
    if exc.name not in {"scripts", "scripts.check_converter_lock", "scripts.generate_spdx_sbom"}:
        raise
    from check_converter_lock import DEFAULT_LICENSES, DEFAULT_LOCK, parse_converter_lock  # type: ignore[no-redef]
    from generate_spdx_sbom import ROOT, resolve_cargo_dependency_graph  # type: ignore[no-redef]

DEFAULT_OUTPUT = ROOT / "dist" / "engine-license-corpus"
_LICENSE_PREFIXES = ("LICENSE", "LICENCE", "COPYING", "NOTICE", "AUTHORS", "COPYRIGHT", "UNLICENSE")
_MAX_LICENSE_FILE_BYTES = 2 * 1024 * 1024
_LICENSING = get_spdx_licensing()
_STANDARD_LICENSE_DIGESTS = {
    "APACHE": "a60eea817514531668d7e00765731449fe14d059d3249e0bc93b36de45f759f2",
    "APACHE_LLVM": "268872b9816f90fd8e85db5a28d33f8150ebb8dd016653fb39ef1f94f2686bc5",
    "BYTECODE_MIT": "23f18e03dc49df91622fe2a76176497404e46ced8a715d9d2b67a7446571cca3",
    "WINAPI_APACHE": "b40930bbcf80744c86c46a12bc9da056641d722716c378f5659b9e555ef833e1",
    "WINAPI_MIT": "ce7bc3499fee93d5022ef430d5e4201e79a6d9154f3974e42f41349f0569e09b",
}


@dataclass(frozen=True)
class _SupplementalLicense:
    """One package-specific license file omitted from a published crate."""

    filename: str
    sha256: str
    source_url: str
    source_revision: str
    repository_path: str | None = None


def _supplemental_license_map() -> dict[tuple[str, str], tuple[_SupplementalLicense, ...]]:
    objc_revisions = {
        ("block2", "0.6.2"): "b4167b582b2f75f9a1be75495c41b765344fd03c",
        ("dispatch2", "0.3.1"): "8852b424193ca41602281b3d7540d7c8ed51e49a",
        ("objc2", "0.6.4"): "8852b424193ca41602281b3d7540d7c8ed51e49a",
        ("objc2-core-foundation", "0.3.2"): "7b1abfd750a2cacaea71d6a56ecfb83cb7de560b",
        ("objc2-encode", "4.1.0"): "8d214f5477365ffcbcbb7de058c86ed9a518efb7",
        ("objc2-exception-helper", "0.1.1"): "8d214f5477365ffcbcbb7de058c86ed9a518efb7",
        ("objc2-io-kit", "0.3.2"): "7b1abfd750a2cacaea71d6a56ecfb83cb7de560b",
    }
    supplements: dict[tuple[str, str], tuple[_SupplementalLicense, ...]] = {}
    for identity, revision in objc_revisions.items():
        supplements[identity] = (
            _SupplementalLicense(
                filename="objc2-LICENSE.md",
                sha256="7f976f7e9cb2d87df7230606feb932c3f21ac0e664045a775b600046ff850c54",
                source_url=f"https://raw.githubusercontent.com/madsmtm/objc2/{revision}/LICENSE.md",
                source_revision=revision,
                repository_path="crates/amw-engine/legal/cargo-supplemental/objc2-LICENSE.md",
            ),
            _SupplementalLicense(
                filename="LICENSE-MIT",
                sha256=_STANDARD_LICENSE_DIGESTS["BYTECODE_MIT"],
                source_url=(
                    "https://raw.githubusercontent.com/bytecodealliance/wasm-tools/"
                    "d4e317f22c3bace76cb3205003bcc34b4929037d/LICENSE-MIT"
                ),
                source_revision="d4e317f22c3bace76cb3205003bcc34b4929037d",
            ),
        )
    supplements["valuable", "0.1.1"] = (
        _SupplementalLicense(
            filename="valuable-LICENSE",
            sha256="ed60d479b8fd1f64e9cbc3de449a16a53ac1b3d1b6aeb9bf9d190a8e93061b44",
            source_url=(
                "https://raw.githubusercontent.com/tokio-rs/valuable/9efc29b6e58cef28f6566a47aa7e142a55fead77/LICENSE"
            ),
            source_revision="9efc29b6e58cef28f6566a47aa7e142a55fead77",
            repository_path="crates/amw-engine/legal/cargo-supplemental/valuable-LICENSE",
        ),
    )
    bytecode_specs = (
        _SupplementalLicense(
            filename="LICENSE-APACHE",
            sha256=_STANDARD_LICENSE_DIGESTS["APACHE"],
            source_url=(
                "https://raw.githubusercontent.com/bytecodealliance/wasm-tools/"
                "d4e317f22c3bace76cb3205003bcc34b4929037d/LICENSE-APACHE"
            ),
            source_revision="d4e317f22c3bace76cb3205003bcc34b4929037d",
        ),
        _SupplementalLicense(
            filename="LICENSE-Apache-2.0_WITH_LLVM-exception",
            sha256=_STANDARD_LICENSE_DIGESTS["APACHE_LLVM"],
            source_url=(
                "https://raw.githubusercontent.com/bytecodealliance/wasm-tools/"
                "d4e317f22c3bace76cb3205003bcc34b4929037d/LICENSE-Apache-2.0_WITH_LLVM-exception"
            ),
            source_revision="d4e317f22c3bace76cb3205003bcc34b4929037d",
        ),
        _SupplementalLicense(
            filename="LICENSE-MIT",
            sha256=_STANDARD_LICENSE_DIGESTS["BYTECODE_MIT"],
            source_url=(
                "https://raw.githubusercontent.com/bytecodealliance/wasm-tools/"
                "d4e317f22c3bace76cb3205003bcc34b4929037d/LICENSE-MIT"
            ),
            source_revision="d4e317f22c3bace76cb3205003bcc34b4929037d",
        ),
    )
    for name in ("wasm-encoder", "wasm-metadata", "wasmparser", "wit-component", "wit-parser"):
        supplements[name, "0.244.0"] = bytecode_specs
    supplements["wasip3", "0.4.0+wasi-0.3.0-rc-2026-01-06"] = tuple(
        _SupplementalLicense(
            filename=spec.filename,
            sha256=spec.sha256,
            source_url=spec.source_url.replace(
                "bytecodealliance/wasm-tools/d4e317f22c3bace76cb3205003bcc34b4929037d",
                "bytecodealliance/wasi-rs/06ce201370fcde0d1b0d47cac8ecb1b0b312c9f9",
            ),
            source_revision="06ce201370fcde0d1b0d47cac8ecb1b0b312c9f9",
        )
        for spec in bytecode_specs
    )
    winapi_specs = (
        _SupplementalLicense(
            filename="LICENSE-APACHE",
            sha256=_STANDARD_LICENSE_DIGESTS["WINAPI_APACHE"],
            source_url=(
                "https://raw.githubusercontent.com/retep998/winapi-rs/"
                "5b1829956ef645f3c2f8236ba18bb198ca4c2468/LICENSE-APACHE"
            ),
            source_revision="5b1829956ef645f3c2f8236ba18bb198ca4c2468",
        ),
        _SupplementalLicense(
            filename="LICENSE-MIT",
            sha256=_STANDARD_LICENSE_DIGESTS["WINAPI_MIT"],
            source_url=(
                "https://raw.githubusercontent.com/retep998/winapi-rs/"
                "5b1829956ef645f3c2f8236ba18bb198ca4c2468/LICENSE-MIT"
            ),
            source_revision="5b1829956ef645f3c2f8236ba18bb198ca4c2468",
        ),
    )
    for name in ("winapi-i686-pc-windows-gnu", "winapi-x86_64-pc-windows-gnu"):
        supplements[name, "0.4.0"] = winapi_specs
    return supplements


_SUPPLEMENTAL_LICENSES = _supplemental_license_map()
_CONVERTER_SUPPLEMENTAL_LICENSES = {
    ("sentencepiece", "0.2.1"): (
        _SupplementalLicense(
            filename="sentencepiece-0.2.1-LICENSE",
            sha256="cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30",
            source_url=(
                "https://raw.githubusercontent.com/google/sentencepiece/"
                "31646a467d2051eb904e0b45de3a73e91fe1c1e3/LICENSE"
            ),
            source_revision="31646a467d2051eb904e0b45de3a73e91fe1c1e3",
            repository_path=("crates/amw-engine/legal/converter-supplemental/sentencepiece-0.2.1-LICENSE"),
        ),
    ),
    ("tokenizers", "0.22.2"): (
        _SupplementalLicense(
            filename="tokenizers-0.22.2-LICENSE",
            sha256="c71d239df91726fc519c6eb72d318ec65820627232b2f796219e87dcf35d0ab4",
            source_url=(
                "https://raw.githubusercontent.com/huggingface/tokenizers/"
                "f383101a26663708484cac0727792aad74f78234/LICENSE"
            ),
            source_revision="f383101a26663708484cac0727792aad74f78234",
            repository_path=("crates/amw-engine/legal/converter-supplemental/tokenizers-0.22.2-LICENSE"),
        ),
    ),
}


class LicenseCorpusError(ValueError):
    """Raised when exact package license files cannot be staged safely."""


def _reject_link_or_reparse(path: Path, metadata: os.stat_result) -> None:
    """Reject a symbolic link or Windows reparse point without following it."""
    file_attributes = getattr(metadata, "st_file_attributes", 0)
    reparse_attribute = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if stat.S_ISLNK(metadata.st_mode) or file_attributes & reparse_attribute:
        raise LicenseCorpusError(f"license corpus path component must not be a link or reparse point: {path}")


def _absolute_without_resolving(path: Path) -> Path:
    """Return a normalized absolute path without dereferencing path components."""
    return Path(os.path.abspath(os.fspath(path)))


def _existing_safe_directory_ancestor(path: Path) -> Path:
    """Return the deepest existing directory in ``path`` after no-follow checks."""
    current = Path(path.anchor)
    try:
        root_metadata = os.lstat(current)
    except OSError as exc:
        raise LicenseCorpusError(f"license corpus filesystem root is unreadable: {current}") from exc
    _reject_link_or_reparse(current, root_metadata)
    if not stat.S_ISDIR(root_metadata.st_mode):
        raise LicenseCorpusError(f"license corpus filesystem root must be a directory: {current}")
    deepest = current
    components = path.parts[1:] if path.anchor else path.parts
    for component in components:
        current /= component
        try:
            metadata = os.lstat(current)
        except FileNotFoundError:
            break
        except OSError as exc:
            raise LicenseCorpusError(f"license corpus path component is unreadable: {current}") from exc
        _reject_link_or_reparse(current, metadata)
        if not stat.S_ISDIR(metadata.st_mode):
            raise LicenseCorpusError(f"license corpus parent component must be a directory: {current}")
        deepest = current
    return deepest


def _prepare_license_corpus_output(output: Path) -> Path:
    """Validate and create a corpus output without following planted links.

    The requested output's immediate parent is the authorized staging root.
    Every existing component is inspected with ``lstat`` before any resolution,
    and the resolved target must remain at the exact lexical location beneath
    that root both before and after creation.
    """
    lexical_output = _absolute_without_resolving(output)
    if lexical_output == Path(lexical_output.anchor):
        raise LicenseCorpusError("license corpus output must not be a filesystem root")

    authorized_lexical_root = lexical_output.parent
    _existing_safe_directory_ancestor(authorized_lexical_root)
    try:
        authorized_lexical_root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise LicenseCorpusError(
            f"license corpus staging root could not be created: {authorized_lexical_root}"
        ) from exc
    final_safe_parent = _existing_safe_directory_ancestor(authorized_lexical_root)
    if final_safe_parent != authorized_lexical_root:
        raise LicenseCorpusError(f"license corpus staging root was not created safely: {authorized_lexical_root}")
    try:
        output_metadata = os.lstat(lexical_output)
    except FileNotFoundError:
        output_metadata = None
    except OSError as exc:
        raise LicenseCorpusError(f"license corpus output is unreadable: {lexical_output}") from exc
    if output_metadata is not None:
        _reject_link_or_reparse(lexical_output, output_metadata)

    try:
        authorized_root = authorized_lexical_root.resolve(strict=True)
        expected_output = authorized_root / lexical_output.name
        resolved_output = lexical_output.resolve(strict=False)
    except (OSError, RuntimeError, ValueError) as exc:
        raise LicenseCorpusError(f"license corpus output cannot be confined safely: {lexical_output}") from exc
    if not resolved_output.is_relative_to(authorized_root) or resolved_output != expected_output:
        raise LicenseCorpusError(f"license corpus output escapes its authorized staging root: {lexical_output}")

    if output_metadata is not None:
        if not stat.S_ISDIR(output_metadata.st_mode) or any(lexical_output.iterdir()):
            raise LicenseCorpusError(f"license corpus output must be an empty directory: {lexical_output}")
    else:
        try:
            lexical_output.mkdir()
        except OSError as exc:
            raise LicenseCorpusError(f"license corpus output could not be created: {lexical_output}") from exc

    final_safe_root = _existing_safe_directory_ancestor(lexical_output)
    if final_safe_root != lexical_output:
        raise LicenseCorpusError(f"license corpus output was not created safely: {lexical_output}")
    try:
        final_output = lexical_output.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise LicenseCorpusError(f"license corpus output cannot be resolved safely: {lexical_output}") from exc
    if not final_output.is_relative_to(authorized_root) or final_output != expected_output:
        raise LicenseCorpusError(
            f"license corpus output escaped its authorized staging root during creation: {lexical_output}"
        )
    return final_output


def _safe_component(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._+-]+", "-", value).strip("-") or "package"


def _cargo_metadata(root: Path) -> dict[str, Any]:
    cargo_path = shutil.which("cargo")
    if cargo_path is None:
        raise LicenseCorpusError("Cargo metadata failed: cargo executable is unavailable")
    try:
        completed = subprocess.run(
            [
                cargo_path,
                "metadata",
                "--format-version",
                "1",
                "--locked",
                "--all-features",
                "--manifest-path",
                str(root / "crates/amw-engine/Cargo.toml"),
            ],
            cwd=root,
            capture_output=True,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise LicenseCorpusError(f"Cargo metadata failed: {exc}") from exc
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace")[-2_000:]
        raise LicenseCorpusError(f"Cargo metadata failed: {detail}")
    try:
        payload = json.loads(completed.stdout.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise LicenseCorpusError("Cargo metadata returned malformed JSON") from exc
    if not isinstance(payload, dict):
        raise LicenseCorpusError("Cargo metadata root must be an object")
    return payload


def _cargo_license_files(package_root: Path) -> list[Path]:
    files: list[Path] = []
    for candidate in package_root.iterdir():
        if candidate.is_file() and candidate.name.upper().startswith(_LICENSE_PREFIXES):
            files.append(candidate)
        elif candidate.is_dir() and candidate.name.upper() in {"LICENSES", "LICENSE"}:
            files.extend(path for path in candidate.rglob("*") if path.is_file())
    return sorted(files, key=lambda path: path.relative_to(package_root).as_posix())


def _copy_license_file(source: Path, destination: Path, corpus_root: Path) -> dict[str, object]:
    if source.is_symlink():
        raise LicenseCorpusError(f"license file must not be a symlink: {source}")
    try:
        content = source.read_bytes()
    except OSError as exc:
        raise LicenseCorpusError(f"license file is unreadable: {source}") from exc
    if not content or len(content) > _MAX_LICENSE_FILE_BYTES:
        raise LicenseCorpusError(f"license file has an invalid size: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(content)
    return {
        "file": destination.relative_to(corpus_root).as_posix(),
        "sha256": hashlib.sha256(content).hexdigest(),
        "size_bytes": len(content),
    }


def _copy_supplemental_license(
    spec: _SupplementalLicense,
    *,
    root: Path,
    known_sources: dict[str, Path],
    destination: Path,
    corpus_root: Path,
) -> dict[str, object]:
    source: Path | None
    if spec.repository_path is not None:
        source = root / spec.repository_path
    else:
        source = known_sources.get(spec.sha256)
        if source is None:
            raise LicenseCorpusError(
                f"pinned supplemental license text is unavailable: {spec.source_url} ({spec.sha256})"
            )
    if source is None:
        raise LicenseCorpusError(f"pinned supplemental license text is unavailable: {spec.source_url}")
    record = _copy_license_file(source, destination, corpus_root)
    if record["sha256"] != spec.sha256:
        raise LicenseCorpusError(f"pinned supplemental license digest drifted: {spec.source_url}")
    return {
        **record,
        "source_url": spec.source_url,
        "source_revision": spec.source_revision,
    }


def _stage_native_licenses(
    *,
    output: Path,
    accelerator: str,
    cuda_eula: Path | None,
) -> list[dict[str, object]]:
    if accelerator == "cpu":
        if cuda_eula is not None:
            raise LicenseCorpusError("CPU corpus must not receive CUDA license evidence")
        return []
    if accelerator != "cuda":
        raise LicenseCorpusError(f"unsupported engine accelerator: {accelerator}")
    if cuda_eula is None:
        raise LicenseCorpusError("CUDA corpus requires the official CUDA 12.4 EULA and third-party notices PDF")
    record = _copy_license_file(
        cuda_eula,
        output / "native" / f"cuda-{CUDA_VERSION}" / "NVIDIA-CUDA-EULA-AND-THIRD-PARTY-NOTICES.pdf",
        output,
    )
    if record["sha256"] != CUDA_EULA_SHA256:
        raise LicenseCorpusError("CUDA EULA digest does not match the official pinned CUDA 12.4.1 document")
    evidence = {
        **record,
        "source_url": CUDA_EULA_URL,
        "source_revision": CUDA_VERSION,
    }
    return [
        {
            "name": name,
            "version": CUDA_VERSION,
            "license": "LicenseRef-NVIDIA-CUDA-Toolkit-EULA-12.4",
            "linkage": "dynamic_external",
            "bundled": False,
            "evidence_purpose": "external_runtime_prerequisite_terms_and_third_party_notices",
            "license_files": [evidence],
        }
        for name in CUDA_DYNAMIC_EXTERNAL_LIBRARIES
    ]


def _query_converter_metadata(converter_python: Path) -> list[dict[str, object]]:
    script = r"""
import json
from importlib import metadata

rows = []
prefixes = ("LICENSE", "LICENCE", "COPYING", "NOTICE", "AUTHORS", "COPYRIGHT", "UNLICENSE")
for distribution in metadata.distributions():
    files = []
    for relative in distribution.files or ():
        parts = [part.upper() for part in relative.parts]
        if not (relative.name.upper().startswith(prefixes) or "LICENSES" in parts):
            continue
        located = distribution.locate_file(relative)
        if located.is_file():
            files.append(str(located.resolve()))
    rows.append({
        "name": distribution.metadata.get("Name", ""),
        "version": distribution.version,
        "license_files": sorted(set(files)),
    })
print(json.dumps(rows, sort_keys=True))
"""
    try:
        completed = subprocess.run(
            [str(converter_python), "-c", script],
            capture_output=True,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise LicenseCorpusError(f"converter metadata query failed: {exc}") from exc
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace")[-2_000:]
        raise LicenseCorpusError(f"converter metadata query failed: {detail}")
    try:
        rows = json.loads(completed.stdout.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise LicenseCorpusError("converter metadata query returned malformed JSON") from exc
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise LicenseCorpusError("converter metadata query returned an invalid package list")
    return rows


def build_engine_spdx_document(index: dict[str, object]) -> dict[str, object]:
    """Build an artifact-specific SPDX 2.3 document from a staged corpus index.

    Args:
        index: Validated license-corpus index for one platform and accelerator.

    Returns:
        Deterministic SPDX 2.3 JSON document describing the exact artifact leg.

    Raises:
        LicenseCorpusError: If index selectors or dependency rows are malformed.
    """
    platform = index.get("platform")
    accelerator = index.get("accelerator")
    cargo_digest = index.get("cargo_identity_sha256")
    converter_digest = index.get("converter_identity_sha256")
    if (
        platform not in {"windows", "linux"}
        or accelerator not in {"cpu", "cuda"}
        or not isinstance(cargo_digest, str)
        or not isinstance(converter_digest, str)
    ):
        raise LicenseCorpusError("SPDX generation requires exact platform, accelerator, and dependency identities")
    root_id = f"SPDXRef-AMWEngine-{platform}-{accelerator}"
    packages: list[dict[str, object]] = [
        {
            "name": f"amw-engine-{platform}-{accelerator}",
            "SPDXID": root_id,
            "downloadLocation": "NOASSERTION",
            "filesAnalyzed": False,
            "licenseConcluded": "NOASSERTION",
            "licenseDeclared": "NOASSERTION",
            "copyrightText": "NOASSERTION",
            "primaryPackagePurpose": "APPLICATION",
            "comment": f"AM Engine release artifact for platform={platform}, accelerator={accelerator}",
        }
    ]
    relationships: list[dict[str, str]] = []

    def append_package(row: object, *, ecosystem: str, ordinal: int) -> None:
        if not isinstance(row, dict):
            raise LicenseCorpusError(f"SPDX {ecosystem} dependency row is invalid")
        name = row.get("name")
        version = row.get("version")
        license_expression = row.get("license")
        if not all(isinstance(value, str) and value for value in (name, version, license_expression)):
            raise LicenseCorpusError(f"SPDX {ecosystem} dependency identity is incomplete")
        if not isinstance(name, str) or not isinstance(version, str) or not isinstance(license_expression, str):
            raise LicenseCorpusError(f"SPDX {ecosystem} dependency identity is incomplete")
        spdx_id = f"SPDXRef-{ecosystem}-{ordinal}-{_safe_component(name)}-{_safe_component(version)}"
        package: dict[str, object] = {
            "name": name,
            "versionInfo": version,
            "SPDXID": spdx_id,
            "downloadLocation": "NOASSERTION",
            "filesAnalyzed": False,
            "licenseConcluded": license_expression,
            "licenseDeclared": license_expression,
            "copyrightText": "NOASSERTION",
            "primaryPackagePurpose": "LIBRARY",
        }
        if ecosystem == "Cargo":
            package["externalRefs"] = [
                {
                    "referenceCategory": "PACKAGE-MANAGER",
                    "referenceType": "purl",
                    "referenceLocator": f"pkg:cargo/{name}@{version}",
                }
            ]
        elif ecosystem == "PyPI":
            package["externalRefs"] = [
                {
                    "referenceCategory": "PACKAGE-MANAGER",
                    "referenceType": "purl",
                    "referenceLocator": f"pkg:pypi/{canonicalize_name(name)}@{version}",
                }
            ]
        else:
            package["comment"] = f"linkage={row.get('linkage')}; bundled={row.get('bundled')}"
        packages.append(package)
        relationships.append({
            "spdxElementId": root_id,
            "relationshipType": "DEPENDS_ON",
            "relatedSpdxElement": spdx_id,
        })

    groups = (
        ("cargo_packages", "Cargo"),
        ("converter_packages", "PyPI"),
        ("native_components", "Native"),
    )
    for key, ecosystem in groups:
        rows = index.get(key)
        if not isinstance(rows, list):
            raise LicenseCorpusError(f"SPDX generation requires a {key} list")
        for ordinal, row in enumerate(rows):
            append_package(row, ecosystem=ecosystem, ordinal=ordinal)
    document: dict[str, object] = {
        "spdxVersion": "SPDX-2.3",
        "dataLicense": "CC0-1.0",
        "SPDXID": "SPDXRef-DOCUMENT",
        "name": f"amw-engine-{platform}-{accelerator}-sbom",
        "documentNamespace": (
            "https://github.com/StrategicMilk/AM-Workbench/spdx/amw-engine/"
            f"{platform}/{accelerator}/{cargo_digest}/{converter_digest}"
        ),
        "creationInfo": {
            "created": "2024-08-01T00:00:00Z",
            "creators": ["Tool: scripts/stage_engine_license_corpus.py"],
        },
        "documentDescribes": [root_id],
        "packages": packages,
        "relationships": relationships,
    }
    has_cuda_eula = any(
        package.get("licenseDeclared") == "LicenseRef-NVIDIA-CUDA-Toolkit-EULA-12.4" for package in packages
    )
    if has_cuda_eula:
        document["hasExtractedLicensingInfos"] = [
            {
                "licenseId": "LicenseRef-NVIDIA-CUDA-Toolkit-EULA-12.4",
                "name": "NVIDIA CUDA Toolkit EULA 12.4",
                "extractedText": (
                    "The exact NVIDIA CUDA Toolkit 12.4.1 EULA and third-party notices are retained "
                    "inside this artifact's ENGINE_LICENSES native evidence directory."
                ),
                "seeAlsos": [CUDA_EULA_URL],
            }
        ]
    return document


def stage_engine_license_corpus(
    root: Path,
    output: Path,
    converter_metadata: list[dict[str, object]],
    *,
    accelerator: str = "cpu",
    cuda_eula: Path | None = None,
) -> dict[str, object]:
    """Stage exact Cargo, converter, and native license files into one corpus.

    Args:
        root: Repository root containing canonical dependency evidence.
        output: Empty destination directory to create.
        converter_metadata: Installed converter distributions and license paths.
        accelerator: Release accelerator selector, either ``cpu`` or ``cuda``.
        cuda_eula: Official pinned CUDA 12.4.1 EULA PDF for CUDA bundles.

    Returns:
        Deterministic corpus index written as ``INDEX.json``.

    Raises:
        LicenseCorpusError: If dependency identity or license files are incomplete.
    """
    output = _prepare_license_corpus_output(output)
    root = root.resolve()
    graph = resolve_cargo_dependency_graph(root)
    metadata_payload = _cargo_metadata(root)
    metadata_packages = {
        package.get("id"): package
        for package in metadata_payload.get("packages", [])
        if isinstance(package, dict) and isinstance(package.get("id"), str)
    }
    engine_roots = [package_id for package_id in graph.roots if graph.packages[package_id].name == "amw-engine"]
    if len(engine_roots) != 1:
        raise LicenseCorpusError("Cargo graph must contain exactly one amw-engine root")
    children: dict[str, set[str]] = {}
    for parent, dependency in graph.relationships:
        children.setdefault(parent, set()).add(dependency)
    reachable: set[str] = set()
    pending = deque(engine_roots)
    while pending:
        parent = pending.popleft()
        for dependency in children.get(parent, set()):
            if dependency not in reachable:
                reachable.add(dependency)
                pending.append(dependency)
    cargo_rows: list[dict[str, object]] = []
    cargo_destinations: dict[tuple[str, str], Path] = {}
    known_license_sources: dict[str, Path] = {}
    for package_id in sorted(reachable):
        package = graph.packages[package_id]
        metadata_package = metadata_packages.get(package_id)
        if not isinstance(metadata_package, dict):
            raise LicenseCorpusError(f"Cargo package metadata is missing: {package_id}")
        manifest_path = metadata_package.get("manifest_path")
        if not isinstance(manifest_path, str):
            raise LicenseCorpusError(f"Cargo package manifest path is missing: {package_id}")
        package_root = Path(manifest_path).resolve().parent
        identity_hash = hashlib.sha256(package.source_identity.encode("utf-8")).hexdigest()[:12]
        directory = output / "cargo" / _safe_component(f"{package.name}-{package.version}-{identity_hash}")
        cargo_destinations[package.name, package.version] = directory
        copied: list[dict[str, object]] = []
        _LICENSING.parse(package.license_expression, validate=True, strict=True)
        for source in _cargo_license_files(package_root):
            relative = source.relative_to(package_root)
            destination = directory / "texts" / relative
            record = _copy_license_file(source, destination, output)
            copied.append(record)
            known_license_sources.setdefault(str(record["sha256"]), source)
        metadata_record = {
            "name": package.name,
            "version": package.version,
            "license": package.license_expression,
            "authors": metadata_package.get("authors", []),
            "repository": metadata_package.get("repository"),
            "source": package.source_identity,
        }
        metadata_path = directory / "PACKAGE-METADATA.json"
        metadata_path.parent.mkdir(parents=True, exist_ok=True)
        metadata_path.write_text(json.dumps(metadata_record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        metadata_bytes = metadata_path.read_bytes()
        cargo_rows.append({
            **metadata_record,
            "license_files": copied,
            "supplemental_license_files": [],
            "metadata_file": {
                "file": metadata_path.relative_to(output).as_posix(),
                "sha256": hashlib.sha256(metadata_bytes).hexdigest(),
                "size_bytes": len(metadata_bytes),
            },
        })
    for row in cargo_rows:
        if row["license_files"]:
            continue
        identity = str(row["name"]), str(row["version"])
        specs = _SUPPLEMENTAL_LICENSES.get(identity)
        if not specs:
            raise LicenseCorpusError(f"Cargo package has no authoritative license text: {identity[0]}=={identity[1]}")
        directory = cargo_destinations[identity]
        row["supplemental_license_files"] = [
            _copy_supplemental_license(
                spec,
                root=root,
                known_sources=known_license_sources,
                destination=directory / "texts" / "supplemental" / spec.filename,
                corpus_root=output,
            )
            for spec in specs
        ]

    cargo_identities = {(str(row["name"]), str(row["version"])) for row in cargo_rows}
    cargo_identity_sha256 = dependency_identity_digest(cargo_identities)
    if len(cargo_identities) != CARGO_IDENTITY_COUNT or cargo_identity_sha256 != CARGO_IDENTITY_SHA256:
        raise LicenseCorpusError("Cargo dependency identity closure drifted from the canonical release contract")

    lock_packages = parse_converter_lock(
        root / DEFAULT_LOCK.relative_to(ROOT),
        root / DEFAULT_LICENSES.relative_to(ROOT),
    )
    marker_environment = default_environment()
    sys_platform = marker_environment["sys_platform"]
    if sys_platform == "win32":
        platform_name = "windows"
    elif sys_platform == "linux":
        platform_name = "linux"
    else:
        raise LicenseCorpusError(f"unsupported engine release platform: {sys_platform}")
    active_packages = [
        package
        for package in lock_packages
        if package.marker is None or Marker(package.marker).evaluate(marker_environment)
    ]
    observed_converter: dict[tuple[str, str], dict[str, object]] = {}
    for row in converter_metadata:
        name = row.get("name")
        version = row.get("version")
        if isinstance(name, str) and isinstance(version, str):
            observed_converter[canonicalize_name(name), version] = row
    converter_rows: list[dict[str, object]] = []
    for package in active_packages:
        identity = (package.name, package.version)
        metadata_row = observed_converter.get(identity)
        if metadata_row is None:
            raise LicenseCorpusError(
                f"installed converter package is missing or drifted: {package.name}=={package.version}"
            )
        directory = output / "python" / _safe_component(f"{package.name}-{package.version}")
        sources = metadata_row.get("license_files")
        copied: list[dict[str, object]] = []
        supplemental: list[dict[str, object]] = []
        if not isinstance(sources, list) or not all(isinstance(item, str) for item in sources):
            raise LicenseCorpusError(
                f"converter package license metadata is invalid: {package.name}=={package.version}"
            )
        used_names: set[str] = set()
        for source_text in sources:
            source = Path(source_text)
            name = _safe_component(source.name)
            if name in used_names:
                name = f"{hashlib.sha256(source_text.encode('utf-8')).hexdigest()[:8]}-{name}"
            used_names.add(name)
            copied.append(_copy_license_file(source, directory / name, output))
        if not copied:
            specs = _CONVERTER_SUPPLEMENTAL_LICENSES.get(identity)
            if not specs:
                raise LicenseCorpusError(
                    f"converter package has no distributable license files: {package.name}=={package.version}"
                )
            supplemental = [
                _copy_supplemental_license(
                    spec,
                    root=root,
                    known_sources=known_license_sources,
                    destination=directory / "supplemental" / spec.filename,
                    corpus_root=output,
                )
                for spec in specs
            ]
        converter_rows.append({
            "name": package.name,
            "version": package.version,
            "license": package.license_expression,
            "marker": package.marker,
            "license_files": copied,
            "supplemental_license_files": supplemental,
        })
    converter_identities = {(str(row["name"]), str(row["version"])) for row in converter_rows}
    converter_identity_sha256 = dependency_identity_digest(converter_identities)
    if (
        len(converter_identities) != CONVERTER_IDENTITY_COUNT_BY_PLATFORM[platform_name]
        or converter_identity_sha256 != CONVERTER_IDENTITY_SHA256_BY_PLATFORM[platform_name]
    ):
        raise LicenseCorpusError("converter dependency identity closure drifted from the canonical release contract")
    native_rows = _stage_native_licenses(output=output, accelerator=accelerator, cuda_eula=cuda_eula)
    index = {
        "schema_version": 1,
        "platform": platform_name,
        "accelerator": accelerator,
        "cargo_identity_sha256": cargo_identity_sha256,
        "converter_identity_sha256": converter_identity_sha256,
        "cargo_packages": cargo_rows,
        "converter_packages": converter_rows,
        "native_components": native_rows,
    }
    (output / "INDEX.json").write_text(json.dumps(index, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    spdx = build_engine_spdx_document(index)
    (output / "SPDX.spdx.json").write_text(json.dumps(spdx, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return index


def main(argv: list[str] | None = None) -> int:
    """Stage the engine license corpus from a hash-locked converter environment.

    Args:
        argv: Optional command-line arguments.

    Returns:
        Zero after complete corpus staging; incomplete evidence raises an error.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--converter-python", type=Path, required=True)
    parser.add_argument("--accelerator", choices=("cpu", "cuda"), required=True)
    parser.add_argument("--cuda-eula", type=Path)
    args = parser.parse_args(argv)
    index = stage_engine_license_corpus(
        args.root,
        args.output,
        _query_converter_metadata(args.converter_python),
        accelerator=args.accelerator,
        cuda_eula=args.cuda_eula,
    )
    cargo_packages = index.get("cargo_packages")
    converter_packages = index.get("converter_packages")
    native_components = index.get("native_components")
    if (
        not isinstance(cargo_packages, list)
        or not isinstance(converter_packages, list)
        or not isinstance(native_components, list)
    ):
        raise LicenseCorpusError("staged license corpus index has invalid package lists")
    print(
        f"staged license corpus: cargo={len(cargo_packages)} "
        f"converter={len(converter_packages)} native={len(native_components)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
