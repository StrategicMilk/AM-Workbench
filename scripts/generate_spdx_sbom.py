"""Generate a minimal SPDX 2.3 SBOM for Vetinari release checks."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from importlib import metadata
from pathlib import Path
from typing import Any

from license_expression import ExpressionError, get_spdx_licensing
from packaging.markers import UndefinedEnvironmentName
from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

try:
    from scripts.check_converter_lock import LockedConverterPackage, parse_converter_lock
    from scripts.release_license_policy import classify_release_license
except ModuleNotFoundError as exc:
    if exc.name not in {"scripts", "scripts.check_converter_lock", "scripts.release_license_policy"}:
        raise
    from check_converter_lock import LockedConverterPackage, parse_converter_lock  # type: ignore[no-redef]
    from release_license_policy import classify_release_license  # type: ignore[no-redef]

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python < 3.11 fallback
    import tomli as tomllib  # type: ignore[no-redef]


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "spdx.json"
PROJECT_SPDX_ID = "SPDXRef-Package-am-workbench"
NOASSERTION = "NOASSERTION"
_CARGO_METADATA_MAX_BYTES = 8 * 1024 * 1024
LOCK_FILE = "requirements-lock.txt"
DEFAULT_CARGO_MANIFESTS = (
    ROOT / "src-tauri/Cargo.toml",
    ROOT / "crates/amw-engine/Cargo.toml",
)
RELEASE_TARGET_ENVIRONMENT = {
    "implementation_name": "cpython",
    "implementation_version": "3.12.0",
    "os_name": "nt",
    "platform_machine": "AMD64",
    "platform_python_implementation": "CPython",
    "platform_release": "",
    "platform_system": "Windows",
    "platform_version": "",
    "python_full_version": "3.12.0",
    "python_version": "3.12",
    "sys_platform": "win32",
    "extra": "",
}
_SPDX_LICENSING = get_spdx_licensing()
_LEGACY_LICENSE_OVERRIDES = {
    ("annotated-types", "0.7.0"): "MIT",
    ("click", "8.1.8"): "BSD-3-Clause",
    ("colorama", "0.4.6"): "BSD-3-Clause",
    ("defusedxml", "0.7.1"): "PSF-2.0",
    ("faker", "40.31.0"): "MIT",
    ("huggingface-hub", "0.36.2"): "Apache-2.0",
    ("id", "1.6.1"): "Apache-2.0",
    ("markdown-it-py", "4.2.0"): "MIT",
    ("mdurl", "0.1.2"): "MIT",
    ("multidict", "6.7.1"): "Apache-2.0",
    ("multipart", "2.0.0"): "MIT",
    ("packaging", "25.0"): "Apache-2.0 OR BSD-2-Clause",
    ("pyopenssl", "26.2.0"): "Apache-2.0",
    ("rfc3161-client", "1.0.7"): "Apache-2.0",
    ("rfc8785", "0.1.4"): "Apache-2.0",
    ("rich-click", "1.9.8"): "MIT",
    ("sigstore", "4.3.0"): "Apache-2.0",
    ("sigstore-models", "0.0.6"): "Apache-2.0",
    ("sigstore-rekor-types", "0.0.18"): "Apache-2.0",
    ("tenacity", "9.1.4"): "Apache-2.0",
}


class DependencyResolutionError(ValueError):
    """Raised when canonical release dependency evidence cannot be resolved."""


@dataclass(frozen=True)
class RuntimePackage:
    """Exact installed package evidence admitted to the release dependency graph."""

    name: str
    version: str
    license_expression: str


@dataclass(frozen=True)
class RuntimeDependencyGraph:
    """Verified runtime packages, roots, and dependency relationships."""

    packages: dict[str, RuntimePackage]
    roots: frozenset[str]
    relationships: frozenset[tuple[str, str]]
    optional_names: frozenset[str] = frozenset()


@dataclass(frozen=True)
class CargoPackage:
    """Exact Cargo package evidence admitted to the release graph."""

    name: str
    version: str
    license_expression: str
    source_identity: str


@dataclass(frozen=True)
class CargoDependencyGraph:
    """Verified Cargo packages, manifest roots, and dependency relationships."""

    packages: dict[str, CargoPackage]
    roots: frozenset[str]
    relationships: frozenset[tuple[str, str]]


def _safe_spdx_id(name: str) -> str:
    token = re.sub(r"[^A-Za-z0-9.-]+", "-", name).strip("-")
    return f"SPDXRef-Package-{token or 'unknown'}"


def _requirement_name(requirement: str) -> str:
    return re.split(r"\s*[\[<>=!~;\s]", requirement.strip(), maxsplit=1)[0].strip()


def _normalized_spdx_expression(expression: str, *, context: str) -> str:
    try:
        parsed = _SPDX_LICENSING.parse(expression, validate=True, strict=True)
    except ExpressionError as exc:
        raise DependencyResolutionError(f"{context} has an invalid SPDX license expression: {expression!r}") from exc
    if parsed is None:
        raise DependencyResolutionError(f"{context} has an empty SPDX license expression")
    return str(parsed)


def _license_expression(
    package_metadata: metadata.PackageMetadata,
    *,
    package_name: str,
    package_version: str,
) -> str:
    legacy_error: DependencyResolutionError | None = None
    declared = package_metadata.get("License-Expression")
    if declared and declared.strip():
        return _normalized_spdx_expression(declared.strip(), context=f"{package_name}=={package_version}")
    legacy = package_metadata.get("License")
    if legacy and legacy.strip():
        try:
            return _normalized_spdx_expression(legacy.strip(), context=f"{package_name}=={package_version}")
        except DependencyResolutionError as exc:
            legacy_error = exc
    override = _LEGACY_LICENSE_OVERRIDES.get((canonicalize_name(package_name), package_version))
    if override is None:
        detail = legacy.strip().splitlines()[0][:120] if legacy and legacy.strip() else "missing"
        raise DependencyResolutionError(
            f"{package_name}=={package_version} has no validated License-Expression or exact legacy override ({detail})"
        ) from legacy_error
    return _normalized_spdx_expression(override, context=f"legacy override for {package_name}=={package_version}")


def _cargo_license_expression(expression: str, *, package_id: str) -> str:
    try:
        return _normalized_spdx_expression(expression, context=f"Cargo package {package_id!r}")
    except DependencyResolutionError as original_exc:
        if "/" not in expression:
            raise
        clarified = " OR ".join(part.strip() for part in expression.split("/") if part.strip())
        try:
            return _normalized_spdx_expression(clarified, context=f"Cargo package {package_id!r}")
        except DependencyResolutionError:
            raise original_exc from None


def _declared_optional_packages(pyproject: dict[str, Any], runtime_names: set[str]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    packages: list[dict[str, Any]] = []
    optional = pyproject.get("project", {}).get("optional-dependencies", {})
    dependencies: list[str] = []
    if isinstance(optional, dict):
        for group_deps in optional.values():
            if isinstance(group_deps, list):
                dependencies.extend(str(item) for item in group_deps)

    for requirement in dependencies:
        name = _requirement_name(str(requirement))
        if not name:
            continue
        normalized = canonicalize_name(name)
        if normalized in seen or normalized in runtime_names or normalized == "vetinari":
            continue
        seen.add(normalized)
        package: dict[str, Any] = {
            "name": name,
            "SPDXID": _safe_spdx_id(name),
            "downloadLocation": NOASSERTION,
            "filesAnalyzed": False,
            "licenseConcluded": NOASSERTION,
            "licenseDeclared": NOASSERTION,
            "copyrightText": NOASSERTION,
            "externalRefs": [
                {
                    "referenceCategory": "PACKAGE-MANAGER",
                    "referenceType": "purl",
                    "referenceLocator": f"pkg:pypi/{normalized}",
                }
            ],
        }
        packages.append(package)

    return sorted(packages, key=lambda item: str(item["name"]).lower())


def _parse_lock(root: Path) -> dict[str, str]:
    path = root / LOCK_FILE
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise DependencyResolutionError(f"canonical runtime dependency lock is unreadable: {path}") from exc
    locked: dict[str, str] = {}
    for line_number, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            requirement = Requirement(line)
        except InvalidRequirement as exc:
            raise DependencyResolutionError(f"invalid lock entry at {path}:{line_number}: {line}") from exc
        specifiers = list(requirement.specifier)
        if requirement.marker is not None or requirement.url is not None or len(specifiers) != 1:
            raise DependencyResolutionError(
                f"lock entry must be one unconditional exact version at {path}:{line_number}"
            )
        specifier = specifiers[0]
        if specifier.operator != "==" or "*" in specifier.version:
            raise DependencyResolutionError(f"lock entry must use an exact == pin at {path}:{line_number}")
        name = canonicalize_name(requirement.name)
        if name in locked:
            raise DependencyResolutionError(f"duplicate canonical lock entry for {name!r}")
        try:
            locked[name] = str(Version(specifier.version))
        except InvalidVersion as exc:
            raise DependencyResolutionError(f"lock entry has an invalid version at {path}:{line_number}") from exc
    return locked


def _marker_applies(requirement: Requirement, active_extras: set[str], environment: dict[str, str]) -> bool:
    if requirement.marker is None:
        return True
    try:
        return any(
            requirement.marker.evaluate({**environment, "extra": extra}, context="metadata")
            for extra in ({""} | active_extras)
        )
    except (KeyError, UndefinedEnvironmentName) as exc:
        raise DependencyResolutionError(f"unresolved environment marker for {requirement}") from exc


def resolve_runtime_dependency_graph(
    root: Path = ROOT,
    *,
    target_environment: dict[str, str] | None = None,
    distribution_provider: Callable[[str], metadata.Distribution] = metadata.distribution,
) -> RuntimeDependencyGraph:
    """Resolve the locked base-runtime dependency graph from exact metadata.

    Args:
        root: Project root containing ``pyproject.toml`` and the canonical lock.
        target_environment: Complete PEP 508 marker environment for the release target.
        distribution_provider: Metadata provider, injectable for deterministic tests.

    Returns:
        Verified runtime packages, root package names, and dependency edges.

    Raises:
        DependencyResolutionError: If a requirement, marker, pin, installed version,
            package metadata record, or dependency edge cannot be verified exactly.
    """
    environment = dict(RELEASE_TARGET_ENVIRONMENT if target_environment is None else target_environment)
    if set(RELEASE_TARGET_ENVIRONMENT) - set(environment):
        missing = sorted(set(RELEASE_TARGET_ENVIRONMENT) - set(environment))
        raise DependencyResolutionError(f"release target marker environment is incomplete: {missing}")
    pyproject = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    dependency_rows = pyproject.get("project", {}).get("dependencies")
    if not isinstance(dependency_rows, list) or not dependency_rows:
        raise DependencyResolutionError("pyproject base dependencies are missing")
    locked = _parse_lock(root)
    pending: deque[tuple[str | None, Requirement]] = deque()
    roots: set[str] = set()
    for raw_requirement in dependency_rows:
        try:
            requirement = Requirement(str(raw_requirement))
        except InvalidRequirement as exc:
            raise DependencyResolutionError(f"invalid pyproject base requirement: {raw_requirement!r}") from exc
        if not _marker_applies(requirement, set(), environment):
            continue
        name = canonicalize_name(requirement.name)
        roots.add(name)
        pending.append((None, requirement))

    packages: dict[str, RuntimePackage] = {}
    relationships: set[tuple[str, str]] = set()
    active_extras: dict[str, set[str]] = {}
    processed_extras: dict[str, frozenset[str]] = {}
    distributions: dict[str, metadata.Distribution] = {}
    while pending:
        parent, requirement = pending.popleft()
        name = canonicalize_name(requirement.name)
        locked_version = locked.get(name)
        if locked_version is None:
            raise DependencyResolutionError(f"reachable runtime dependency {name!r} has no exact canonical lock entry")
        if locked_version not in requirement.specifier:
            raise DependencyResolutionError(
                f"locked {name}=={locked_version} does not satisfy reachable requirement {requirement}"
            )
        if parent is not None:
            relationships.add((parent, name))
        extras = active_extras.setdefault(name, set())
        extras.update(canonicalize_name(extra) for extra in requirement.extras)
        if name not in distributions:
            try:
                distribution = distribution_provider(requirement.name)
            except metadata.PackageNotFoundError as exc:
                raise DependencyResolutionError(f"locked runtime dependency metadata is missing for {name!r}") from exc
            try:
                installed_version = str(Version(distribution.version))
            except InvalidVersion as exc:
                raise DependencyResolutionError(
                    f"installed runtime dependency {name!r} has an invalid version"
                ) from exc
            if installed_version != locked_version:
                raise DependencyResolutionError(
                    f"installed runtime dependency {name}=={installed_version} disagrees with lock {locked_version}"
                )
            distributions[name] = distribution
            packages[name] = RuntimePackage(
                name=str(distribution.metadata.get("Name") or requirement.name),
                version=installed_version,
                license_expression=_license_expression(
                    distribution.metadata,
                    package_name=name,
                    package_version=installed_version,
                ),
            )
        extras_snapshot = frozenset(extras)
        if processed_extras.get(name) == extras_snapshot:
            continue
        processed_extras[name] = extras_snapshot
        distribution = distributions[name]
        for raw_dependency in distribution.requires or []:
            try:
                dependency = Requirement(raw_dependency)
            except InvalidRequirement as exc:
                raise DependencyResolutionError(
                    f"invalid installed dependency metadata for {name}: {raw_dependency!r}"
                ) from exc
            if _marker_applies(dependency, extras, environment):
                pending.append((name, dependency))
    optional_names = frozenset(
        canonicalize_name(str(row["name"])) for row in _declared_optional_packages(pyproject, set(packages))
    )
    return RuntimeDependencyGraph(
        packages=packages,
        roots=frozenset(roots),
        relationships=frozenset(relationships),
        optional_names=optional_names,
    )


def _runtime_package_rows(graph: RuntimeDependencyGraph) -> list[dict[str, Any]]:
    rows = []
    for canonical_name, package in sorted(graph.packages.items()):
        rows.append({
            "name": package.name,
            "SPDXID": _safe_spdx_id(package.name),
            "versionInfo": package.version,
            "downloadLocation": NOASSERTION,
            "filesAnalyzed": False,
            "licenseConcluded": NOASSERTION,
            "licenseDeclared": package.license_expression,
            "copyrightText": NOASSERTION,
            "externalRefs": [
                {
                    "referenceCategory": "PACKAGE-MANAGER",
                    "referenceType": "purl",
                    "referenceLocator": f"pkg:pypi/{canonical_name}@{package.version}",
                }
            ],
        })
    return rows


def _validate_spdx_expression(expression: str) -> bool:
    """Return whether a string is a recognized SPDX license expression.

    Args:
        expression: SPDX license expression string or ``NOASSERTION``.

    Returns:
        True when the expression uses recognized SPDX symbols, False otherwise.
    """
    if not isinstance(expression, str) or not expression.strip():
        return False
    expr = expression.strip()
    if expr == NOASSERTION:
        return True
    try:
        parsed = _SPDX_LICENSING.parse(expr, validate=True, strict=True)
    except ExpressionError:
        return False
    return parsed is not None


def resolve_cargo_dependency_graph(
    root: Path = ROOT,
    *,
    manifest_paths: tuple[Path, ...] = DEFAULT_CARGO_MANIFESTS,
    cargo_path: str | None = None,
) -> CargoDependencyGraph:
    """Resolve exact Cargo dependency graphs for release manifests.

    Args:
        root: Repository root used to resolve manifest paths.
        manifest_paths: Repository-relative Cargo manifests serving as graph roots.
        cargo_path: Optional explicit Cargo executable.

    Returns:
        Verified reachable Cargo packages, manifest roots, and dependency edges.

    Raises:
        DependencyResolutionError: If Cargo, a manifest, metadata, package identity,
            dependency edge, or SPDX license expression cannot be resolved.
    """
    cargo = cargo_path or shutil.which("cargo")
    if cargo is None:
        raise DependencyResolutionError("Cargo executable is unavailable for strict Rust license evidence")
    packages: dict[str, CargoPackage] = {}
    roots: set[str] = set()
    relationships: set[tuple[str, str]] = set()
    for relative_manifest in manifest_paths:
        manifest = (root / relative_manifest).resolve()
        if not manifest.is_file():
            raise DependencyResolutionError(f"release Cargo manifest is missing: {relative_manifest.as_posix()}")
        try:
            completed = subprocess.run(
                [
                    cargo,
                    "metadata",
                    "--format-version",
                    "1",
                    "--locked",
                    "--manifest-path",
                    str(manifest),
                ],
                cwd=root,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=60,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise DependencyResolutionError(f"cargo metadata failed for {relative_manifest.as_posix()}: {exc}") from exc
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip() or "no diagnostic output"
            raise DependencyResolutionError(
                f"cargo metadata failed for {relative_manifest.as_posix()} with exit {completed.returncode}: {detail}"
            )
        if len(completed.stdout.encode("utf-8")) > _CARGO_METADATA_MAX_BYTES:
            raise DependencyResolutionError(
                f"cargo metadata exceeded its size budget for {relative_manifest.as_posix()}"
            )
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise DependencyResolutionError(
                f"cargo metadata was not valid JSON for {relative_manifest.as_posix()}"
            ) from exc
        package_rows = payload.get("packages") if isinstance(payload, dict) else None
        resolve = payload.get("resolve") if isinstance(payload, dict) else None
        node_rows = resolve.get("nodes") if isinstance(resolve, dict) else None
        if not isinstance(package_rows, list) or not isinstance(node_rows, list):
            raise DependencyResolutionError(f"cargo metadata graph is incomplete for {relative_manifest.as_posix()}")
        rows_by_id: dict[str, dict[str, Any]] = {}
        for row in package_rows:
            if not isinstance(row, dict) or not isinstance(row.get("id"), str):
                raise DependencyResolutionError(
                    f"cargo metadata contains a malformed package for {relative_manifest.as_posix()}"
                )
            package_id = row["id"]
            if package_id in rows_by_id:
                raise DependencyResolutionError(f"cargo metadata contains duplicate package id {package_id!r}")
            rows_by_id[package_id] = row
        nodes: dict[str, list[str]] = {}
        for row in node_rows:
            if not isinstance(row, dict) or not isinstance(row.get("id"), str):
                raise DependencyResolutionError(
                    f"cargo metadata contains a malformed node for {relative_manifest.as_posix()}"
                )
            dependency_ids = row.get("dependencies")
            if not isinstance(dependency_ids, list) or not all(isinstance(value, str) for value in dependency_ids):
                raise DependencyResolutionError(f"cargo metadata node has malformed dependencies: {row.get('id')!r}")
            nodes[row["id"]] = dependency_ids
        matching_roots = [
            package_id
            for package_id, row in rows_by_id.items()
            if isinstance(row.get("manifest_path"), str) and Path(row["manifest_path"]).resolve() == manifest
        ]
        if len(matching_roots) != 1:
            raise DependencyResolutionError(
                f"cargo metadata selected {len(matching_roots)} roots for {relative_manifest.as_posix()}"
            )
        manifest_root = matching_roots[0]
        roots.add(manifest_root)
        pending = deque([manifest_root])
        reached: set[str] = set()
        while pending:
            package_id = pending.popleft()
            if package_id in reached:
                continue
            reached.add(package_id)
            row = rows_by_id.get(package_id)
            dependency_ids = nodes.get(package_id)
            if row is None or dependency_ids is None:
                raise DependencyResolutionError(f"cargo metadata has an unresolved package node {package_id!r}")
            name = row.get("name")
            version = row.get("version")
            license_expression = row.get("license")
            if not all(isinstance(value, str) and value for value in (name, version, license_expression)):
                raise DependencyResolutionError(f"Cargo package {package_id!r} has incomplete version/license evidence")
            license_expression = _cargo_license_expression(license_expression, package_id=package_id)
            source = row.get("source")
            if source is None:
                package_manifest_value = row.get("manifest_path")
                if not isinstance(package_manifest_value, str):
                    raise DependencyResolutionError(f"Cargo path package {package_id!r} has no manifest identity")
                try:
                    package_manifest = Path(package_manifest_value).resolve().relative_to(root.resolve()).as_posix()
                except ValueError as exc:
                    raise DependencyResolutionError(
                        f"Cargo path package {package_id!r} resolves outside the release repository"
                    ) from exc
                source_identity = f"path:{package_manifest}#{name}@{version}"
            elif isinstance(source, str) and source:
                source_identity = f"{source}#{name}@{version}"
            else:
                raise DependencyResolutionError(f"Cargo package {package_id!r} has an invalid source identity")
            existing = packages.get(package_id)
            package = CargoPackage(
                name=name,
                version=version,
                license_expression=license_expression,
                source_identity=source_identity,
            )
            if existing is not None and existing != package:
                raise DependencyResolutionError(f"Cargo package identity changed across manifests: {package_id!r}")
            packages[package_id] = package
            for dependency_id in dependency_ids:
                if dependency_id not in rows_by_id:
                    raise DependencyResolutionError(
                        f"Cargo package {package_id!r} references unknown dependency {dependency_id!r}"
                    )
                relationships.add((package_id, dependency_id))
                pending.append(dependency_id)
    stable_identities = [package.source_identity for package in packages.values()]
    if len(stable_identities) != len(set(stable_identities)):
        raise DependencyResolutionError("Cargo graph contains colliding stable package identities")
    return CargoDependencyGraph(
        packages=packages,
        roots=frozenset(roots),
        relationships=frozenset(relationships),
    )


def _cargo_spdx_id(_package_id: str, package: CargoPackage) -> str:
    identity_hash = hashlib.sha256(package.source_identity.encode("utf-8")).hexdigest()[:12]
    return _safe_spdx_id(f"cargo-{package.name}-{package.version}-{identity_hash}")


def _cargo_package_rows(graph: CargoDependencyGraph) -> list[dict[str, Any]]:
    rows = []
    for package_id, package in sorted(
        graph.packages.items(), key=lambda item: (item[1].name, item[1].version, item[0])
    ):
        rows.append({
            "name": package.name,
            "SPDXID": _cargo_spdx_id(package_id, package),
            "versionInfo": package.version,
            "downloadLocation": NOASSERTION,
            "filesAnalyzed": False,
            "licenseConcluded": NOASSERTION,
            "licenseDeclared": package.license_expression,
            "copyrightText": NOASSERTION,
            "externalRefs": [
                {
                    "referenceCategory": "PACKAGE-MANAGER",
                    "referenceType": "purl",
                    "referenceLocator": f"pkg:cargo/{package.name}@{package.version}",
                }
            ],
        })
    return rows


def _converter_spdx_id(package: LockedConverterPackage) -> str:
    identity = f"{package.name}=={package.version};{package.marker or ''}"
    identity_hash = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:12]
    return _safe_spdx_id(f"converter-{package.name}-{package.version}-{identity_hash}")


def _converter_package_rows(packages: tuple[LockedConverterPackage, ...]) -> list[dict[str, Any]]:
    return [
        {
            "name": package.name,
            "SPDXID": _converter_spdx_id(package),
            "versionInfo": package.version,
            "downloadLocation": NOASSERTION,
            "filesAnalyzed": False,
            "licenseConcluded": NOASSERTION,
            "licenseDeclared": package.license_expression,
            "copyrightText": NOASSERTION,
            "comment": f"Hash-locked converter dependency; marker={package.marker or 'all targets'}",
            "externalRefs": [
                {
                    "referenceCategory": "PACKAGE-MANAGER",
                    "referenceType": "purl",
                    "referenceLocator": f"pkg:pypi/{package.name}@{package.version}",
                }
            ],
        }
        for package in packages
    ]


def build_spdx_document(root: Path = ROOT) -> dict[str, Any]:
    """Build the canonical SPDX document from verified release evidence.

    Args:
        root: Project root containing dependency manifests and exact metadata.

    Returns:
        SPDX 2.3 document with verified Python and Cargo dependency graphs.

    Raises:
        DependencyResolutionError: If a locked Python or Cargo graph cannot be proven.
    """
    pyproject = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    runtime_graph = resolve_runtime_dependency_graph(root)
    cargo_graph = resolve_cargo_dependency_graph(root)
    converter_packages = parse_converter_lock(
        root / "crates/amw-engine/converter/requirements-convert_lora_to_gguf.txt",
        root / "crates/amw-engine/converter/dependency-licenses.toml",
    )
    created = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    namespace_stamp = created.replace(":", "").replace("-", "").replace("Z", "Z")
    packages = [
        {
            "name": "AM Workbench",
            "SPDXID": PROJECT_SPDX_ID,
            "downloadLocation": NOASSERTION,
            "filesAnalyzed": False,
            "licenseConcluded": "MIT",
            "licenseDeclared": "MIT",
            "copyrightText": "Copyright (c) 2026 Vetinari contributors",
        },
        *_runtime_package_rows(runtime_graph),
        *_declared_optional_packages(pyproject, set(runtime_graph.packages)),
        *_cargo_package_rows(cargo_graph),
        *_converter_package_rows(converter_packages),
    ]
    package_ids = {
        canonicalize_name(str(package["name"])): str(package["SPDXID"])
        for package in packages
        if str(package.get("SPDXID", "")).startswith("SPDXRef-Package-")
        and not str(package.get("SPDXID", "")).startswith("SPDXRef-Package-converter-")
        and str(package.get("SPDXID")) != PROJECT_SPDX_ID
        and any(
            reference.get("referenceLocator", "").startswith("pkg:pypi/")
            for reference in package.get("externalRefs", [])
        )
    }
    relationships = [
        {
            "spdxElementId": PROJECT_SPDX_ID,
            "relationshipType": "DEPENDS_ON",
            "relatedSpdxElement": package_ids[root_name],
        }
        for root_name in sorted(runtime_graph.roots)
    ]
    relationships.extend(
        {
            "spdxElementId": package_ids[parent],
            "relationshipType": "DEPENDS_ON",
            "relatedSpdxElement": package_ids[dependency],
        }
        for parent, dependency in sorted(runtime_graph.relationships)
    )
    cargo_ids = {
        package_id: _cargo_spdx_id(package_id, package) for package_id, package in cargo_graph.packages.items()
    }
    relationships.extend(
        {
            "spdxElementId": PROJECT_SPDX_ID,
            "relationshipType": "DEPENDS_ON",
            "relatedSpdxElement": cargo_ids[root_id],
        }
        for root_id in sorted(cargo_graph.roots)
    )
    relationships.extend(
        {
            "spdxElementId": cargo_ids[parent],
            "relationshipType": "DEPENDS_ON",
            "relatedSpdxElement": cargo_ids[dependency],
        }
        for parent, dependency in sorted(cargo_graph.relationships)
    )
    relationships.extend(
        {
            "spdxElementId": PROJECT_SPDX_ID,
            "relationshipType": "DEPENDS_ON",
            "relatedSpdxElement": _converter_spdx_id(package),
        }
        for package in converter_packages
    )
    relationships.extend([
        {
            "spdxElementId": package["SPDXID"],
            "relationshipType": "OPTIONAL_DEPENDENCY_OF",
            "relatedSpdxElement": PROJECT_SPDX_ID,
        }
        for package in packages
        if not str(package.get("SPDXID", "")).startswith("SPDXRef-Package-converter-")
        and any(
            reference.get("referenceLocator", "").startswith("pkg:pypi/")
            for reference in package.get("externalRefs", [])
        )
        and canonicalize_name(str(package["name"])) not in runtime_graph.packages
    ])
    return {
        "spdxVersion": "SPDX-2.3",
        "dataLicense": "CC0-1.0",
        "SPDXID": "SPDXRef-DOCUMENT",
        "name": "AM Workbench release SBOM",
        "documentNamespace": f"https://am-workbench.local/spdx/vetinari-{namespace_stamp}",
        "creationInfo": {
            "created": created,
            "creators": ["Tool: scripts/generate_spdx_sbom.py"],
            "comment": (
                "Python runtime markers evaluated for CPython 3.12 on Windows AMD64; "
                "Rust dependencies resolved from Cargo.lock."
            ),
        },
        "packages": packages,
        "relationships": relationships,
    }


def validate_runtime_dependency_evidence(
    document: dict[str, Any],
    graph: RuntimeDependencyGraph,
) -> list[str]:
    """Validate exact runtime package and edge evidence in an SPDX document.

    Args:
        document: SPDX document to validate.
        graph: Canonical verified runtime dependency graph.

    Returns:
        Stable validation messages; empty means the runtime evidence is complete.
    """
    errors: list[str] = []
    rows = document.get("packages")
    if not isinstance(rows, list):
        return ["packages must be an array before runtime dependency validation"]
    package_rows: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("name"), str):
            continue
        if str(row.get("SPDXID", "")).startswith("SPDXRef-Package-converter-"):
            continue
        references = row.get("externalRefs")
        if not isinstance(references, list) or not any(
            isinstance(reference, dict) and str(reference.get("referenceLocator", "")).startswith("pkg:pypi/")
            for reference in references
        ):
            continue
        canonical_name = canonicalize_name(row["name"])
        if canonical_name in package_rows:
            errors.append(f"duplicate SPDX Python package identity: {canonical_name}")
            continue
        package_rows[canonical_name] = row
    expected_names = set(graph.packages) | set(graph.optional_names)
    unexpected_names = sorted(set(package_rows) - expected_names)
    missing_names = sorted(expected_names - set(package_rows))
    if unexpected_names:
        errors.append(f"SPDX contains unexpected Python package records: {', '.join(unexpected_names)}")
    if missing_names:
        errors.append(f"SPDX is missing Python package records: {', '.join(missing_names)}")
    for name, package in sorted(graph.packages.items()):
        row = package_rows.get(name)
        if row is None:
            continue
        if row.get("versionInfo") != package.version:
            errors.append(
                f"SPDX runtime package version drift for {name}: {row.get('versionInfo')!r} != {package.version!r}"
            )
        if row.get("licenseDeclared") != package.license_expression:
            errors.append(f"SPDX runtime package license drift for {name}")
        expected_purl = f"pkg:pypi/{name}@{package.version}"
        if not any(
            isinstance(reference, dict) and reference.get("referenceLocator") == expected_purl
            for reference in row.get("externalRefs", [])
        ):
            errors.append(f"SPDX runtime package purl missing for {name}")
    for name in sorted(graph.optional_names):
        row = package_rows.get(name)
        if row is None:
            continue
        expected_purl = f"pkg:pypi/{name}"
        if not any(
            isinstance(reference, dict) and reference.get("referenceLocator") == expected_purl
            for reference in row.get("externalRefs", [])
        ):
            errors.append(f"SPDX optional package purl missing for {name}")
    relationship_rows = document.get("relationships")
    if not isinstance(relationship_rows, list):
        return [*errors, "relationships must be an array before runtime dependency validation"]
    ids = {name: str(row["SPDXID"]) for name, row in package_rows.items() if isinstance(row.get("SPDXID"), str)}
    runtime_ids = set(ids.values())
    scoped_rows = [
        row
        for row in relationship_rows
        if isinstance(row, dict)
        and (row.get("spdxElementId") in runtime_ids or row.get("relatedSpdxElement") in runtime_ids)
    ]
    observed = {
        (row.get("spdxElementId"), row.get("relationshipType"), row.get("relatedSpdxElement")) for row in scoped_rows
    }
    if len(scoped_rows) != len(observed):
        errors.append("SPDX contains duplicate Python dependency relationships")
    expected = {(PROJECT_SPDX_ID, "DEPENDS_ON", ids[root_name]) for root_name in graph.roots if root_name in ids}
    expected.update({
        (ids[parent], "DEPENDS_ON", ids[dependency])
        for parent, dependency in graph.relationships
        if parent in ids and dependency in ids
    })
    expected.update({
        (ids[name], "OPTIONAL_DEPENDENCY_OF", PROJECT_SPDX_ID) for name in graph.optional_names if name in ids
    })
    for relationship in sorted(expected - observed, key=repr):
        errors.append(f"SPDX Python dependency relationship missing: {relationship}")
    for relationship in sorted(observed - expected, key=repr):
        errors.append(f"SPDX contains unexpected Python dependency relationship: {relationship}")
    return errors


def validate_cargo_dependency_evidence(
    document: dict[str, Any],
    graph: CargoDependencyGraph,
) -> list[str]:
    """Validate exact Cargo package and edge evidence in an SPDX document.

    Args:
        document: SPDX document to validate.
        graph: Canonical verified Cargo dependency graph.

    Returns:
        Stable validation messages; empty means the Cargo evidence is complete.
    """
    errors: list[str] = []
    rows = document.get("packages")
    if not isinstance(rows, list):
        return ["packages must be an array before Cargo dependency validation"]
    expected_ids = {package_id: _cargo_spdx_id(package_id, package) for package_id, package in graph.packages.items()}
    package_rows: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("SPDXID"), str):
            continue
        references = row.get("externalRefs")
        if not isinstance(references, list) or not any(
            isinstance(reference, dict) and str(reference.get("referenceLocator", "")).startswith("pkg:cargo/")
            for reference in references
        ):
            continue
        if row["SPDXID"] in package_rows:
            errors.append(f"duplicate SPDX Cargo package identity: {row['SPDXID']}")
            continue
        package_rows[row["SPDXID"]] = row
    unexpected_ids = sorted(set(package_rows) - set(expected_ids.values()))
    if unexpected_ids:
        errors.append(f"SPDX contains unexpected Cargo package records: {', '.join(unexpected_ids)}")
    for package_id, package in sorted(graph.packages.items()):
        spdx_id = expected_ids[package_id]
        row = package_rows.get(spdx_id)
        if row is None:
            errors.append(f"Cargo package missing from SPDX: {package.name}@{package.version} ({package_id})")
            continue
        if row.get("name") != package.name or row.get("versionInfo") != package.version:
            errors.append(f"SPDX Cargo package identity drift for {package_id}")
        if row.get("licenseDeclared") != package.license_expression:
            errors.append(f"SPDX Cargo package license drift for {package_id}")
        expected_purl = f"pkg:cargo/{package.name}@{package.version}"
        references = row.get("externalRefs", [])
        if not any(
            isinstance(reference, dict) and reference.get("referenceLocator") == expected_purl
            for reference in references
        ):
            errors.append(f"SPDX Cargo package purl missing for {package_id}")
    relationship_rows = document.get("relationships")
    if not isinstance(relationship_rows, list):
        return [*errors, "relationships must be an array before Cargo dependency validation"]
    cargo_ids = set(expected_ids.values()) | set(package_rows)
    scoped_rows = [
        row
        for row in relationship_rows
        if isinstance(row, dict)
        and (row.get("spdxElementId") in cargo_ids or row.get("relatedSpdxElement") in cargo_ids)
    ]
    observed = {
        (row.get("spdxElementId"), row.get("relationshipType"), row.get("relatedSpdxElement")) for row in scoped_rows
    }
    if len(scoped_rows) != len(observed):
        errors.append("SPDX contains duplicate Cargo dependency relationships")
    expected = {(PROJECT_SPDX_ID, "DEPENDS_ON", expected_ids[root_id]) for root_id in graph.roots}
    expected.update({
        (expected_ids[parent], "DEPENDS_ON", expected_ids[dependency]) for parent, dependency in graph.relationships
    })
    for relationship in sorted(expected - observed, key=repr):
        errors.append(f"SPDX Cargo dependency relationship missing: {relationship}")
    for relationship in sorted(observed - expected, key=repr):
        errors.append(f"SPDX contains unexpected Cargo dependency relationship: {relationship}")
    return errors


def validate_converter_dependency_evidence(
    document: dict[str, Any],
    packages: tuple[LockedConverterPackage, ...],
) -> list[str]:
    """Validate exact hash-locked converter packages in an SPDX document.

    Args:
        document: SPDX document to validate.
        packages: Canonical converter lock package rows.

    Returns:
        Stable validation messages; empty means converter evidence is complete.
    """
    rows = document.get("packages")
    relationships = document.get("relationships")
    if not isinstance(rows, list) or not isinstance(relationships, list):
        return ["SPDX packages/relationships must be arrays before converter validation"]
    expected = {_converter_spdx_id(package): package for package in packages}
    observed: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    for row in rows:
        if not isinstance(row, dict) or not str(row.get("SPDXID", "")).startswith("SPDXRef-Package-converter-"):
            continue
        spdx_id = row.get("SPDXID")
        if not isinstance(spdx_id, str) or spdx_id in observed:
            errors.append(f"duplicate SPDX converter package identity: {spdx_id!r}")
            continue
        observed[spdx_id] = row
    if set(observed) != set(expected):
        errors.append("SPDX converter package set does not match the canonical hash lock")
    for spdx_id, package in expected.items():
        row = observed.get(spdx_id)
        if row is None:
            continue
        if (
            row.get("name") != package.name
            or row.get("versionInfo") != package.version
            or row.get("licenseDeclared") != package.license_expression
        ):
            errors.append(f"SPDX converter package evidence drift for {package.name}=={package.version}")
        expected_purl = f"pkg:pypi/{package.name}@{package.version}"
        if not any(
            isinstance(reference, dict) and reference.get("referenceLocator") == expected_purl
            for reference in row.get("externalRefs", [])
        ):
            errors.append(f"SPDX converter purl missing for {package.name}=={package.version}")
    converter_ids = set(expected) | set(observed)
    scoped_rows = [
        row
        for row in relationships
        if isinstance(row, dict)
        and (row.get("spdxElementId") in converter_ids or row.get("relatedSpdxElement") in converter_ids)
    ]
    observed_relationships = {
        (row.get("spdxElementId"), row.get("relationshipType"), row.get("relatedSpdxElement")) for row in scoped_rows
    }
    expected_relationships = {(PROJECT_SPDX_ID, "DEPENDS_ON", spdx_id) for spdx_id in expected}
    if len(scoped_rows) != len(observed_relationships):
        errors.append("SPDX contains duplicate converter dependency relationships")
    if observed_relationships != expected_relationships:
        errors.append("SPDX converter dependency relationships do not match the canonical hash lock")
    return errors


def validate_spdx_document(
    document: dict[str, Any],
    runtime_graph: RuntimeDependencyGraph | None = None,
    cargo_graph: CargoDependencyGraph | None = None,
    converter_packages: tuple[LockedConverterPackage, ...] | None = None,
) -> list[str]:
    """Validate SPDX structure and optional exact dependency evidence.

    Args:
        document: SPDX document object.
        runtime_graph: Verified graph that the document must represent exactly.
        cargo_graph: Verified Cargo graph that the document must represent exactly.
        converter_packages: Verified hash-locked converter package rows.

    Returns:
        Stable validation messages; empty means all requested checks passed.
    """
    errors: list[str] = []
    if document.get("spdxVersion") != "SPDX-2.3":
        errors.append("spdxVersion must be SPDX-2.3")
    if document.get("SPDXID") != "SPDXRef-DOCUMENT":
        errors.append("document SPDXID must be SPDXRef-DOCUMENT")
    packages = document.get("packages")
    if not isinstance(packages, list) or not packages:
        errors.append("packages must be a non-empty list")
        return errors
    id_rows = [package.get("SPDXID") for package in packages if isinstance(package, dict)]
    ids = set(id_rows)
    if len(id_rows) != len(ids):
        errors.append("package SPDXIDs must be unique")
    if PROJECT_SPDX_ID not in ids:
        errors.append(f"{PROJECT_SPDX_ID} package is missing")
    for package in packages:
        if not isinstance(package, dict):
            errors.append("package entries must be objects")
            continue
        for key in ("name", "SPDXID", "downloadLocation", "filesAnalyzed", "licenseDeclared"):
            if key not in package:
                errors.append(f"package {package.get('name', '<unknown>')} missing {key}")
        license_declared = package.get("licenseDeclared")
        if isinstance(license_declared, str) and not _validate_spdx_expression(license_declared):
            errors.append(f"package {package.get('name', '<unknown>')} has invalid licenseDeclared")
        elif isinstance(license_declared, str) and classify_release_license(license_declared) == "blocked":
            errors.append(f"package {package.get('name', '<unknown>')} has a release-blocked licenseDeclared")
        spdx_id = package.get("SPDXID")
        if spdx_id == PROJECT_SPDX_ID:
            continue
        references = package.get("externalRefs")
        purls = (
            [
                reference.get("referenceLocator")
                for reference in references
                if isinstance(reference, dict)
                and reference.get("referenceCategory") == "PACKAGE-MANAGER"
                and reference.get("referenceType") == "purl"
            ]
            if isinstance(references, list)
            else []
        )
        if len(purls) != 1 or not isinstance(purls[0], str):
            errors.append(f"package {package.get('name', '<unknown>')} must have exactly one package-manager purl")
            continue
        is_converter = isinstance(spdx_id, str) and spdx_id.startswith("SPDXRef-Package-converter-")
        if purls[0].startswith("pkg:pypi/"):
            continue
        elif purls[0].startswith("pkg:cargo/"):
            if is_converter:
                errors.append(f"package {package.get('name', '<unknown>')} has an invalid Cargo classification")
        else:
            errors.append(f"package {package.get('name', '<unknown>')} has an unsupported package ecosystem")
    relationships = document.get("relationships")
    if not isinstance(relationships, list):
        errors.append("relationships must be an array")
    else:
        for relationship in relationships:
            if not isinstance(relationship, dict):
                errors.append("relationship entries must be objects")
                continue
            source = relationship.get("spdxElementId")
            target = relationship.get("relatedSpdxElement")
            if source not in ids or target not in ids:
                errors.append(f"relationship references an unknown package: {source!r} -> {target!r}")
    if runtime_graph is not None:
        errors.extend(validate_runtime_dependency_evidence(document, runtime_graph))
    if cargo_graph is not None:
        errors.extend(validate_cargo_dependency_evidence(document, cargo_graph))
    if converter_packages is not None:
        errors.extend(validate_converter_dependency_evidence(document, converter_packages))
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--check", action="store_true", help="Validate the output file instead of writing it.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List Python and Rust crate package names that would be written, without producing the SBOM file.",
    )
    args = parser.parse_args(argv)

    if args.check:
        document = json.loads(args.output.read_text(encoding="utf-8"))
        errors = validate_spdx_document(
            document,
            resolve_runtime_dependency_graph(ROOT),
            resolve_cargo_dependency_graph(ROOT),
            parse_converter_lock(
                ROOT / "crates/amw-engine/converter/requirements-convert_lora_to_gguf.txt",
                ROOT / "crates/amw-engine/converter/dependency-licenses.toml",
            ),
        )
        if errors:
            for error in errors:
                print(error)
            return 1
        return 0

    if args.dry_run:
        document = build_spdx_document(ROOT)
        for package in document.get("packages", []):
            purl_locator = ""
            for ref in package.get("externalRefs", []):
                if ref.get("referenceType") == "purl":
                    purl_locator = ref.get("referenceLocator", "")
                    break
            origin = (
                "python"
                if purl_locator.startswith("pkg:pypi/")
                else ("cargo crate (Rust)" if purl_locator.startswith("pkg:cargo/") else "project")
            )
            print(f"{origin}: {package.get('name')}")
        return 0

    document = build_spdx_document(ROOT)
    args.output.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
