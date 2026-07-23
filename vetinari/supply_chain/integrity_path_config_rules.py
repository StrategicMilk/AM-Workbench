"""Path portability and config parity supply-chain rules."""

from __future__ import annotations

import ast
import re
from pathlib import Path

from .integrity_common import (
    ABSOLUTE_DEV_PATH_RE,
    IntegrityRule,
    IntegrityViolation,
    _iter_string_values,
    _load_json,
    _load_yaml,
    _optional_missing,
    _parse_python,
    _read_text,
    _target_name,
)


class PathPortabilityRule(IntegrityRule):
    """Detect developer-local paths and platform-specific imports in release inputs."""

    rule = "path_portability"

    def check(self, project_root: Path) -> list[IntegrityViolation]:
        """Return non-portable path and private-host reference violations.

        Returns:
            Violations for absolute local paths and cross-platform imports.
        """
        violations: list[IntegrityViolation] = []
        lanes, found = _load_yaml(project_root / "config" / "test_lanes.yaml", self.rule)
        violations.extend(found)
        violations.extend(self._absolute_paths(lanes, "config/test_lanes.yaml"))

        registry, found = _load_json(
            project_root / "config" / "full_spectrum_lane_scaffold_registry.json", self.rule, optional=True
        )
        violations.extend(found)
        violations.extend(self._absolute_paths(registry, "config/full_spectrum_lane_scaffold_registry.json"))

        path = project_root / "vetinari" / "testing" / "adversarial_tests.py"
        module, text, found = _parse_python(path, self.rule)
        violations.extend(found)
        if module is not None:
            violations.extend(_relative_default_results_dir_violations(module, text, self.rule))

        module, _, found = _parse_python(project_root / "installer" / "linux" / "build_appimage.py", self.rule)
        violations.extend(found)
        if module is not None:
            violations.extend(_linux_installer_cross_import_violations(module, self.rule))
        return violations

    def _absolute_paths(self, data: object, site: str) -> list[IntegrityViolation]:
        violations: list[IntegrityViolation] = []
        for path, value in _iter_string_values(data):
            if ABSOLUTE_DEV_PATH_RE.search(value):
                violations.append(
                    IntegrityViolation(self.rule, f"{site}:{path}", f"developer-local absolute path '{value}'", "error")
                )
        return violations


def _relative_default_results_dir_violations(module: ast.Module, text: str, rule: str) -> list[IntegrityViolation]:
    violations: list[IntegrityViolation] = []
    for node in ast.walk(module):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if "_DEFAULT_RESULTS_DIR" not in {_target_name(target) for target in targets}:
            continue
        source = ast.get_source_segment(text, node) or ""
        if ("_PROJECT_ROOT" not in source and "__file__" not in source) and _contains_relative_path_literal(node.value):
            violations.append(
                IntegrityViolation(
                    rule,
                    "adversarial_tests.py:_DEFAULT_RESULTS_DIR",
                    "_DEFAULT_RESULTS_DIR is relative without project-root anchor",
                    "error",
                )
            )
    return violations


def _linux_installer_cross_import_violations(module: ast.Module, rule: str) -> list[IntegrityViolation]:
    violations: list[IntegrityViolation] = []
    for node in module.body:
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(("installer.windows", "msi")):
            violations.append(
                IntegrityViolation(
                    rule, f"build_appimage.py:{node.lineno}", "Linux installer cross-imports Windows module", "error"
                )
            )
        if isinstance(node, ast.Import):
            violations.extend(
                IntegrityViolation(
                    rule, f"build_appimage.py:{node.lineno}", "Linux installer cross-imports Windows module", "error"
                )
                for alias in node.names
                if alias.name.startswith(("installer.windows", "msi"))
            )
    return violations


def _contains_relative_path_literal(node: ast.AST | None) -> bool:
    if node is None:
        return False
    for child in ast.walk(node):
        if isinstance(child, ast.Constant) and isinstance(child.value, str):
            value = child.value
            if value and not Path(value).is_absolute():
                return True
    return False


class ConfigParityRule(IntegrityRule):
    """Detect drift between parity rubrics, backend pins, runbooks, and CDN assets."""

    rule = "config_parity"

    def check(self, project_root: Path) -> list[IntegrityViolation]:
        """Return drift between runtime config and parity metadata.

        Returns:
            Violations for backend parity, importer pinning, runbook drift, and CDN SRI gaps.
        """
        violations: list[IntegrityViolation] = []
        rubric, found = _load_yaml(project_root / "config" / "backend_parity_rubric.yaml", self.rule)
        violations.extend(found)
        pins, found = _load_yaml(project_root / "config" / "backend_pins.yaml", self.rule)
        violations.extend(found)
        violations.extend(
            IntegrityViolation(
                self.rule,
                f"backend_pins.yaml:{backend}",
                "backend from parity rubric is absent from backend pins",
                "error",
            )
            for backend in _backend_names(rubric)
            if not _pin_entry_present(pins, backend)
        )

        importers, found = _load_yaml(
            project_root / "config" / "workbench" / "benchmark_importers.yaml", self.rule, optional=True
        )
        violations.extend(found)
        violations.extend(_benchmark_importer_violations(importers))

        runbook = project_root / "docs" / "runbooks" / "end-to-end-workflow.md"
        if runbook.exists():
            text = _read_text(runbook)
            violations.extend(
                IntegrityViolation(self.rule, str(runbook), f"stale backend tier in runbook: {backend}", "warning")
                for backend in ("vLLM", "SGLang", "NIM")
                if backend in text
            )
        else:
            violations.append(_optional_missing(self.rule, runbook))

        index = project_root / "ui" / "templates" / "index.html"
        if index.exists():
            violations.extend(_cdn_sri_violations(index, self.rule))
        else:
            violations.append(_optional_missing(self.rule, index))
        return violations


def _cdn_sri_violations(index: Path, rule: str) -> list[IntegrityViolation]:
    violations: list[IntegrityViolation] = []
    lines = _read_text(index).splitlines()
    for index_no, line in enumerate(lines):
        if not (
            re.search(r"<script\b[^>]*src=[\"']https://", line)
            or re.search(r"<link\b[^>]*rel=[\"']stylesheet[\"'][^>]*href=[\"']https://", line)
        ):
            continue
        adjacent = line + (lines[index_no + 1] if index_no + 1 < len(lines) else "")
        if "integrity=" not in adjacent:
            violations.append(
                IntegrityViolation(
                    rule, f"{index}:{index_no + 1}", "CDN asset is missing SRI integrity attribute", "error"
                )
            )
    return violations


def _backend_names(rubric: object) -> set[str]:
    if not isinstance(rubric, dict):
        return set()
    if "backends" in rubric:
        candidates = rubric["backends"]
    elif "first_class_backends" in rubric:
        candidates = rubric["first_class_backends"]
    else:
        candidates = rubric
    if isinstance(candidates, dict):
        return {str(key) for key in candidates}
    if isinstance(candidates, list):
        return _backend_names_from_list(candidates)
    return set()


def _backend_names_from_list(candidates: list[object]) -> set[str]:
    names: set[str] = set()
    for item in candidates:
        if isinstance(item, str):
            names.add(item)
        elif isinstance(item, dict):
            for key in ("backend", "name", "id"):
                value = item.get(key)
                if isinstance(value, str):
                    names.add(value)
    return names


def _benchmark_importer_violations(importers: object) -> list[IntegrityViolation]:
    if not isinstance(importers, dict):
        return []
    providers = importers.get("providers")
    if not isinstance(providers, dict):
        return []
    violations: list[IntegrityViolation] = []
    required = {"source_url", "version_pin", "integrity_sha256"}
    for name, provider in providers.items():
        if not isinstance(provider, dict) or provider.get("status") == "pending_pin":
            continue
        missing = sorted(required.difference(provider))
        if missing:
            violations.append(
                IntegrityViolation(
                    "config_parity",
                    f"benchmark_importers.yaml:{name}",
                    f"benchmark importer missing pin metadata: {', '.join(missing)}",
                    "error",
                )
            )
    return violations


def _pin_entry_present(pins: object, backend: str) -> bool:
    if not isinstance(pins, dict):
        return False
    candidates = pins.get("backends", pins)
    if not isinstance(candidates, dict):
        return False
    entry = candidates.get(backend)
    if entry is None:
        return False
    if isinstance(entry, dict):
        return not (entry.get("parity_rubric_declared") is not True and entry.get("status") == "pending_pin")
    return True
