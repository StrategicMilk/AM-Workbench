#!/usr/bin/env python3
"""Verify the runtime supported-matrix provenance artifact exists and matches."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MATRIX = ROOT / "config" / "runtime" / "supported_matrix.yaml"
DEFAULT_ARTIFACT_ROOT = ROOT / "outputs" / "runtime"
SCHEMA_VERSION = "runtime-supported-matrix-verification.v1"


def _load_yaml(path: Path) -> dict[str, Any]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except OSError as exc:
        raise ValueError(f"matrix unreadable: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("matrix root must be a mapping")
    return data


def _load_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"artifact unreadable: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("artifact root must be a mapping")
    return data


def expected_artifact_path(matrix: dict[str, Any], artifact_root: Path) -> Path:
    dates = {
        str(row.get("verified_at") or "").strip()
        for row in matrix.get("components", [])
        if isinstance(row, dict) and str(row.get("verified_at") or "").strip()
    }
    if not dates:
        raise ValueError("matrix components must declare verified_at dates")
    latest = max(dates)
    return artifact_root / f"supported_matrix_verification_{latest}.json"


def _component_key(row: dict[str, Any]) -> str:
    component = str(row.get("component") or "").strip()
    if not component:
        raise ValueError("component row missing component")
    return component


def _display_path(path: Path) -> str:
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def validate_artifact(*, matrix_path: Path, artifact_root: Path) -> list[str]:
    matrix = _load_yaml(matrix_path)
    artifact_path = expected_artifact_path(matrix, artifact_root)
    artifact = _load_json(artifact_path)
    errors: list[str] = []
    if artifact.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"{artifact_path}: schema_version must be {SCHEMA_VERSION!r}")
    expected_matrix_path = _display_path(matrix_path)
    if artifact.get("matrix_path") != expected_matrix_path:
        errors.append(f"{artifact_path}: matrix_path does not point at {expected_matrix_path}")
    matrix_rows = {_component_key(row): row for row in matrix.get("components", []) if isinstance(row, dict)}
    artifact_rows = artifact.get("components")
    if not isinstance(artifact_rows, list):
        return [*errors, f"{artifact_path}: components must be a list"]
    artifact_by_component = {_component_key(row): row for row in artifact_rows if isinstance(row, dict)}
    missing = sorted(set(matrix_rows) - set(artifact_by_component))
    extra = sorted(set(artifact_by_component) - set(matrix_rows))
    if missing:
        errors.append(f"{artifact_path}: missing component proof rows: {', '.join(missing)}")
    if extra:
        errors.append(f"{artifact_path}: extra component proof rows: {', '.join(extra)}")
    for component, matrix_row in matrix_rows.items():
        artifact_row = artifact_by_component.get(component)
        if not isinstance(artifact_row, dict):
            continue
        for key in ("minimum_version", "known_bad_ranges", "required_compute_capability", "verified_at"):
            if artifact_row.get(key) != matrix_row.get(key):
                errors.append(f"{artifact_path}: {component}.{key} does not match matrix")
        matrix_sources = matrix_row.get("verified_sources")
        artifact_sources = artifact_row.get("verified_sources")
        if not isinstance(artifact_sources, list) or not artifact_sources:
            errors.append(f"{artifact_path}: {component}.verified_sources must be non-empty")
        elif artifact_sources != matrix_sources:
            errors.append(f"{artifact_path}: {component}.verified_sources does not match matrix")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--matrix", type=Path, default=DEFAULT_MATRIX)
    parser.add_argument("--artifact-root", type=Path, default=DEFAULT_ARTIFACT_ROOT)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    matrix_path = args.matrix if args.matrix.is_absolute() else ROOT / args.matrix
    artifact_root = args.artifact_root if args.artifact_root.is_absolute() else ROOT / args.artifact_root
    try:
        errors = validate_artifact(matrix_path=matrix_path, artifact_root=artifact_root)
    except ValueError as exc:
        errors = [str(exc)]
    payload = {"status": "pass" if not errors else "fail", "errors": errors}
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    elif errors:
        print("Runtime supported-matrix artifact check failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
    else:
        print("Runtime supported-matrix artifact check passed.")
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
