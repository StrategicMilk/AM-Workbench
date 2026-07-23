#!/usr/bin/env python3
"""Fail closed when release-facing package versions diverge."""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import tomllib

ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Finding:
    path: Path
    message: str

    def format(self) -> str:
        return f"{self.path}: {self.message}"


def _python_version(root: Path) -> str | None:
    text = (root / "vetinari" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r"^__version__\s*=\s*['\"]([^'\"]+)['\"]", text, re.MULTILINE)
    return match.group(1) if match else None


def _load_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        data = json.load(handle)
    return data if isinstance(data, dict) else {}


def run_checks(root: Path) -> list[Finding]:
    findings: list[Finding] = []
    expected = _python_version(root)
    if not expected:
        return [Finding(root / "vetinari" / "__init__.py", "could not determine Python package __version__")]

    tauri_path = root / "src-tauri" / "tauri.conf.json"
    tauri_version = str(_load_json(tauri_path).get("version", ""))
    if tauri_version != expected:
        findings.append(Finding(tauri_path, f"Tauri bundle version {tauri_version!r} must match {expected!r}"))

    cargo_path = root / "src-tauri" / "Cargo.toml"
    cargo_version = str(tomllib.loads(cargo_path.read_text(encoding="utf-8")).get("package", {}).get("version", ""))
    if cargo_version != expected:
        findings.append(Finding(cargo_path, f"Tauri Cargo package version {cargo_version!r} must match {expected!r}"))

    package_path = root / "ui" / "svelte" / "package.json"
    ui_version = str(_load_json(package_path).get("version", ""))
    if ui_version != expected:
        findings.append(Finding(package_path, f"Svelte package version {ui_version!r} must match {expected!r}"))
    package_lock_path = root / "ui" / "svelte" / "package-lock.json"
    package_lock = _load_json(package_lock_path)
    lock_versions = {
        "$.version": package_lock.get("version"),
        "$.packages[''].version": (package_lock.get("packages") or {}).get("", {}).get("version")
        if isinstance(package_lock.get("packages"), dict)
        else None,
    }
    for field, version in lock_versions.items():
        if str(version) != expected:
            findings.append(Finding(package_lock_path, f"Svelte lockfile {field} {version!r} must match {expected!r}"))
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args(argv)

    findings = run_checks(args.root.resolve())
    if findings:
        print(f"Release version alignment check failed with {len(findings)} finding(s):")
        for finding in findings:
            print(f"- {finding.format()}")
        return 1
    print("Release version alignment check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
