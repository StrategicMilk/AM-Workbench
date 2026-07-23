"""Fail-closed supply-chain integrity checks for release and runtime inputs.

The checker groups five rule families: artifact pinning, license attestation,
runtime resolution, path portability, and config parity. Error-severity
violations make the report fail; missing optional inputs are reported as
warnings so a clean checkout can still be scanned. AST-based checks are limited
to literal values and cannot prove dynamically composed IDs such as f-strings.
"""

from __future__ import annotations

import ast
import logging
import re
from pathlib import Path

from .integrity_common import (
    IntegrityReport,
    IntegrityRule,
    IntegrityViolation,
    _annotation_is_optional_str,
    _call_name,
    _field_value_is_enforced,
    _iter_string_values,
    _load_toml,
    _load_yaml,
    _optional_missing,
    _parse_python,
    _read_text,
    _target_name,
)
from .integrity_path_config_rules import ConfigParityRule, PathPortabilityRule

logger = logging.getLogger(__name__)

PROJECT_ROOT: Path = Path(__file__).parent.parent.parent

SHA_40_RE = re.compile(r"@[0-9a-f]{40}$")
DIGEST_RE = re.compile(r"@sha256:[0-9a-f]{64}")
HF_MODEL_ID_RE = re.compile(r"[\w.-]+/[\w.-]+")
MUTABLE_SEMVER_RE = re.compile(r"^v?\d+\.\d+(\.\d+)?$")


class ArtifactPinRule(IntegrityRule):
    """Detect mutable artifact references in workflow, hook, image, and crate inputs."""

    rule = "artifact_pin"

    def check(self, project_root: Path) -> list[IntegrityViolation]:
        """Return mutable artifact reference violations.

        Returns:
            Violations for mutable workflow, hook, image, and crate references.
        """
        violations: list[IntegrityViolation] = []
        workflows = project_root / ".github" / "workflows"
        for workflow in sorted([*workflows.glob("*.yml"), *workflows.glob("*.yaml")]) if workflows.exists() else []:
            data, found = _load_yaml(workflow, self.rule)
            violations.extend(found)
            for site, value in _iter_string_values(data):
                if site.endswith(".uses") and not SHA_40_RE.search(value) and not DIGEST_RE.search(value):
                    violations.append(
                        IntegrityViolation(self.rule, f"{workflow}:{site}", f"unpinned action ref '{value}'", "error")
                    )

        pre_commit = project_root / ".pre-commit-config.yaml"
        if pre_commit.exists():
            data, found = _load_yaml(pre_commit, self.rule)
            violations.extend(found)
        else:
            data = None
        if isinstance(data, dict):
            for repo in data.get("repos", []):
                if not isinstance(repo, dict):
                    continue
                repo_url = repo.get("repo")
                rev = repo.get("rev")
                if (
                    isinstance(repo_url, str)
                    and repo_url not in {"local", "meta"}
                    and isinstance(rev, str)
                    and MUTABLE_SEMVER_RE.match(rev)
                    and not re.fullmatch(r"[0-9a-f]{40}", rev)
                ):
                    violations.append(
                        IntegrityViolation(
                            self.rule,
                            f"{pre_commit}:{repo_url}",
                            f"mutable pre-commit rev '{rev}'",
                            "warning",
                        )
                    )

        dockerfile = project_root / "deploy" / "Dockerfile"
        if dockerfile.exists():
            for line_no, line in enumerate(_read_text(dockerfile).splitlines(), start=1):
                stripped = line.strip()
                if stripped.upper().startswith("FROM ") and "@sha256:" not in stripped:
                    violations.append(
                        IntegrityViolation(
                            self.rule, f"{dockerfile}:{line_no}", "Docker base image is not digest-pinned", "error"
                        )
                    )

        for cargo_toml in sorted((project_root / "crates").glob("*/Cargo.toml")):
            cargo, found = _load_toml(cargo_toml, self.rule)
            violations.extend(found)
            package = cargo.get("package", {}) if cargo else {}
            if not isinstance(package, dict):
                continue
            version = str(package.get("version", ""))
            publish = package.get("publish")
            major = _major_version(version)
            if major is not None and major < 1 and publish is not False:
                violations.append(
                    IntegrityViolation(
                        self.rule,
                        str(cargo_toml),
                        f"pre-1.0 crate '{version}' is publishable",
                        "warning",
                    )
                )
        return violations


def _major_version(version: str) -> int | None:
    match = re.match(r"^(\d+)\.", version)
    return int(match.group(1)) if match else None


class LicenseAttestationRule(IntegrityRule):
    """Detect license metadata drift and blocked default model choices."""

    rule = "license_attestation"

    def check(self, project_root: Path) -> list[IntegrityViolation]:
        """Return dependency license and package metadata violations.

        Returns:
            Violations for missing or unsafe dependency license metadata.
        """
        violations: list[IntegrityViolation] = []
        pyproject, found = _load_toml(project_root / "pyproject.toml", self.rule)
        violations.extend(found)
        project = pyproject.get("project", {}) if pyproject else {}
        declared_license = _license_text(project.get("license") if isinstance(project, dict) else None)
        notice = project_root / "NOTICE"
        notice_text = _read_text(notice) if notice.exists() else ""
        if not notice.exists():
            violations.append(_optional_missing(self.rule, notice))
        if "Apache" in notice_text and declared_license == "MIT":
            violations.append(
                IntegrityViolation(self.rule, "NOTICE", "NOTICE/LICENSE mismatch (Apache vs MIT)", "error")
            )

        module, _, found = _parse_python(
            project_root / "vetinari" / "workbench" / "model_registry_contracts.py", self.rule
        )
        violations.extend(found)
        if module is not None and _model_card_license_is_unenforced(module):
            violations.append(
                IntegrityViolation(
                    self.rule,
                    "ModelCard.license_spdx",
                    "license_spdx is optional with no enforcement",
                    "error",
                )
            )

        defaults, found = _load_yaml(project_root / "config" / "agent_model_defaults.yaml", self.rule)
        violations.extend(found)
        if isinstance(defaults, dict):
            violations.extend(self._blocked_default_model_violations(defaults))
        return violations

    def _blocked_default_model_violations(self, defaults: dict[object, object]) -> list[IntegrityViolation]:
        violations: list[IntegrityViolation] = []
        role_defaults = defaults.get("defaults") if isinstance(defaults.get("defaults"), dict) else defaults
        for role, role_config in role_defaults.items():
            if not isinstance(role_config, dict):
                continue
            review = role_config.get("release_license_review")
            modes = role_config.get("modes")
            if not isinstance(review, dict) or not isinstance(modes, dict):
                continue
            blocked = {
                str(model_id)
                for model_id, data in review.items()
                if isinstance(data, dict) and data.get("status") == "blocked"
            }
            for mode, value in modes.items():
                model_id = _model_id_from_config(value)
                if model_id in blocked:
                    violations.append(
                        IntegrityViolation(
                            self.rule,
                            f"{role}.modes.{mode}",
                            f"build/mode default selects a license-blocked model '{model_id}'",
                            "error",
                        )
                    )
        return violations


def _license_text(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        text = value.get("text") or value.get("file")
        return str(text) if text is not None else ""
    return ""


def _model_id_from_config(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("model", "model_id", "id", "default"):
            candidate = value.get(key)
            if isinstance(candidate, str):
                return candidate
    return ""


def _model_card_license_is_unenforced(module: ast.Module) -> bool:
    for node in module.body:
        if isinstance(node, ast.ClassDef) and node.name == "ModelCard":
            for item in node.body:
                if isinstance(item, ast.AnnAssign) and _target_name(item.target) == "license_spdx":
                    return _annotation_is_optional_str(item.annotation) and not _field_value_is_enforced(item.value)
    return False


class RuntimeResolutionRule(IntegrityRule):
    """Detect unresolved or mutable runtime dependency and model selection inputs."""

    rule = "runtime_resolution"

    def check(self, project_root: Path) -> list[IntegrityViolation]:
        """Return runtime package and model-resolution violations.

        Returns:
            Violations for mutable runtime package or model resolution.
        """
        violations: list[IntegrityViolation] = []
        module, _, found = _parse_python(project_root / "vetinari" / "training" / "data_seeder.py", self.rule)
        violations.extend(found)
        if module is not None:
            violations.extend(self._load_dataset_without_revision(module, "data_seeder.py"))

        module, _, found = _parse_python(
            project_root / "vetinari" / "training" / "external_data.py", self.rule, optional=True
        )
        violations.extend(found)
        if module is not None:
            violations.extend(self._training_allowed_without_revision(module, "external_data.py"))

        module, text, found = _parse_python(
            project_root / "vetinari" / "inference" / "embedder.py", self.rule, optional=True
        )
        violations.extend(found)
        if module is not None:
            violations.extend(self._literal_hf_model_assignments(module, text, "embedder.py"))

        module, _, found = _parse_python(
            project_root / "vetinari" / "models" / "model_pool.py", self.rule, optional=True
        )
        violations.extend(found)
        if module is not None:
            violations.extend(
                IntegrityViolation(
                    self.rule,
                    f"model_pool.py:{getattr(node, 'lineno', '?')}",
                    "latest literal in model version field",
                    "error",
                )
                for node in ast.walk(module)
                if isinstance(node, ast.Constant) and isinstance(node.value, str) and "latest" in node.value.lower()
            )

        module, text, found = _parse_python(project_root / "vetinari" / "training" / "pipeline_core.py", self.rule)
        violations.extend(found)
        if module is not None and "_PINNED_PACKAGE_SPECS" not in text:
            violations.extend(
                IntegrityViolation(
                    self.rule,
                    "pipeline_core.py:_ensure_packages",
                    "unpinned runtime pip install",
                    "warning",
                )
                for node in ast.walk(module)
                if (
                    isinstance(node, ast.FunctionDef)
                    and node.name == "_ensure_packages"
                    and any(
                        isinstance(child, ast.Call) and _call_name(child.func) == "subprocess.run"
                        for child in ast.walk(node)
                    )
                )
            )
        return violations

    def _load_dataset_without_revision(self, module: ast.Module, filename: str) -> list[IntegrityViolation]:
        return [
            IntegrityViolation(
                self.rule,
                f"{filename}:{node.lineno}",
                "load_dataset without revision pin",
                "error",
            )
            for node in ast.walk(module)
            if (
                isinstance(node, ast.Call)
                and _call_name(node.func) in {"load_dataset", "datasets.load_dataset"}
                and not any(keyword.arg == "revision" for keyword in node.keywords)
            )
        ]

    def _training_allowed_without_revision(self, module: ast.Module, filename: str) -> list[IntegrityViolation]:
        violations: list[IntegrityViolation] = []
        for node in ast.walk(module):
            if not isinstance(node, ast.Call) or _call_name(node.func) != "DatasetSpec":
                continue
            revision_none = False
            has_default_training_flag = False
            for keyword in node.keywords:
                if (
                    keyword.arg == "revision"
                    and isinstance(keyword.value, ast.Constant)
                    and keyword.value.value is None
                ):
                    revision_none = True
                if (
                    keyword.arg == "default_training_allowed"
                    and isinstance(keyword.value, ast.Constant)
                    and keyword.value.value is True
                ):
                    has_default_training_flag = bool(keyword.value.value)
            if revision_none and has_default_training_flag:
                violations.append(
                    IntegrityViolation(
                        self.rule,
                        f"{filename}:{node.lineno}",
                        "DatasetSpec allows training with revision=None",
                        "error",
                    )
                )
        return violations

    def _literal_hf_model_assignments(self, module: ast.Module, text: str, filename: str) -> list[IntegrityViolation]:
        violations: list[IntegrityViolation] = []
        for node in ast.walk(module):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            value = node.value
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            target_text = ",".join(_target_name(target).lower() for target in targets)
            source = ast.get_source_segment(text, node) or ""
            if "registry" in target_text or "registry" in source.lower():
                continue
            if (
                isinstance(value, ast.Constant)
                and isinstance(value.value, str)
                and HF_MODEL_ID_RE.fullmatch(value.value)
            ):
                violations.append(
                    IntegrityViolation(
                        self.rule,
                        f"{filename}:{node.lineno}",
                        f"hardcoded model id '{value.value}' without pin-registry lookup",
                        "warning",
                    )
                )
        return violations


def check_all(project_root: Path | None = None) -> IntegrityReport:
    """Run every integrity rule and convert rule failures into violations.

    Returns:
        Aggregated integrity report for the supplied project root.
    """
    root = project_root or PROJECT_ROOT
    rules: list[IntegrityRule] = [
        ArtifactPinRule(),
        LicenseAttestationRule(),
        RuntimeResolutionRule(),
        PathPortabilityRule(),
        ConfigParityRule(),
    ]
    violations: list[IntegrityViolation] = []
    checked = 0
    for rule in rules:
        try:
            violations.extend(rule.check(root))
            checked += 1
        except Exception as exc:
            logger.exception("supply-chain integrity rule failed: %s", rule.__class__.__name__)
            violations.append(
                IntegrityViolation(
                    "rule_load_failure",
                    rule.__class__.__name__,
                    f"rule raised: {exc!r}",
                    "error",
                )
            )
    return IntegrityReport(tuple(violations), checked)
