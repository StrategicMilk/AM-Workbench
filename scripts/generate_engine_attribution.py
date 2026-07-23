#!/usr/bin/env python3
"""Generate the exact Rust and Python dependency attribution shipped by AM Engine."""

from __future__ import annotations

import argparse
import json
import sys
from collections import deque
from pathlib import Path
from typing import Any

from packaging.markers import Marker, default_environment

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from vetinari.engine.release_contract import (
    CARGO_IDENTITY_COUNT,
    CARGO_IDENTITY_SHA256,
    CONVERTER_IDENTITY_COUNT_BY_PLATFORM,
    CONVERTER_IDENTITY_SHA256_BY_PLATFORM,
    dependency_identity_digest,
)

try:
    from scripts.check_converter_lock import DEFAULT_LICENSES, DEFAULT_LOCK, parse_converter_lock
    from scripts.generate_spdx_sbom import (
        ROOT,
        _cargo_spdx_id,
        resolve_cargo_dependency_graph,
        validate_cargo_dependency_evidence,
        validate_converter_dependency_evidence,
        validate_spdx_document,
    )
except ModuleNotFoundError as exc:
    if exc.name not in {"scripts", "scripts.check_converter_lock", "scripts.generate_spdx_sbom"}:
        raise
    from check_converter_lock import DEFAULT_LICENSES, DEFAULT_LOCK, parse_converter_lock  # type: ignore[no-redef]
    from generate_spdx_sbom import (  # type: ignore[no-redef]
        ROOT,
        _cargo_spdx_id,
        resolve_cargo_dependency_graph,
        validate_cargo_dependency_evidence,
        validate_converter_dependency_evidence,
        validate_spdx_document,
    )

DEFAULT_SPDX = ROOT / "spdx.json"
DEFAULT_OUTPUT = ROOT / "crates" / "amw-engine" / "ENGINE_THIRD_PARTY_LICENSES.md"
MARKER = "<!-- amw-engine-attribution:v1 -->"


class EngineAttributionError(ValueError):
    """Raised when release attribution cannot be derived from exact dependency evidence."""


def _load_spdx(path: Path) -> dict[str, Any]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EngineAttributionError(f"SPDX evidence is unreadable: {path}") from exc
    if not isinstance(document, dict):
        raise EngineAttributionError("SPDX evidence must be a JSON object")
    return document


def _engine_cargo_closure(root: Path) -> tuple[list[tuple[str, str, str, str]], list[str]]:
    graph = resolve_cargo_dependency_graph(root)
    engine_roots = [package_id for package_id in graph.roots if graph.packages[package_id].name == "amw-engine"]
    if len(engine_roots) != 1:
        raise EngineAttributionError("Cargo graph must contain exactly one amw-engine root")
    children: dict[str, set[str]] = {}
    for parent, dependency in graph.relationships:
        children.setdefault(parent, set()).add(dependency)
    reachable: set[str] = set()
    pending = deque(engine_roots)
    while pending:
        package_id = pending.popleft()
        for dependency in children.get(package_id, set()):
            if dependency not in reachable:
                reachable.add(dependency)
                pending.append(dependency)
    rows = [
        (
            graph.packages[package_id].name,
            graph.packages[package_id].version,
            graph.packages[package_id].license_expression,
            f"pkg:cargo/{graph.packages[package_id].name}@{graph.packages[package_id].version}",
        )
        for package_id in reachable
    ]
    spdx_ids = [_cargo_spdx_id(package_id, graph.packages[package_id]) for package_id in reachable]
    return sorted(rows, key=lambda row: (row[0].casefold(), row[1], row[3])), sorted(spdx_ids)


def build_engine_attribution(root: Path = ROOT) -> str:
    """Build a deterministic attribution ledger for every engine-bundled dependency.

    Args:
        root: Repository root containing canonical Cargo, converter, and SPDX evidence.

    Returns:
        Deterministic Markdown attribution ledger.

    Raises:
        EngineAttributionError: If any dependency graph or SPDX evidence is incomplete.
    """
    root = root.resolve()
    document = _load_spdx(root / "spdx.json")
    graph = resolve_cargo_dependency_graph(root)
    converter_packages = parse_converter_lock(
        root / DEFAULT_LOCK.relative_to(ROOT),
        root / DEFAULT_LICENSES.relative_to(ROOT),
    )
    errors = [
        *validate_spdx_document(document),
        *validate_cargo_dependency_evidence(document, graph),
        *validate_converter_dependency_evidence(document, converter_packages),
    ]
    if errors:
        raise EngineAttributionError("release dependency evidence is invalid: " + "; ".join(errors))
    cargo_rows, cargo_spdx_ids = _engine_cargo_closure(root)
    cargo_identities = {(name, version) for name, version, _license, _purl in cargo_rows}
    if (
        len(cargo_identities) != CARGO_IDENTITY_COUNT
        or dependency_identity_digest(cargo_identities) != CARGO_IDENTITY_SHA256
    ):
        raise EngineAttributionError("Cargo identity closure drifted from the runtime release contract")
    base_marker_environment = default_environment()
    platform_environments = {
        "windows": {
            **base_marker_environment,
            "sys_platform": "win32",
            "platform_system": "Windows",
            "platform_machine": "AMD64",
        },
        "linux": {
            **base_marker_environment,
            "sys_platform": "linux",
            "platform_system": "Linux",
            "platform_machine": "x86_64",
        },
    }
    for platform_name, marker_environment in platform_environments.items():
        identities = {
            (package.name, package.version)
            for package in converter_packages
            if package.marker is None or Marker(package.marker).evaluate(marker_environment)
        }
        if (
            len(identities) != CONVERTER_IDENTITY_COUNT_BY_PLATFORM[platform_name]
            or dependency_identity_digest(identities) != CONVERTER_IDENTITY_SHA256_BY_PLATFORM[platform_name]
        ):
            raise EngineAttributionError(
                f"{platform_name} converter identity closure drifted from the runtime release contract"
            )
    packages = document.get("packages", [])
    observed_ids = {str(package.get("SPDXID")) for package in packages if isinstance(package, dict)}
    missing_ids = sorted(set(cargo_spdx_ids) - observed_ids)
    if missing_ids:
        raise EngineAttributionError(f"engine Cargo closure is absent from SPDX: {', '.join(missing_ids)}")
    lines = [
        "# AM Engine Third-Party Dependency Attribution",
        "",
        MARKER,
        "",
        (
            "This file is generated from the exact locked dependency graphs used to build this archive. "
            "The corresponding package license and copyright texts are shipped under `ENGINE_LICENSES/`."
        ),
        "",
        "The complete upstream llama.cpp MIT license is shipped separately as `LICENSE.llama.cpp`.",
        "",
        "## Rust dependency closure",
        "",
        "| Crate | Version | SPDX license | Package URL |",
        "| --- | --- | --- | --- |",
    ]
    lines.extend(
        f"| `{name}` | `{version}` | `{license_text}` | `{purl}` |" for name, version, license_text, purl in cargo_rows
    )
    lines.extend([
        "",
        "## Converter Python dependency closure",
        "",
        "| Package | Version | Target marker | SPDX license | Package URL |",
        "| --- | --- | --- | --- | --- |",
    ])
    lines.extend(
        (
            f"| `{package.name}` | `{package.version}` | `{package.marker or 'all targets'}` | "
            f"`{package.license_expression}` | `pkg:pypi/{package.name}@{package.version}` |"
        )
        for package in converter_packages
    )
    lines.extend([
        "",
        f"Rust closure records: {len(cargo_rows)}",
        f"Converter lock records: {len(converter_packages)}",
        "",
    ])
    return "\n".join(lines)


def validate_engine_attribution(
    root: Path = ROOT,
    attribution_path: Path | None = None,
) -> list[str]:
    """Compare the committed engine attribution ledger to canonical evidence.

    Args:
        root: Repository root containing the committed ledger and dependency evidence.
        attribution_path: Optional ledger path for validating a staged copy.

    Returns:
        Stable validation errors; empty means the ledger is exact.
    """
    expected = build_engine_attribution(root)
    path = attribution_path or (root.resolve() / DEFAULT_OUTPUT.relative_to(ROOT))
    try:
        observed = path.read_text(encoding="utf-8")
    except OSError as exc:
        return [f"engine attribution ledger is unreadable: {exc}"]
    if observed != expected:
        return ["engine attribution ledger is stale or incomplete"]
    return []


def main(argv: list[str] | None = None) -> int:
    """Generate or validate the engine attribution ledger.

    Args:
        argv: Optional command-line arguments.

    Returns:
        Zero when generated or valid, one for a stale committed ledger.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    root = args.root.resolve()
    if args.check:
        errors = validate_engine_attribution(root)
        for error in errors:
            print(error)
        return int(bool(errors))
    output = root / DEFAULT_OUTPUT.relative_to(ROOT)
    output.write_text(build_engine_attribution(root), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
