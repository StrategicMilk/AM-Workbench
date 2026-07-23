#!/usr/bin/env python3
"""Release evidence certifier helpers.

These checks are intentionally strict and fixture-friendly. They are meant to
fail closed before release evidence can cite generated files, package metadata,
tool output, or model provenance as proof.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from email.parser import Parser
from pathlib import Path
from typing import Any

import tomllib
from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name

FORBIDDEN_PATH_PARTS = {
    ".agents",
    ".ai-codex",
    ".audit_probe",
    ".claude",
    ".claire",
    ".codex",
    ".omc",
    ".pytest-tmp-root",
    ".svelte-kit",
    ".vite",
    ".vite-temp",
    "node_modules",
    "vetinari.egg-info",
}
FORBIDDEN_PREFIXES = (
    "ui/",
    "vetinari/../ui/",
)
FORBIDDEN_SUFFIXES = (
    ".map",
    ".safetensors",
    ".gguf",
    ".bin",
    ".onnx",
    ".pt",
    ".pth",
    ".ckpt",
    ".dll",
    ".dylib",
    ".node",
    ".pyd",
    ".so",
    ".svelte",
    ".wasm",
)
COORDINATION_ONLY_PARENTS = {
    "SESSION-29F.md",
    "SESSION-34D.md",
    "SESSION-34F.md",
    "SESSION-34G.md",
    "SESSION-34I.md",
}
CFG_DFG_CATEGORIES = {
    "bare-except",
    "deep-nesting",
    "exception-swallow",
    "high-complexity",
    "missing-return",
    "mutable-default",
    "parse-error",
    "shadowed-variable",
    "unreachable-code",
    "unused-import",
    "unused-variable",
}


@dataclass(frozen=True)
class CertificationFailure:
    """A single release certification failure."""

    check: str
    message: str
    path: str | None = None


@dataclass(frozen=True)
class CertificationResult:
    """Collection of release certification failures."""

    failures: tuple[CertificationFailure, ...]

    @property
    def ok(self) -> bool:
        """Return True when no certification failures were found."""
        return not self.failures


def _failure(check: str, message: str, path: str | Path | None = None) -> CertificationFailure:
    return CertificationFailure(check=check, message=message, path=str(path) if path is not None else None)


def _combine(*results: CertificationResult) -> CertificationResult:
    failures: list[CertificationFailure] = []
    for result in results:
        failures.extend(result.failures)
    return CertificationResult(tuple(failures))


def _normalise_manifest_path(value: str) -> str:
    return value.strip().replace("\\", "/")


def _path_parts(value: str) -> set[str]:
    return {part for part in value.split("/") if part}


def _is_absolute_or_parent_relative(value: str) -> bool:
    return value.startswith("/") or bool(re.match(r"^[A-Za-z]:/", value)) or ".." in _path_parts(value)


def _normalised_requirement(requirement: str) -> str:
    requirement_without_marker = requirement.split(";", 1)[0].strip()
    try:
        parsed = Requirement(requirement_without_marker)
    except InvalidRequirement:
        return re.sub(r"\s+", "", requirement_without_marker).replace("_", "-").lower()
    extras = f"[{','.join(sorted(canonicalize_name(extra) for extra in parsed.extras))}]" if parsed.extras else ""
    url = f"@{parsed.url}" if parsed.url else ""
    return f"{canonicalize_name(parsed.name)}{extras}{parsed.specifier}{url}"


def _read_text(path: Path, failures: list[CertificationFailure], check: str) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        failures.append(_failure(check, f"could not read file: {exc}", path))
        return ""


def _load_pyproject(path: Path, failures: list[CertificationFailure]) -> dict[str, Any]:
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        failures.append(_failure("metadata-parity", f"could not parse pyproject.toml: {exc}", path))
        return {}


def certify_release_manifest(entries: Iterable[str], root: Path | None = None) -> CertificationResult:
    """Reject release manifests that include stale, malformed, or forbidden paths."""
    failures: list[CertificationFailure] = []
    seen: set[str] = set()
    for raw_entry in entries:
        entry = _normalise_manifest_path(raw_entry)
        if not entry:
            continue
        if entry in seen:
            failures.append(_failure("release-manifest", f"duplicate manifest entry: {entry}"))
        seen.add(entry)
        if "\x00" in entry or "\r" in entry or "\n" in entry:
            failures.append(_failure("release-manifest", f"malformed manifest token: {entry!r}"))
            continue
        if _is_absolute_or_parent_relative(entry):
            failures.append(
                _failure("release-manifest", f"parent-relative or absolute path is not releasable: {entry}")
            )
        lower = entry.lower()
        parts = _path_parts(lower)
        if any(part in FORBIDDEN_PATH_PARTS for part in parts):
            failures.append(_failure("release-manifest", f"forbidden workspace path in release manifest: {entry}"))
        if any(lower.startswith(prefix) for prefix in FORBIDDEN_PREFIXES):
            failures.append(
                _failure("release-manifest", f"frontend workspace path is outside release boundary: {entry}")
            )
        if lower.endswith(FORBIDDEN_SUFFIXES):
            failures.append(
                _failure("release-manifest", f"forbidden generated/binary/model artifact in release manifest: {entry}")
            )
        if root is not None and not (root / entry).exists():
            failures.append(
                _failure("release-manifest", f"manifest entry points to missing file: {entry}", root / entry)
            )
    return CertificationResult(tuple(failures))


def _certify_setup_py(source: str, path: Path) -> CertificationResult:
    failures: list[CertificationFailure] = []
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return CertificationResult((_failure("setup-py", f"setup.py is not parseable: {exc}", path),))
    setup_calls = [
        node for node in ast.walk(tree) if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "setup"
    ]
    if len(setup_calls) != 1:
        failures.append(_failure("setup-py", f"expected exactly one setup() call, found {len(setup_calls)}", path))
    elif setup_calls[0].keywords:
        names = ", ".join(keyword.arg or "<dynamic>" for keyword in setup_calls[0].keywords)
        failures.append(
            _failure("setup-py", f"setup.py must delegate package metadata to pyproject.toml, found: {names}", path)
        )
    if "Programming Language :: Python :: 3.14" in source:
        failures.append(_failure("setup-py", "setup.py claims Python 3.14 support without release proof", path))
    return CertificationResult(tuple(failures))


def certify_package_metadata(root: Path) -> CertificationResult:
    """Check pyproject/setup.py/PKG-INFO/SOURCES.txt parity and release boundary."""
    failures: list[CertificationFailure] = []
    pyproject_path = root / "pyproject.toml"
    setup_path = root / "setup.py"
    pkg_info_path = root / "vetinari.egg-info" / "PKG-INFO"
    sources_path = root / "vetinari.egg-info" / "SOURCES.txt"

    required_files = (pyproject_path, setup_path, pkg_info_path, sources_path)
    for path in required_files:
        if not path.exists():
            failures.append(_failure("metadata-parity", "required packaging metadata file is missing", path))
    if failures:
        return CertificationResult(tuple(failures))

    pyproject = _load_pyproject(pyproject_path, failures)
    setup_source = _read_text(setup_path, failures, "setup-py")
    pkg_info_text = _read_text(pkg_info_path, failures, "metadata-parity")
    sources_text = _read_text(sources_path, failures, "release-manifest")
    if failures:
        return CertificationResult(tuple(failures))

    project = pyproject.get("project", {}) if isinstance(pyproject, dict) else {}
    metadata = Parser().parsestr(pkg_info_text)

    failures.extend(_certify_setup_py(setup_source, setup_path).failures)

    pyproject_name = project.get("name")
    if pyproject_name and metadata.get("Name") != pyproject_name:
        failures.append(
            _failure(
                "metadata-parity",
                f"PKG-INFO Name {metadata.get('Name')!r} does not match pyproject name {pyproject_name!r}",
                pkg_info_path,
            )
        )
    pyproject_description = project.get("description")
    if pyproject_description and metadata.get("Summary") != pyproject_description:
        failures.append(
            _failure("metadata-parity", "PKG-INFO Summary does not match pyproject description", pkg_info_path)
        )

    pyproject_classifiers = set(project.get("classifiers") or [])
    pkg_classifiers = set(metadata.get_all("Classifier") or [])
    if "Programming Language :: Python :: 3.14" in pyproject_classifiers | pkg_classifiers:
        failures.append(_failure("metadata-parity", "Python 3.14 classifier requires explicit tested-release proof"))
    if pyproject_classifiers != pkg_classifiers:
        failures.append(
            _failure("metadata-parity", "PKG-INFO classifiers do not match pyproject classifiers", pkg_info_path)
        )

    pyproject_deps = {_normalised_requirement(req) for req in project.get("dependencies", [])}
    pkg_deps = {
        _normalised_requirement(req) for req in metadata.get_all("Requires-Dist") or [] if "extra ==" not in req
    }
    if pyproject_deps != pkg_deps:
        missing = sorted(pyproject_deps - pkg_deps)
        extra = sorted(pkg_deps - pyproject_deps)
        failures.append(
            _failure(
                "metadata-parity",
                f"PKG-INFO requirements disagree with pyproject dependencies; missing={missing} extra={extra}",
                pkg_info_path,
            )
        )

    source_lines = [line.strip() for line in sources_text.splitlines() if line.strip()]
    failures.extend(certify_release_manifest(source_lines, root=root).failures)
    return CertificationResult(tuple(failures))


def certify_model_provenance_source(source: str, path: str | Path | None = None) -> CertificationResult:
    """Reject source patterns that overclaim immutable model/download provenance."""
    failures: list[CertificationFailure] = []
    if re.search(r"revision\s*=\s*['\"](?:main|master|latest)['\"]", source):
        failures.append(
            _failure("model-provenance", "mutable model/download revision is not immutable provenance", path)
        )
    if re.search(r"SHA-?256", source, re.IGNORECASE) and "print(" in source:
        has_enforcement = re.search(
            r"(expected_(?:sha256|hash)|compare_digest|==\s*(?:digest|sha256)|raise\s+.*hash)", source
        )
        if not has_enforcement:
            failures.append(
                _failure("model-provenance", "display-only hash output is not integrity verification", path)
            )
    lines = source.splitlines()
    for index, line in enumerate(lines):
        if not re.search(r"\bif\s+\w+\.exists\(\):", line):
            continue
        base_indent = len(line) - len(line.lstrip())
        body: list[str] = []
        for following in lines[index + 1 :]:
            if not following.strip():
                continue
            indent = len(following) - len(following.lstrip())
            if indent <= base_indent:
                break
            body.append(following.strip())
        body_text = "\n".join(body)
        if "return 0" in body_text and not re.search(r"(sha256|checksum|digest|hash)", body_text, re.IGNORECASE):
            failures.append(
                _failure("model-provenance", "existing-file trust must verify identity and integrity", path)
            )
            break
    if ".stem" in source and "gguf" in source.lower():
        failures.append(_failure("model-provenance", "stem-only GGUF identity is not release provenance", path))
    return CertificationResult(tuple(failures))


def certify_release_evidence(payload: Mapping[str, Any]) -> CertificationResult:
    """Reject false-green release evidence payloads."""
    failures: list[CertificationFailure] = []
    status = str(payload.get("status") or payload.get("outcome") or "").lower()
    successful = status in {"clean", "ok", "pass", "passed", "success"}
    release_evidence = bool(payload.get("release_evidence") or payload.get("used_as_release_evidence") or successful)

    tools_run = payload.get("tools_run", payload.get("tool_count"))
    if successful and (tools_run == 0 or tools_run == []):
        failures.append(_failure("release-evidence", "zero-tool project scan cannot be clean release evidence"))

    coverage = payload.get("coverage", payload)
    if isinstance(coverage, Mapping) and "files" in coverage and not coverage.get("files") and successful:
        failures.append(
            _failure("release-evidence", "coverage report with no file data cannot be clean release evidence")
        )

    tests = payload.get("tests", payload.get("test_summary", {}))
    if isinstance(tests, Mapping):
        total = int(tests.get("total", 0) or 0)
        skipped = int(tests.get("skipped", 0) or 0)
        if successful and total > 0 and skipped >= total:
            failures.append(_failure("release-evidence", "all-skipped test suite cannot be release evidence"))

    if release_evidence and payload.get("continue_on_error") is True:
        failures.append(_failure("release-evidence", "continue-on-error CI evidence is advisory, not release proof"))
    if release_evidence and int(payload.get("xfail_count", 0) or 0) > 0:
        failures.append(_failure("release-evidence", "xfail-bearing suites cannot certify release behavior"))
    return CertificationResult(tuple(failures))


def _string_field(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    return value.strip() if isinstance(value, str) else ""


def certify_supply_chain_release_guard(payload: Mapping[str, Any]) -> CertificationResult:
    """Reject release artifact evidence without supply-chain proof."""
    failures = list(certify_release_evidence(payload).failures)
    artifact = payload.get("artifact")
    if not isinstance(artifact, Mapping):
        failures.append(_failure("supply-chain-release", "artifact metadata must be a JSON object"))
        artifact = {}

    artifact_path = _string_field(artifact, "path")
    if artifact_path:
        failures.extend(certify_release_manifest([artifact_path]).failures)
    else:
        failures.append(_failure("supply-chain-release", "artifact.path is required"))

    sha256 = _string_field(artifact, "sha256")
    if not re.fullmatch(r"[0-9a-fA-F]{64}", sha256):
        failures.append(_failure("supply-chain-release", "artifact.sha256 must be a 64-character hex digest"))

    for key in ("provenance_ref", "sbom_ref", "license_evidence_ref", "builder_command"):
        if not _string_field(artifact, key):
            failures.append(_failure("supply-chain-release", f"artifact.{key} is required"))

    signature = artifact.get("signature")
    if not isinstance(signature, Mapping):
        failures.append(_failure("supply-chain-release", "artifact.signature must be a JSON object"))
    else:
        signer = _string_field(signature, "signer")
        signature_ref = _string_field(signature, "signature_ref") or _string_field(signature, "sigstore_bundle_ref")
        if not signer:
            failures.append(_failure("supply-chain-release", "artifact.signature.signer is required"))
        if not signature_ref:
            failures.append(
                _failure("supply-chain-release", "artifact.signature signature_ref or sigstore_bundle_ref is required")
            )

    return CertificationResult(tuple(failures))


def certify_plan_index(index_text: str, required_children: Iterable[str] = ()) -> CertificationResult:
    """Reject generated route/session indexes that omit children or route to parents."""
    failures: list[CertificationFailure] = []
    for child in required_children:
        if child not in index_text:
            failures.append(_failure("plan-index", f"generated index omits child shard: {child}"))
    for line in index_text.splitlines():
        lowered = line.lower()
        if "coordination-only" in lowered or "historical" in lowered:
            continue
        for parent in COORDINATION_ONLY_PARENTS:
            if parent in line and re.search(r"\b(owner|route|routes|execute|session|proof)\b", lowered):
                failures.append(
                    _failure("plan-index", f"release evidence routes to coordination-only parent: {parent}")
                )
    return CertificationResult(tuple(failures))


def certify_analysis_router_tools(tools: Iterable[str]) -> CertificationResult:
    """Ensure generated CFG/DFG guidance is executable or explicitly advisory."""
    failures: list[CertificationFailure] = []
    for tool in tools:
        if "cfg_dfg_analysis.py" not in tool:
            continue
        lowered = tool.lower()
        if "non-executable guidance" in lowered:
            continue
        tokens = tool.replace("\\", "/").split()
        command_indexes = [
            idx for idx, token in enumerate(tokens) if token in {"analyze", "analyze-dir", "summary", "check"}
        ]
        if not command_indexes:
            failures.append(
                _failure("analysis-router", f"CFG/DFG tool guidance lacks a cfg_dfg_analysis subcommand: {tool}")
            )
            continue
        command_index = command_indexes[0]
        command = tokens[command_index]
        if command in {"analyze", "check"} and (
            command_index + 1 >= len(tokens) or tokens[command_index + 1].startswith("--")
        ):
            failures.append(_failure("analysis-router", f"CFG/DFG {command} guidance lacks path argument: {tool}"))
        if "--category" in tokens:
            category_index = tokens.index("--category") + 1
            if category_index >= len(tokens):
                failures.append(_failure("analysis-router", f"CFG/DFG category flag lacks a value: {tool}"))
                continue
            category = tokens[category_index]
            if "," in category or category not in CFG_DFG_CATEGORIES:
                failures.append(_failure("analysis-router", f"CFG/DFG category is not parser-valid: {category}"))
    return CertificationResult(tuple(failures))


def _print_result(result: CertificationResult) -> int:
    if result.ok:
        print("Release certification OK.")
        return 0
    for failure in result.failures:
        location = f" [{failure.path}]" if failure.path else ""
        print(f"{failure.check}: {failure.message}{location}", file=sys.stderr)
    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Fail-closed release evidence certifier")
    sub = parser.add_subparsers(dest="command", required=True)

    package = sub.add_parser("package-metadata", help="Check package metadata parity and SOURCES.txt boundary")
    package.add_argument("--root", type=Path, default=Path.cwd())

    manifest = sub.add_parser("release-manifest", help="Check a newline-delimited release manifest")
    manifest.add_argument("path", type=Path)
    manifest.add_argument("--root", type=Path)

    evidence = sub.add_parser("evidence-json", help="Check a JSON evidence payload")
    evidence.add_argument("path", type=Path)

    supply_chain = sub.add_parser("supply-chain-json", help="Check release artifact supply-chain evidence")
    supply_chain.add_argument("path", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "package-metadata":
        return _print_result(certify_package_metadata(args.root))
    if args.command == "release-manifest":
        entries = args.path.read_text(encoding="utf-8").splitlines()
        return _print_result(certify_release_manifest(entries, root=args.root))
    if args.command == "evidence-json":
        try:
            payload = json.loads(args.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"evidence-json: could not read JSON payload: {exc}", file=sys.stderr)
            return 2
        if not isinstance(payload, Mapping):
            print("evidence-json: payload must be a JSON object", file=sys.stderr)
            return 2
        return _print_result(certify_release_evidence(payload))
    if args.command == "supply-chain-json":
        try:
            payload = json.loads(args.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"supply-chain-json: could not read JSON payload: {exc}", file=sys.stderr)
            return 2
        if not isinstance(payload, Mapping):
            print("supply-chain-json: payload must be a JSON object", file=sys.stderr)
            return 2
        return _print_result(certify_supply_chain_release_guard(payload))
    raise AssertionError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
