#!/usr/bin/env python3
"""Validate AM Workbench dependency, license, and optional-extra export proof."""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CARGO_MANIFEST = "src-tauri/Cargo.toml"  # Rust crate dependencies for the Tauri shell
ROOT_CARGO_MANIFEST = "Cargo.toml"
CONVERTER_LOCK = "crates/amw-engine/converter/requirements-convert_lora_to_gguf.txt"
CONVERTER_LICENSES = "crates/amw-engine/converter/dependency-licenses.toml"
ENGINE_CARGO_MANIFEST = "crates/amw-engine/Cargo.toml"

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python < 3.11 fallback
    import tomli as tomllib  # type: ignore[no-redef]

try:
    from scripts.check_converter_lock import ConverterLockError, parse_converter_lock
    from scripts.generate_spdx_sbom import (
        DependencyResolutionError,
        _validate_spdx_expression,
        resolve_cargo_dependency_graph,
        resolve_runtime_dependency_graph,
        validate_cargo_dependency_evidence,
        validate_converter_dependency_evidence,
        validate_runtime_dependency_evidence,
    )
    from scripts.release_license_policy import classify_release_license
except ModuleNotFoundError as exc:
    if exc.name not in {
        "scripts",
        "scripts.check_converter_lock",
        "scripts.generate_spdx_sbom",
        "scripts.release_license_policy",
    }:
        raise
    from check_converter_lock import ConverterLockError, parse_converter_lock  # type: ignore[no-redef]
    from generate_spdx_sbom import (  # type: ignore[no-redef]
        DependencyResolutionError,
        _validate_spdx_expression,
        resolve_cargo_dependency_graph,
        resolve_runtime_dependency_graph,
        validate_cargo_dependency_evidence,
        validate_converter_dependency_evidence,
        validate_runtime_dependency_evidence,
    )
    from release_license_policy import classify_release_license  # type: ignore[no-redef]

THIRD_PARTY_PATH = "THIRD_PARTY_LICENSES.md"
SPDX_PATH = "spdx.json"
MARKER = "<!-- dependency-license-export:v1 -->"
PROJECT_SPDX_ID = "SPDXRef-Package-am-workbench"
NOASSERTION = "NOASSERTION"
PROJECT_NAMES = {"vetinari", "am-workbench"}
REQUIRED_SECTIONS = (
    "## Project License Authority",
    "## Direct Runtime Dependencies",
    "## Locked Runtime Transitive Coverage",
    "## Locked Converter Dependency Coverage",
    "## Locked Rust Dependency Coverage",
    "## Optional Extra Coverage",
    "## SPDX SBOM Cross-Check",
    "## Release Disposition",
)
ALLOWED_EXTRA_CLASSES = {
    "base",
    "optional-runtime",
    "optional-native",
    "optional-ml",
    "optional-cloud",
    "dev-only",
    "meta-extra",
}


@dataclass(frozen=True)
class Finding:
    code: str
    path: str
    message: str


@dataclass(frozen=True)
class DependencyExport:
    direct_dependencies: list[str]
    optional_extras: dict[str, list[str]]
    package_names: list[str]
    runtime_transitive_dependencies: list[str]
    spdx_package_names: list[str]
    spdx_noassertion_packages: list[str]
    cargo_package_count: int = 0
    converter_package_count: int = 0


class DependencyLicenseError(ValueError):
    """Raised when required dependency/license export evidence is unreadable."""


def _repo_relative(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        payload = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise DependencyLicenseError(f"missing pyproject: {_repo_relative(path, PROJECT_ROOT)}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise DependencyLicenseError(f"malformed pyproject: {exc}") from exc
    if not isinstance(payload, dict):
        raise DependencyLicenseError("malformed pyproject: root must be a table")
    return payload


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise DependencyLicenseError(f"missing JSON artifact: {_repo_relative(path, PROJECT_ROOT)}") from exc
    except json.JSONDecodeError as exc:
        raise DependencyLicenseError(f"malformed JSON artifact: {_repo_relative(path, PROJECT_ROOT)}: {exc}") from exc


def _requirement_name(requirement: str) -> str:
    return re.split(r"\s*[\[<>=!~;\s,]", requirement.strip(), maxsplit=1)[0].strip()


def _extra_names(requirement: str) -> list[str]:
    match = re.match(r"\s*[A-Za-z0-9_.-]+\[([^\]]+)\]", requirement)
    if not match:
        return []
    return sorted({part.strip() for part in match.group(1).split(",") if part.strip()})


def _canonical_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _is_project_self_reference(requirement: str, project_name: str) -> bool:
    return _canonical_name(_requirement_name(requirement)) in {_canonical_name(project_name), *PROJECT_NAMES}


def _dependency_export_from_pyproject(pyproject: dict[str, Any]) -> DependencyExport:
    project = pyproject.get("project")
    if not isinstance(project, dict):
        raise DependencyLicenseError("malformed pyproject: missing [project] table")
    project_name = str(project.get("name") or "vetinari")
    direct = [_requirement_name(str(item)) for item in project.get("dependencies", []) if _requirement_name(str(item))]
    optional_raw = project.get("optional-dependencies", {})
    if not isinstance(optional_raw, dict):
        raise DependencyLicenseError("malformed pyproject: [project.optional-dependencies] must be a table")

    optional: dict[str, list[str]] = {}
    package_names = {_canonical_name(name): name for name in direct}
    for extra, requirements in optional_raw.items():
        if not isinstance(extra, str) or not extra:
            raise DependencyLicenseError("malformed pyproject: optional extra names must be non-empty strings")
        if not isinstance(requirements, list):
            raise DependencyLicenseError(f"malformed pyproject: optional extra {extra!r} must be a list")
        names: list[str] = []
        for requirement in requirements:
            requirement_text = str(requirement)
            if _is_project_self_reference(requirement_text, project_name):
                names.extend(f"extra:{nested}" for nested in _extra_names(requirement_text))
                continue
            name = _requirement_name(requirement_text)
            if not name:
                continue
            names.append(name)
            package_names.setdefault(_canonical_name(name), name)
        optional[extra] = sorted(dict.fromkeys(names), key=str.lower)
    return DependencyExport(
        direct_dependencies=sorted(dict.fromkeys(direct), key=str.lower),
        optional_extras=dict(sorted(optional.items())),
        package_names=sorted(package_names.values(), key=str.lower),
        runtime_transitive_dependencies=[],
        spdx_package_names=[],
        spdx_noassertion_packages=[],
    )


def _spdx_package_license_map(document: Any) -> dict[str, str]:
    if not isinstance(document, dict):
        raise DependencyLicenseError("malformed SPDX: root must be an object")
    if document.get("spdxVersion") != "SPDX-2.3":
        raise DependencyLicenseError("malformed SPDX: spdxVersion must be SPDX-2.3")
    packages = document.get("packages")
    if not isinstance(packages, list):
        raise DependencyLicenseError("malformed SPDX: packages must be a list")
    package_map: dict[str, str] = {}
    project_seen = False
    for package in packages:
        if not isinstance(package, dict):
            raise DependencyLicenseError("malformed SPDX: package entries must be objects")
        spdx_id = package.get("SPDXID")
        name = package.get("name")
        if spdx_id == PROJECT_SPDX_ID:
            project_seen = True
            continue
        if str(spdx_id).startswith("SPDXRef-Package-converter-"):
            continue
        references = package.get("externalRefs")
        locators = (
            [str(reference.get("referenceLocator", "")) for reference in references if isinstance(reference, dict)]
            if isinstance(references, list)
            else []
        )
        if any(locator.startswith("pkg:cargo/") for locator in locators):
            continue
        if locators and not any(locator.startswith("pkg:pypi/") for locator in locators):
            continue
        if not isinstance(name, str) or not name:
            raise DependencyLicenseError("malformed SPDX: dependency package missing name")
        license_declared = package.get("licenseDeclared")
        if not isinstance(license_declared, str) or not license_declared.strip():
            raise DependencyLicenseError(f"malformed SPDX: package {name} missing licenseDeclared")
        if not _validate_spdx_expression(license_declared):
            raise DependencyLicenseError(f"malformed SPDX: package {name} has invalid licenseDeclared")
        canonical_name = _canonical_name(name)
        if canonical_name in package_map:
            raise DependencyLicenseError(f"malformed SPDX: duplicate Python package identity {name}")
        package_map[canonical_name] = license_declared.strip()
    if not project_seen:
        raise DependencyLicenseError(f"malformed SPDX: missing {PROJECT_SPDX_ID}")
    return package_map


def _spdx_cargo_package_rows(document: Any) -> list[tuple[str, str, str]]:
    """Extract exact Cargo name, version, and license rows from SPDX.

    Args:
        document: Parsed SPDX document.

    Returns:
        Deterministically sorted Cargo package identity and license rows.

    Raises:
        DependencyLicenseError: If a Cargo package row is incomplete.
    """
    if not isinstance(document, dict) or not isinstance(document.get("packages"), list):
        raise DependencyLicenseError("malformed SPDX: packages must be a list")
    rows: set[tuple[str, str, str]] = set()
    for package in document["packages"]:
        if not isinstance(package, dict):
            raise DependencyLicenseError("malformed SPDX: package entries must be objects")
        references = package.get("externalRefs")
        locators = (
            [str(reference.get("referenceLocator", "")) for reference in references if isinstance(reference, dict)]
            if isinstance(references, list)
            else []
        )
        if not any(locator.startswith("pkg:cargo/") for locator in locators):
            continue
        name = package.get("name")
        version = package.get("versionInfo")
        license_declared = package.get("licenseDeclared")
        if not all(isinstance(value, str) and value.strip() for value in (name, version, license_declared)):
            raise DependencyLicenseError("malformed SPDX: Cargo package identity/license is incomplete")
        if not _validate_spdx_expression(license_declared):
            raise DependencyLicenseError(f"malformed SPDX: Cargo package {name}@{version} has invalid licenseDeclared")
        rows.add((name, version, license_declared))
    return sorted(rows, key=lambda row: (row[0].lower(), row[1], row[2]))


def _lines_for_token(markdown: str, token: str) -> list[str]:
    pattern = re.compile(rf"^\|\s*`{re.escape(token)}`\s*\|.*$", re.IGNORECASE | re.MULTILINE)
    return [match.group(0) for match in pattern.finditer(markdown)]


def _line_for_token(markdown: str, token: str) -> str | None:
    lines = _lines_for_token(markdown, token)
    return lines[0] if lines else None


def _extra_release_class(line: str) -> str | None:
    for release_class in ALLOWED_EXTRA_CLASSES:
        if f"`{release_class}`" in line or f"| {release_class} " in line:
            return release_class
    return None


def validate_export(
    root: Path = PROJECT_ROOT, *, strict: bool = False, all_extras: bool = False
) -> tuple[DependencyExport, list[Finding]]:
    root = root.resolve()
    findings: list[Finding] = []
    pyproject = _read_toml(root / "pyproject.toml")
    export = _dependency_export_from_pyproject(pyproject)
    project_raw = pyproject.get("project")
    project = project_raw if isinstance(project_raw, dict) else {}

    if project.get("license") != "MIT":
        findings.append(Finding("DLE001", "pyproject.toml", "project license must be MIT"))
    license_files = project.get("license-files")
    if not isinstance(license_files, list) or {"LICENSE", "NOTICE"} - {str(item) for item in license_files}:
        findings.append(Finding("DLE002", "pyproject.toml", "license-files must include LICENSE and NOTICE"))
    for required_file in ("LICENSE", "NOTICE"):
        if not (root / required_file).exists():
            findings.append(Finding("DLE003", required_file, "required legal file is missing"))

    cargo_manifest = root / ROOT_CARGO_MANIFEST
    if cargo_manifest.exists():
        try:
            cargo_toml = tomllib.loads(cargo_manifest.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as exc:
            findings.append(Finding("DLE013", ROOT_CARGO_MANIFEST, f"malformed Cargo manifest: {exc}"))
        else:
            workspace = cargo_toml.get("workspace")
            workspace_package = workspace.get("package") if isinstance(workspace, dict) else None
            workspace_license = workspace_package.get("license") if isinstance(workspace_package, dict) else None
            if workspace_license != "MIT":
                findings.append(
                    Finding(
                        "DLE014",
                        ROOT_CARGO_MANIFEST,
                        "workspace.package.license must be SPDX MIT to match LICENSE/NOTICE release authority",
                    )
                )

    try:
        markdown = (root / THIRD_PARTY_PATH).read_text(encoding="utf-8")
    except FileNotFoundError:
        findings.append(Finding("DLE004", THIRD_PARTY_PATH, "third-party license export is missing"))
        markdown = ""
    if markdown:
        if MARKER not in markdown:
            findings.append(Finding("DLE005", THIRD_PARTY_PATH, "dependency-license-export marker is missing"))
        for section in REQUIRED_SECTIONS:
            if section not in markdown:
                findings.append(Finding("DLE006", THIRD_PARTY_PATH, f"required section missing: {section}"))

    spdx_document: Any = None
    cargo_package_rows: list[tuple[str, str, str]] = []
    try:
        spdx_document = _load_json(root / SPDX_PATH)
        spdx_map = _spdx_package_license_map(spdx_document)
        cargo_package_rows = _spdx_cargo_package_rows(spdx_document)
    except DependencyLicenseError as exc:
        findings.append(Finding("DLE007", SPDX_PATH, str(exc)))
        spdx_map = {}
    spdx_names = sorted(spdx_map, key=str.lower)
    noassertion = sorted(name for name, license_text in spdx_map.items() if license_text == NOASSERTION)
    for name, license_text in sorted(spdx_map.items()):
        if _disposition_for_license(license_text).startswith("blocked-license-policy"):
            findings.append(Finding("DLE020", SPDX_PATH, f"{name} uses release-blocked license {license_text}"))
    runtime_transitives: list[str] = []
    if (root / "requirements-lock.txt").is_file() and spdx_document is not None:
        try:
            runtime_graph = resolve_runtime_dependency_graph(root)
        except (DependencyResolutionError, OSError, tomllib.TOMLDecodeError) as exc:
            findings.append(Finding("DLE015", "requirements-lock.txt", f"runtime dependency closure failed: {exc}"))
        else:
            runtime_transitives = sorted(
                (runtime_graph.packages[name].name for name in set(runtime_graph.packages) - set(runtime_graph.roots)),
                key=str.lower,
            )
            for error in validate_runtime_dependency_evidence(spdx_document, runtime_graph):
                findings.append(Finding("DLE016", SPDX_PATH, error))
    converter_package_count = 0
    if spdx_document is not None and (root / ENGINE_CARGO_MANIFEST).is_file():
        try:
            converter_packages = parse_converter_lock(
                root / CONVERTER_LOCK,
                root / CONVERTER_LICENSES,
            )
        except ConverterLockError as exc:
            findings.append(Finding("DLE021", CONVERTER_LOCK, f"converter dependency closure failed: {exc}"))
        else:
            converter_package_count = len(converter_packages)
            for package in converter_packages:
                if _disposition_for_license(package.license_expression).startswith("blocked-license-policy"):
                    findings.append(
                        Finding(
                            "DLE024",
                            CONVERTER_LICENSES,
                            f"{package.name}=={package.version} uses release-blocked license "
                            f"{package.license_expression}",
                        )
                    )
            for error in validate_converter_dependency_evidence(spdx_document, converter_packages):
                findings.append(Finding("DLE022", SPDX_PATH, error))
    export = DependencyExport(
        direct_dependencies=export.direct_dependencies,
        optional_extras=export.optional_extras,
        package_names=export.package_names,
        runtime_transitive_dependencies=runtime_transitives,
        spdx_package_names=spdx_names,
        spdx_noassertion_packages=noassertion,
        cargo_package_count=len(cargo_package_rows),
        converter_package_count=converter_package_count,
    )

    for name in export.package_names:
        canonical = _canonical_name(name)
        if canonical not in spdx_map:
            findings.append(Finding("DLE008", SPDX_PATH, f"SPDX package missing for {name}"))
            continue
        rows = _lines_for_token(markdown, name)
        if not rows:
            findings.append(Finding("DLE009", THIRD_PARTY_PATH, f"dependency row missing for {name}"))
            continue
        if spdx_map[canonical] == NOASSERTION and not any("metadata-unresolved" in row for row in rows):
            findings.append(
                Finding(
                    "DLE010",
                    THIRD_PARTY_PATH,
                    f"{name} has NOASSERTION SPDX license without metadata-unresolved disposition",
                )
            )

    for name in export.runtime_transitive_dependencies:
        canonical = _canonical_name(name)
        if canonical not in spdx_map:
            findings.append(Finding("DLE016", SPDX_PATH, f"locked runtime transitive missing from SPDX: {name}"))
            continue
        rows = _lines_for_token(markdown, name)
        if not rows:
            findings.append(Finding("DLE017", THIRD_PARTY_PATH, f"runtime transitive row missing for {name}"))
        elif spdx_map[canonical] == NOASSERTION and not any("metadata-unresolved" in row for row in rows):
            findings.append(
                Finding(
                    "DLE018",
                    THIRD_PARTY_PATH,
                    f"{name} transitive has NOASSERTION SPDX license without metadata-unresolved disposition",
                )
            )

    for name, version, license_text in cargo_package_rows:
        if _cargo_license_report_line(name, version, license_text) not in markdown:
            findings.append(
                Finding(
                    "DLE019",
                    THIRD_PARTY_PATH,
                    f"Cargo dependency row missing for {name}@{version}",
                )
            )
    if spdx_document is not None and (root / ENGINE_CARGO_MANIFEST).is_file():
        try:
            converter_packages = parse_converter_lock(
                root / CONVERTER_LOCK,
                root / CONVERTER_LICENSES,
            )
        except ConverterLockError:
            converter_packages = ()
        for package in converter_packages:
            if _cargo_license_report_line(package.name, package.version, package.license_expression) not in markdown:
                findings.append(
                    Finding(
                        "DLE023",
                        THIRD_PARTY_PATH,
                        f"converter dependency row missing for {package.name}=={package.version}",
                    )
                )

    extras_to_check = (
        export.optional_extras
        if all_extras
        else {key: value for key, value in export.optional_extras.items() if key in {"core", "dev", "all"}}
    )
    for extra in extras_to_check:
        row = _line_for_token(markdown, extra)
        if row is None:
            findings.append(Finding("DLE011", THIRD_PARTY_PATH, f"optional extra row missing for {extra}"))
            continue
        if _extra_release_class(row) is None:
            findings.append(Finding("DLE012", THIRD_PARTY_PATH, f"optional extra {extra} missing release class"))

    if strict and findings:
        return export, findings
    return export, findings


def _license_for(name: str, spdx_map: dict[str, str]) -> str:
    return spdx_map.get(_canonical_name(name), NOASSERTION)


def _disposition_for_license(license_text: str) -> str:
    disposition = classify_release_license(license_text)
    if disposition == "unresolved":
        return "metadata-unresolved; not release-bundled without certifier proof"
    if disposition == "blocked":
        return "blocked-license-policy; not release-bundled"
    if disposition == "conditional":
        return "conditional-source-obligations-recorded"
    return "compatible-attribution-required"


def _cargo_license_report_line(name: str, version: str, license_text: str) -> str:
    return f"| `{name}` | `{version}` | `{license_text}` | {_disposition_for_license(license_text)} |"


def _release_class_for_extra(extra: str) -> str:
    if extra in {"dev", "audit-deterministic", "audit-inference-eval"}:
        return "dev-only"
    if extra in {"core", "all"}:
        return "meta-extra"
    if extra in {"local", "vllm", "sglang", "audio", "video", "comfyui", "image", "ml", "training"}:
        return "optional-ml"
    if extra in {"cloud", "observability", "redteam"}:
        return "optional-cloud"
    if extra in {"crypto", "notifications"}:
        return "optional-native"
    return "optional-runtime"


def build_third_party_report(root: Path = PROJECT_ROOT) -> str:
    root = root.resolve()
    pyproject = _read_toml(root / "pyproject.toml")
    export = _dependency_export_from_pyproject(pyproject)
    spdx_document = _load_json(root / SPDX_PATH)
    spdx_map = _spdx_package_license_map(spdx_document)
    cargo_package_rows = _spdx_cargo_package_rows(spdx_document)
    converter_packages = (
        parse_converter_lock(root / CONVERTER_LOCK, root / CONVERTER_LICENSES)
        if (root / ENGINE_CARGO_MANIFEST).is_file()
        else ()
    )
    runtime_transitives = []
    if (root / "requirements-lock.txt").is_file():
        runtime_graph = resolve_runtime_dependency_graph(root)
        runtime_transitives = [
            runtime_graph.packages[name] for name in sorted(set(runtime_graph.packages) - set(runtime_graph.roots))
        ]
    generated_at = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    lines = [
        "# Third Party Licenses",
        "",
        "> Generated release dependency and optional-extra license export proof.",
        "",
        MARKER,
        "",
        f"Generated: {generated_at}",
        "",
        "## Project License Authority",
        "",
        "- Project package metadata declares `MIT` in `pyproject.toml`.",
        "- `LICENSE` and `NOTICE` are required release legal files.",
        "- `spdx.json` is the machine-readable SPDX package rollup checked by this ledger.",
        "",
        "## Direct Runtime Dependencies",
        "",
        "| Package | SPDX licenseDeclared | Release disposition |",
        "| --- | --- | --- |",
    ]
    for name in export.direct_dependencies:
        license_text = _license_for(name, spdx_map)
        lines.append(f"| `{name}` | `{license_text}` | {_disposition_for_license(license_text)} |")

    lines.extend([
        "",
        "## Locked Runtime Transitive Coverage",
        "",
        "| Package | Locked version | SPDX licenseDeclared | Release disposition |",
        "| --- | --- | --- | --- |",
    ])
    for package in runtime_transitives:
        license_text = _license_for(package.name, spdx_map)
        lines.append(
            f"| `{package.name}` | `{package.version}` | `{license_text}` | {_disposition_for_license(license_text)} |"
        )
    if not runtime_transitives:
        lines.append("| _none_ | _none_ | _none_ | no canonical runtime lock in this fixture |")

    lines.extend([
        "",
        "## Locked Converter Dependency Coverage",
        "",
        "| Package | Locked version | SPDX licenseDeclared | Release disposition |",
        "| --- | --- | --- | --- |",
    ])
    for package in converter_packages:
        lines.append(_cargo_license_report_line(package.name, package.version, package.license_expression))
    if not converter_packages:
        lines.append("| _none_ | _none_ | _none_ | no converter lock in this fixture |")

    lines.extend([
        "",
        "## Locked Rust Dependency Coverage",
        "",
        "| Crate | Locked version | SPDX licenseDeclared | Release disposition |",
        "| --- | --- | --- | --- |",
    ])
    for name, version, license_text in cargo_package_rows:
        lines.append(_cargo_license_report_line(name, version, license_text))
    if not cargo_package_rows:
        lines.append("| _none_ | _none_ | _none_ | no Cargo package rows in this fixture |")

    lines.extend([
        "",
        "## Optional Extra Coverage",
        "",
        "| Extra | Release class | Declared requirements |",
        "| --- | --- | --- |",
    ])
    for extra, requirements in export.optional_extras.items():
        release_class = _release_class_for_extra(extra)
        requirement_text = ", ".join(f"`{item}`" for item in requirements) if requirements else "_none_"
        lines.append(f"| `{extra}` | `{release_class}` | {requirement_text} |")

    lines.extend([
        "",
        "## SPDX SBOM Cross-Check",
        "",
        "| Package | SPDX licenseDeclared | Export disposition |",
        "| --- | --- | --- |",
    ])
    for name in export.package_names:
        license_text = _license_for(name, spdx_map)
        lines.append(f"| `{name}` | `{license_text}` | {_disposition_for_license(license_text)} |")

    lines.extend([
        "",
        "## Release Disposition",
        "",
        "- This ledger covers locked Python and Rust dependencies plus declared Python extras from `pyproject.toml` and `spdx.json`.",
        "- Rows marked `metadata-unresolved` are fail-closed: they are not certified for bundled release artifacts until the release certifier records authoritative transitive license proof.",
        "- Optional extras are isolated by release class; installing `vetinari[all]` is not treated as the default release posture.",
        "",
    ])
    return "\n".join(lines)


def write_third_party_report(root: Path = PROJECT_ROOT) -> Path:
    path = root.resolve() / THIRD_PARTY_PATH
    path.write_text(build_third_party_report(root), encoding="utf-8")
    return path


def _write_evidence(path: Path, export: DependencyExport, findings: list[Finding], passed: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "passed": passed,
        "direct_dependency_count": len(export.direct_dependencies),
        "runtime_transitive_dependency_count": len(export.runtime_transitive_dependencies),
        "optional_extra_count": len(export.optional_extras),
        "package_count": len(export.package_names),
        "spdx_package_count": len(export.spdx_package_names),
        "spdx_noassertion_packages": export.spdx_noassertion_packages,
        "cargo_package_count": export.cargo_package_count,
        "converter_package_count": export.converter_package_count,
        "findings": [asdict(finding) for finding in findings],
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def check_rust_deps(root: Path) -> list[Finding]:
    """Verify locked Cargo graph package, license, and edge evidence.

    Args:
        root: Repository root containing the Cargo manifest and SPDX artifact.

    Returns:
        Fail-closed findings for unresolved Cargo metadata or stale SPDX evidence.
    """
    try:
        graph = resolve_cargo_dependency_graph(root)
    except DependencyResolutionError as exc:
        return [Finding(code="DLE-RUST-000", path=CARGO_MANIFEST, message=str(exc))]
    policy_findings = [
        Finding(
            code="DLE-RUST-002",
            path=CARGO_MANIFEST,
            message=f"{package.name}@{package.version} uses release-blocked license {package.license_expression}",
        )
        for package in graph.packages.values()
        if _disposition_for_license(package.license_expression).startswith("blocked-license-policy")
    ]
    spdx_path = root / SPDX_PATH
    try:
        document = json.loads(spdx_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [
            *policy_findings,
            Finding(code="DLE-RUST-001", path=SPDX_PATH, message=f"SPDX artifact is unreadable: {exc}"),
        ]
    errors = validate_cargo_dependency_evidence(document, graph)
    return [
        *policy_findings,
        *(Finding(code="DLE-RUST-001", path=SPDX_PATH, message=error) for error in errors),
    ]


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--all-extras", action="store_true")
    parser.add_argument("--write-third-party", action="store_true")
    parser.add_argument("--evidence-output", type=Path)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    if args.write_third_party:
        write_third_party_report(args.root)

    try:
        export, findings = validate_export(args.root, strict=args.strict, all_extras=args.all_extras)
    except DependencyLicenseError as exc:
        export = DependencyExport([], {}, [], [], [], [])
        findings = [Finding("DLE000", str(args.root), str(exc))]
    findings = list(findings) + check_rust_deps(args.root)
    passed = not findings
    if args.evidence_output:
        _write_evidence(args.evidence_output, export, findings, passed)
    if args.json:
        print(
            json.dumps(
                {"passed": passed, "export": asdict(export), "findings": [asdict(item) for item in findings]},
                indent=2,
                sort_keys=True,
            )
        )
    elif findings:
        for finding in findings:
            print(f"{finding.code} {finding.path}: {finding.message}", file=sys.stderr)
    else:
        print(
            "dependency license export passed: "
            f"direct={len(export.direct_dependencies)} extras={len(export.optional_extras)} packages={len(export.package_names)}"
        )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
