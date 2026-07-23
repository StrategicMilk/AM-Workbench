#!/usr/bin/env python3
"""Validate the release converter's governed two-platform hash-lock union."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import tomllib
from license_expression import ExpressionError, get_spdx_licensing
from packaging.markers import Marker, default_environment
from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

try:
    from scripts.release_license_policy import classify_release_license
except ModuleNotFoundError as exc:
    if exc.name not in {"scripts", "scripts.release_license_policy"}:
        raise
    from release_license_policy import classify_release_license  # type: ignore[no-redef]

ROOT = Path(__file__).resolve().parents[1]
CONVERTER_DIR = ROOT / "crates" / "amw-engine" / "converter"
DEFAULT_DIRECT = CONVERTER_DIR / "requirements-convert_lora_to_gguf.in"
DEFAULT_LOCK = CONVERTER_DIR / "requirements-convert_lora_to_gguf.txt"
DEFAULT_LICENSES = CONVERTER_DIR / "dependency-licenses.toml"
_ALLOWED_INDEX_DIRECTIVES = {
    "--index-url https://pypi.org/simple",
}
_TRUSTED_TORCH_WHEELS = {
    (
        "https://download-r2.pytorch.org/whl/cpu/torch-2.11.0%2Bcpu-cp312-cp312-manylinux_2_28_x86_64.whl"
        "#sha256=f82e2ae20c1545bb03997d1cc3143d94e14b800038669ee1aca45808a9acc338"
    ): 'sys_platform == "linux"',
    (
        "https://download-r2.pytorch.org/whl/cpu/torch-2.11.0%2Bcpu-cp312-cp312-win_amd64.whl"
        "#sha256=1abeaa46fa7532ed35ed79146f4de5d7a9d4b30462c98052ea4ddfe781ea3eca"
    ): 'sys_platform == "win32"',
}
_HASH_RE = re.compile(r"--hash=sha256:([0-9a-f]{64})(?:\s|$)")
_SPDX_LICENSING = get_spdx_licensing()


class ConverterLockError(ValueError):
    """Raised when converter lock evidence is malformed or incomplete."""


@dataclass(frozen=True)
class LockedConverterPackage:
    """One exact, hash-authorized package row from the converter lock."""

    name: str
    version: str
    marker: str | None
    hashes: tuple[str, ...]
    license_expression: str


def _logical_requirement_rows(path: Path) -> tuple[list[str], set[str]]:
    try:
        raw_lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ConverterLockError(f"converter requirement file is unreadable: {path}") from exc
    rows: list[str] = []
    directives: set[str] = set()
    current: list[str] = []
    for line_number, raw_line in enumerate(raw_lines, start=1):
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("--") and not stripped.startswith("--hash="):
            if current:
                rows.append(" ".join(current))
                current = []
            if stripped not in _ALLOWED_INDEX_DIRECTIVES:
                raise ConverterLockError(f"unsupported converter lock directive at {path}:{line_number}: {stripped}")
            directives.add(stripped)
            continue
        if not raw_line[:1].isspace() and current:
            rows.append(" ".join(current))
            current = []
        current.append(stripped.removesuffix("\\").strip())
    if current:
        rows.append(" ".join(current))
    return rows, directives


def _license_evidence(path: Path) -> dict[str, str]:
    try:
        payload = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConverterLockError(f"converter license evidence is unreadable: {path}") from exc
    packages = payload.get("packages")
    if not isinstance(packages, dict):
        raise ConverterLockError("converter license evidence must contain a [packages] table")
    evidence: dict[str, str] = {}
    for identity, expression in packages.items():
        if not isinstance(identity, str) or not isinstance(expression, str):
            raise ConverterLockError("converter license evidence entries must be string identities and expressions")
        try:
            parsed = _SPDX_LICENSING.parse(expression, validate=True, strict=True)
        except ExpressionError as exc:
            raise ConverterLockError(f"converter package {identity} has invalid SPDX license evidence") from exc
        if parsed is None:
            raise ConverterLockError(f"converter package {identity} has empty SPDX license evidence")
        evidence[identity] = str(parsed)
    return evidence


def _exact_requirement_version(
    requirement: Requirement,
    *,
    resolved_platform_marker: str | None = None,
) -> tuple[str, tuple[str, ...]]:
    """Return an exact version and any hash bound by a trusted direct URL."""
    if requirement.url is None:
        specifiers = list(requirement.specifier)
        if len(specifiers) != 1 or specifiers[0].operator != "==":
            raise ConverterLockError(f"converter lock requirement is not one exact pin: {requirement!s}")
        return str(Version(specifiers[0].version)), ()
    marker = str(requirement.marker) if requirement.marker is not None else resolved_platform_marker
    if requirement.name != "torch" or _TRUSTED_TORCH_WHEELS.get(requirement.url) != marker:
        raise ConverterLockError("converter direct URL is not an approved platform-specific torch wheel")
    parsed = urlparse(requirement.url)
    if parsed.scheme != "https" or parsed.hostname != "download-r2.pytorch.org":
        raise ConverterLockError("converter torch wheel URL is not on the governed HTTPS host")
    match = re.fullmatch(r"torch-(2\.11\.0\+cpu)-cp312-cp312-.+\.whl", unquote(Path(parsed.path).name))
    digest_rows = parse_qs(parsed.fragment, strict_parsing=True).get("sha256", [])
    if match is None or len(digest_rows) != 1 or re.fullmatch(r"[0-9a-f]{64}", digest_rows[0]) is None:
        raise ConverterLockError("converter torch wheel URL lacks its exact CPython 3.12 identity or SHA-256")
    return str(Version(match.group(1))), (digest_rows[0],)


def parse_converter_lock(
    lock_path: Path = DEFAULT_LOCK,
    license_path: Path = DEFAULT_LICENSES,
    *,
    allow_stale_licenses: bool = False,
    resolved_platform_marker: str | None = None,
) -> tuple[LockedConverterPackage, ...]:
    """Parse and validate all exact package rows in the converter hash lock.

    Args:
        lock_path: Governed union of the Windows and Linux uv resolutions.
        license_path: Exact package/version SPDX license evidence.

    Returns:
        Deterministically sorted converter package rows.

    Raises:
        ConverterLockError: If pins, hashes, markers, indexes, or licenses are invalid.
    """
    rows, directives = _logical_requirement_rows(lock_path)
    if directives != _ALLOWED_INDEX_DIRECTIVES:
        raise ConverterLockError("converter lock must use PyPI as its sole general package index")
    licenses = _license_evidence(license_path)
    packages: list[LockedConverterPackage] = []
    identities: set[tuple[str, str, str | None]] = set()
    license_identities: set[str] = set()
    for row in rows:
        requirement_text = row.split("--hash=", maxsplit=1)[0].strip()
        try:
            requirement = Requirement(requirement_text)
        except InvalidRequirement as exc:
            raise ConverterLockError(f"invalid converter lock requirement: {requirement_text!r}") from exc
        try:
            version, url_hashes = _exact_requirement_version(
                requirement,
                resolved_platform_marker=resolved_platform_marker,
            )
        except (InvalidVersion, ValueError) as exc:
            raise ConverterLockError(f"converter lock has invalid version: {requirement_text!r}") from exc
        hashes = tuple(sorted(set(_HASH_RE.findall(row)) | set(url_hashes)))
        if not hashes:
            raise ConverterLockError(f"converter lock package has no SHA-256 hashes: {requirement.name}=={version}")
        name = canonicalize_name(requirement.name)
        marker = (
            str(requirement.marker)
            if requirement.marker is not None
            else resolved_platform_marker
            if requirement.url
            else None
        )
        identity = (name, version, marker)
        if identity in identities:
            raise ConverterLockError(f"duplicate converter lock identity: {identity}")
        identities.add(identity)
        license_identity = f"{name}=={version}"
        license_expression = licenses.get(license_identity)
        if license_expression is None:
            raise ConverterLockError(f"converter license evidence missing for {license_identity}")
        license_identities.add(license_identity)
        packages.append(LockedConverterPackage(name, version, marker, hashes, license_expression))
    stale_licenses = sorted(set(licenses) - license_identities)
    if stale_licenses and not allow_stale_licenses:
        raise ConverterLockError(f"stale converter license evidence: {', '.join(stale_licenses)}")
    return tuple(sorted(packages, key=lambda item: (item.name, item.version, item.marker or "")))


def validate_converter_lock(
    direct_path: Path = DEFAULT_DIRECT,
    lock_path: Path = DEFAULT_LOCK,
    license_path: Path = DEFAULT_LICENSES,
) -> list[str]:
    """Validate direct roots against the complete converter package closure.

    Args:
        direct_path: Human-maintained direct converter requirements.
        lock_path: Generated universal hash lock.
        license_path: Exact license evidence for every locked package.

    Returns:
        Stable validation errors; empty means the closure is release-ready.
    """
    try:
        packages = parse_converter_lock(lock_path, license_path)
        direct_rows, direct_directives = _logical_requirement_rows(direct_path)
    except ConverterLockError as exc:
        return [str(exc)]
    if direct_directives != _ALLOWED_INDEX_DIRECTIVES:
        return ["converter direct requirements must use PyPI as the sole general package index"]
    errors: list[str] = []
    direct_names: set[str] = set()
    for row in direct_rows:
        try:
            requirement = Requirement(row)
        except InvalidRequirement as exc:
            errors.append(f"invalid direct converter requirement {row!r}: {exc}")
            continue
        try:
            exact_version, _url_hashes = _exact_requirement_version(requirement)
        except (ConverterLockError, InvalidVersion, ValueError) as exc:
            errors.append(f"direct converter requirement is not exact: {row!r}: {exc}")
            continue
        name = canonicalize_name(requirement.name)
        direct_names.add(name)
        expected_version = Version(exact_version).base_version
        matching = [package for package in packages if package.name == name]
        if not matching:
            errors.append(f"direct converter dependency missing from lock: {name}")
        elif any(Version(package.version).base_version != expected_version for package in matching):
            errors.append(f"direct converter dependency version drift: {name}=={expected_version}")
    locked_names = {package.name for package in packages}
    for package in packages:
        if classify_release_license(package.license_expression) == "blocked":
            errors.append(
                f"converter package uses a release-blocked license: "
                f"{package.name}=={package.version} ({package.license_expression})"
            )
    if len(locked_names - direct_names) < 10:
        errors.append("converter lock does not contain a substantive transitive closure")
    return errors


def validate_resolved_converter_lock(
    resolved_lock_path: Path,
    canonical_lock_path: Path = DEFAULT_LOCK,
    license_path: Path = DEFAULT_LICENSES,
) -> list[str]:
    """Compare a fresh universal resolution with the committed converter lock.

    Args:
        resolved_lock_path: Fresh lock emitted from the direct requirements.
        canonical_lock_path: Committed universal hash lock.
        license_path: Exact package/version license evidence.

    Returns:
        Stable errors for missing, extra, or drifted transitive package evidence.
    """
    try:
        resolved = parse_converter_lock(resolved_lock_path, license_path)
        canonical = parse_converter_lock(canonical_lock_path, license_path)
    except ConverterLockError as exc:
        return [f"fresh converter resolution is inconsistent with release evidence: {exc}"]
    if resolved != canonical:
        return ["committed converter lock differs from a fresh universal dependency resolution"]
    return []


def verify_converter_resolution(
    direct_path: Path = DEFAULT_DIRECT,
    lock_path: Path = DEFAULT_LOCK,
    license_path: Path = DEFAULT_LICENSES,
    *,
    uv_path: str = "uv",
) -> list[str]:
    """Resolve converter requirements with pinned uv and compare exact closure.

    Args:
        direct_path: Human-maintained exact direct requirements.
        lock_path: Committed universal hash lock.
        license_path: Exact package/version license evidence.
        uv_path: Pinned uv executable used for fresh resolution.

    Returns:
        Stable resolution or exact-comparison errors; empty means complete.
    """
    canonical = parse_converter_lock(lock_path, license_path)
    base_environment = default_environment()
    platforms = {
        "windows": ("windows", {**base_environment, "sys_platform": "win32", "platform_system": "Windows"}),
        "linux": (
            "x86_64-manylinux_2_28",
            {**base_environment, "sys_platform": "linux", "platform_system": "Linux", "platform_machine": "x86_64"},
        ),
    }

    def active_rows(
        packages: tuple[LockedConverterPackage, ...], environment: dict[str, str]
    ) -> set[tuple[str, str, tuple[str, ...], str]]:
        return {
            (package.name, package.version, package.hashes, package.license_expression)
            for package in packages
            if package.marker is None or Marker(package.marker).evaluate(environment)
        }

    with tempfile.TemporaryDirectory(prefix="amw-converter-resolution-") as temporary:
        for label, (python_platform, environment) in platforms.items():
            resolved_path = Path(temporary) / f"requirements-{label}.txt"
            command = ([sys.executable, "-m", "uv"] if uv_path == "uv" else [uv_path]) + [
                "pip",
                "compile",
                str(direct_path.resolve()),
                "--generate-hashes",
                "--python-version",
                "3.12",
                "--python-platform",
                python_platform,
                "--emit-index-url",
                "--quiet",
                "--output-file",
                str(resolved_path),
            ]
            try:
                completed = subprocess.run(
                    command,
                    cwd=ROOT,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=600,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                return [f"fresh {label} converter dependency resolution failed: {exc}"]
            if completed.returncode != 0:
                detail = completed.stderr.strip()[-2_000:]
                return [f"fresh {label} converter dependency resolution failed: {detail or 'uv exited nonzero'}"]
            platform_marker = 'sys_platform == "win32"' if label == "windows" else 'sys_platform == "linux"'
            resolved = parse_converter_lock(
                resolved_path,
                license_path,
                allow_stale_licenses=True,
                resolved_platform_marker=platform_marker,
            )
            if active_rows(resolved, environment) != active_rows(canonical, environment):
                return [f"committed converter lock differs from the fresh governed {label} resolution"]
        return []


def main(argv: list[str] | None = None) -> int:
    """Run the converter lock validator CLI.

    Args:
        argv: Optional command-line arguments.

    Returns:
        Zero when the lock is valid, one otherwise.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--direct", type=Path, default=DEFAULT_DIRECT)
    parser.add_argument("--lock", type=Path, default=DEFAULT_LOCK)
    parser.add_argument("--licenses", type=Path, default=DEFAULT_LICENSES)
    parser.add_argument(
        "--skip-resolution",
        action="store_true",
        help="Skip the fresh pinned-uv platform resolutions (unit-fixture use only).",
    )
    args = parser.parse_args(argv)
    errors = validate_converter_lock(args.direct, args.lock, args.licenses)
    if not errors and not args.skip_resolution:
        errors.extend(verify_converter_resolution(args.direct, args.lock, args.licenses))
    for error in errors:
        print(error, file=sys.stderr)
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())
