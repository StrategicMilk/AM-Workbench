#!/usr/bin/env python3
"""Fail closed on RustSec findings reachable from one workspace package."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections import Counter, deque
from pathlib import Path
from typing import Any

COMMAND_TIMEOUT_SECONDS = 300


def _run_json(command: list[str], *, accepted_exit_codes: set[int]) -> dict[str, Any]:
    """Run *command* and parse its JSON stdout, preserving diagnostic stderr."""
    result = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=COMMAND_TIMEOUT_SECONDS,
    )
    if result.returncode not in accepted_exit_codes:
        detail = result.stderr.strip() or result.stdout.strip() or "no diagnostic output"
        raise RuntimeError(f"{' '.join(command)} exited {result.returncode}: {detail}")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{' '.join(command)} did not emit valid JSON") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"{' '.join(command)} emitted a non-object JSON payload")
    return payload


def reachable_package_versions(metadata: dict[str, Any], package_name: str) -> set[tuple[str, str]]:
    """Return dependency identities reachable from a workspace package.

    Args:
        metadata: Cargo metadata version-one JSON object.
        package_name: Unique workspace package whose non-development closure is required.

    Returns:
        Reachable ``(name, version)`` package identities, including the root.

    Raises:
        ValueError: If the metadata graph or package identity is malformed or ambiguous.
    """
    package_rows = metadata.get("packages")
    if not isinstance(package_rows, list):
        raise ValueError("cargo metadata packages is not an array")
    packages: dict[str, dict[str, Any]] = {}
    for index, row in enumerate(package_rows):
        if not isinstance(row, dict):
            raise ValueError(f"cargo metadata packages[{index}] is not an object")
        package_id = row.get("id")
        if not isinstance(package_id, str) or not package_id:
            raise ValueError(f"cargo metadata packages[{index}] has no package id")
        if package_id in packages:
            raise ValueError(f"cargo metadata contains duplicate package id {package_id!r}")
        if not all(isinstance(row.get(field), str) and row[field] for field in ("name", "version")):
            raise ValueError(f"cargo metadata package {package_id!r} has an incomplete identity")
        packages[package_id] = row

    workspace_rows = metadata.get("workspace_members")
    if not isinstance(workspace_rows, list) or not all(isinstance(value, str) and value for value in workspace_rows):
        raise ValueError("cargo metadata workspace_members is not a string array")
    if len(workspace_rows) != len(set(workspace_rows)):
        raise ValueError("cargo metadata workspace_members contains duplicate package ids")
    workspace_members = set(workspace_rows)
    unknown_workspace_members = sorted(workspace_members - set(packages))
    if unknown_workspace_members:
        raise ValueError(
            f"cargo metadata workspace_members references unknown package ids: {unknown_workspace_members[:3]}"
        )
    roots = [
        package_id
        for package_id, row in packages.items()
        if package_id in workspace_members and row.get("name") == package_name
    ]
    if len(roots) != 1:
        raise ValueError(f"expected one workspace package named {package_name!r}, found {len(roots)}")

    resolve = metadata.get("resolve")
    if not isinstance(resolve, dict):
        raise ValueError("cargo metadata did not include a resolve graph")
    node_rows = resolve.get("nodes")
    if not isinstance(node_rows, list):
        raise ValueError("cargo metadata resolve.nodes is not an array")
    nodes: dict[str, dict[str, Any]] = {}
    for index, row in enumerate(node_rows):
        if not isinstance(row, dict):
            raise ValueError(f"resolve.nodes[{index}] is not an object")
        package_id = row.get("id")
        if not isinstance(package_id, str) or not package_id:
            raise ValueError(f"resolve.nodes[{index}] has no package id")
        if package_id in nodes:
            raise ValueError(f"resolve graph contains duplicate node {package_id!r}")
        dependency_rows = row.get("deps")
        if not isinstance(dependency_rows, list):
            raise ValueError(f"resolve node {package_id!r} has malformed deps")
        for dependency_index, dependency in enumerate(dependency_rows):
            if not isinstance(dependency, dict):
                raise ValueError(f"resolve node {package_id!r} deps[{dependency_index}] is not an object")
            dependency_id = dependency.get("pkg")
            if not isinstance(dependency_id, str) or not dependency_id:
                raise ValueError(f"resolve node {package_id!r} deps[{dependency_index}] has no package id")
            dependency_name = dependency.get("name")
            if not isinstance(dependency_name, str) or not dependency_name:
                raise ValueError(f"resolve node {package_id!r} deps[{dependency_index}] has no dependency name")
            dependency_kinds = dependency.get("dep_kinds")
            if not isinstance(dependency_kinds, list) or not dependency_kinds:
                raise ValueError(f"resolve node {package_id!r} deps[{dependency_index}] has malformed dep_kinds")
            for kind_index, kind in enumerate(dependency_kinds):
                if not isinstance(kind, dict):
                    raise ValueError(
                        f"resolve node {package_id!r} deps[{dependency_index}].dep_kinds[{kind_index}] is not an object"
                    )
                if "kind" not in kind or kind["kind"] not in {None, "normal", "build", "dev"}:
                    raise ValueError(
                        f"resolve node {package_id!r} deps[{dependency_index}].dep_kinds[{kind_index}] "
                        "has an invalid kind"
                    )
                if "target" not in kind or not (kind["target"] is None or isinstance(kind["target"], str)):
                    raise ValueError(
                        f"resolve node {package_id!r} deps[{dependency_index}].dep_kinds[{kind_index}] "
                        "has an invalid target"
                    )
        dependencies = row.get("dependencies")
        if not isinstance(dependencies, list) or not all(isinstance(value, str) and value for value in dependencies):
            raise ValueError(f"resolve node {package_id!r} has malformed dependencies")
        nodes[package_id] = row

    package_ids = set(packages)
    node_ids = set(nodes)
    missing_nodes = sorted(package_ids - node_ids)
    unknown_nodes = sorted(node_ids - package_ids)
    if missing_nodes or unknown_nodes:
        raise ValueError(
            "resolve graph package/node identities disagree: "
            f"missing_nodes={missing_nodes[:3]}, unknown_nodes={unknown_nodes[:3]}"
        )
    resolve_root = resolve.get("root")
    if resolve_root is not None and (not isinstance(resolve_root, str) or resolve_root not in package_ids):
        raise ValueError("cargo metadata resolve.root references an unknown package id")
    for package_id, node in nodes.items():
        deps = [dependency["pkg"] for dependency in node["deps"]]
        dependencies = node["dependencies"]
        unknown_references = sorted((set(deps) | set(dependencies)) - package_ids)
        if unknown_references:
            raise ValueError(f"resolve node {package_id!r} references unknown package ids: {unknown_references[:3]}")
        if Counter(deps) != Counter(dependencies):
            raise ValueError(f"resolve node {package_id!r} deps and dependencies disagree")

    reached: set[str] = set()
    pending = deque(roots)
    while pending:
        package_id = pending.popleft()
        if package_id in reached:
            continue
        reached.add(package_id)
        node = nodes.get(package_id)
        if not isinstance(node, dict):
            raise ValueError(f"resolve graph has no node for package id {package_id!r}")
        dependency_ids = []
        for row in node["deps"]:
            dependency_kinds = row["dep_kinds"]
            if dependency_kinds and all(kind["kind"] == "dev" for kind in dependency_kinds):
                continue
            dependency_ids.append(row["pkg"])
        pending.extend(dependency_id for dependency_id in dependency_ids if dependency_id not in reached)

    return {(str(packages[package_id]["name"]), str(packages[package_id]["version"])) for package_id in reached}


def scoped_rustsec_findings(audit_report: dict[str, Any], reachable: set[tuple[str, str]]) -> list[dict[str, str]]:
    """Return normalized RustSec findings whose package is reachable.

    Args:
        audit_report: Cargo-audit JSON report.
        reachable: Dependency identities allowed into the scoped result.

    Returns:
        Stable, sorted vulnerability and warning records for reachable packages.

    Raises:
        ValueError: If the audit report does not satisfy the required schema.
    """
    candidates: list[tuple[str, dict[str, Any]]] = []
    vulnerabilities = audit_report.get("vulnerabilities")
    if not isinstance(vulnerabilities, dict) or not isinstance(vulnerabilities.get("list"), list):
        raise ValueError("cargo audit JSON has no vulnerabilities.list array")
    for row in vulnerabilities["list"]:
        if not isinstance(row, dict):
            raise ValueError("cargo audit vulnerabilities.list contains a non-object row")
        candidates.append(("vulnerability", row))

    warnings = audit_report.get("warnings")
    if not isinstance(warnings, dict):
        raise ValueError("cargo audit JSON has no warnings object")
    for warning_kind, rows in warnings.items():
        if not isinstance(rows, list):
            raise ValueError(f"cargo audit warnings.{warning_kind} is not an array")
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError(f"cargo audit warnings.{warning_kind} contains a non-object row")
            candidates.append((str(warning_kind), row))

    findings: list[dict[str, str]] = []
    for finding_kind, row in candidates:
        package = row.get("package")
        if not isinstance(package, dict):
            raise ValueError(f"cargo audit {finding_kind} row has no package object")
        identity = (str(package.get("name", "")), str(package.get("version", "")))
        if not all(identity):
            raise ValueError(f"cargo audit {finding_kind} row has an incomplete package identity")
        if identity not in reachable:
            continue
        advisory = row.get("advisory") if isinstance(row.get("advisory"), dict) else {}
        findings.append({
            "kind": finding_kind,
            "id": str(advisory.get("id") or row.get("id") or "unknown"),
            "package": identity[0],
            "version": identity[1],
            "title": str(advisory.get("title") or row.get("title") or "RustSec finding"),
        })
    return sorted(findings, key=lambda row: (row["kind"], row["package"], row["version"], row["id"]))


def build_parser() -> argparse.ArgumentParser:
    """Build the package-scoped RustSec checker CLI parser.

    Returns:
        Configured command-line parser.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", required=True, help="Workspace package whose dependency closure is release-gated")
    parser.add_argument("--cargo", default="cargo", help="Cargo executable")
    parser.add_argument("--output", type=Path, help="Optional JSON report path")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run cargo metadata and cargo-audit for one dependency closure.

    Args:
        argv: Optional command-line arguments.

    Returns:
        Zero when clean, one for findings, or two for malformed/unavailable evidence.
    """
    args = build_parser().parse_args(argv)
    try:
        metadata = _run_json(
            [args.cargo, "metadata", "--format-version", "1", "--locked", "--all-features"],
            accepted_exit_codes={0},
        )
        audit_report = _run_json([args.cargo, "audit", "--json"], accepted_exit_codes={0, 1})
        reachable = reachable_package_versions(metadata, args.package)
        findings = scoped_rustsec_findings(audit_report, reachable)
    except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
        if args.output:
            report = {
                "schema_version": "rustsec-package-scope.v1",
                "package": args.package,
                "reachable_package_count": None,
                "finding_count": None,
                "findings": [],
                "status": "error",
                "error": str(exc),
            }
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        sys.stderr.write(f"package-scoped RustSec check failed closed: {exc}\n")
        return 2

    report = {
        "schema_version": "rustsec-package-scope.v1",
        "package": args.package,
        "reachable_package_count": len(reachable),
        "finding_count": len(findings),
        "findings": findings,
        "status": "blocked" if findings else "passed",
    }
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    sys.stdout.write(rendered)
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
