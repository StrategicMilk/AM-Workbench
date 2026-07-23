"""Catalog-backed lint engine for artifact reviews."""

from __future__ import annotations

import re
import threading
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from vetinari.constants import PROJECT_ROOT

from .runtime import ArtifactReviewLintFinding, LintSeverity, RiskTag

_LINT_CATALOG_CONFIG_PATH: Path = PROJECT_ROOT / "config" / "workbench" / "artifact_lint.yaml"
_LINT_CATALOG_LOCK: threading.Lock = threading.Lock()
_LINT_CATALOG_CACHE: tuple[LintRule, ...] | None = None
_STRUCTURE_CHECKS = {
    "links_resolve_to_known_targets",
    "provenance_has_source_pair",
    "images_have_alt_text",
    "dependency_revisions_are_known",
    "at_least_one_evidence_ref",
}
_FAMILY_RUNNERS = {
    "broken_links": "_lint_broken_links",
    "missing_provenance": "_lint_missing_provenance",
    "off_policy_claims": "_lint_off_policy_claims",
    "weak_accessibility_metadata": "_lint_weak_accessibility_metadata",
    "stale_references": "_lint_stale_references",
    "brand_style_violations": "_lint_brand_style_violations",
    "missing_evidence_anchors": "_lint_missing_evidence_anchors",
}


class LintCatalogError(Exception):
    """Raised when the artifact lint catalog is unreadable or unsafe."""


@dataclass(frozen=True, slots=True)
class LintRule:
    """One catalog lint rule."""

    rule_id: str
    family: str
    severity: LintSeverity
    applies_to_kinds: tuple[str, ...]
    risk_tags: tuple[RiskTag, ...]
    description: str
    pattern: str | None = None
    structure_check: str | None = None
    term_list: tuple[str, ...] = ()

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"LintRule(rule_id={self.rule_id!r}, family={self.family!r}, severity={self.severity!r})"


class ArtifactLintEngine:
    """Run configured lint families against a structured artifact."""

    def __init__(self, rules: tuple[LintRule, ...] | None = None) -> None:
        self._rules = rules

    def run(self, *, kind: str, artifact: Mapping[str, Any]) -> tuple[ArtifactReviewLintFinding, ...]:
        """Return deterministic findings for the artifact kind.

        Returns:
            tuple[ArtifactReviewLintFinding, ...] value produced by run().
        """
        rules = self._rules if self._rules is not None else load_lint_catalog()
        findings: list[ArtifactReviewLintFinding] = []
        for rule in rules:
            if kind not in rule.applies_to_kinds and "*" not in rule.applies_to_kinds:
                continue
            runner = globals()[_FAMILY_RUNNERS[rule.family]]
            findings.extend(runner(rule, artifact))
        return tuple(
            sorted(findings, key=lambda item: (item.severity.value, item.rule_id, item.location, item.message))
        )


def load_lint_catalog() -> tuple[LintRule, ...]:
    """Return the cached immutable lint catalog.

    Returns:
        Resolved lint catalog value.
    """
    global _LINT_CATALOG_CACHE
    if _LINT_CATALOG_CACHE is not None:
        return _LINT_CATALOG_CACHE
    with _LINT_CATALOG_LOCK:
        if _LINT_CATALOG_CACHE is None:
            _LINT_CATALOG_CACHE = _load_lint_catalog_uncached()
        return _LINT_CATALOG_CACHE


def _load_lint_catalog_uncached() -> tuple[LintRule, ...]:
    try:
        data = yaml.safe_load(_LINT_CATALOG_CONFIG_PATH.read_text(encoding="utf-8"))
    except OSError as exc:
        raise LintCatalogError("artifact lint catalog unreadable") from exc
    except yaml.YAMLError as exc:
        raise LintCatalogError("artifact lint catalog invalid YAML") from exc
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise LintCatalogError("artifact lint catalog schema_version must be 1")
    rows = data.get("rules")
    if not isinstance(rows, list):
        raise LintCatalogError("artifact lint catalog must contain a rules list")
    rules = tuple(_parse_rule(row) for row in rows)
    missing = set(_FAMILY_RUNNERS) - {rule.family for rule in rules}
    if missing:
        raise LintCatalogError(f"artifact lint catalog missing families: {sorted(missing)}")
    return rules


def _reset_lint_catalog_for_test() -> None:
    """Clear the cached catalog for isolated tests."""
    global _LINT_CATALOG_CACHE
    with _LINT_CATALOG_LOCK:
        _LINT_CATALOG_CACHE = None


def _parse_rule(row: Any) -> LintRule:
    if not isinstance(row, dict):
        raise LintCatalogError("artifact lint rule rows must be mappings")
    rule_id = _required_str(row, "rule_id")
    family = _required_str(row, "family")
    if family not in _FAMILY_RUNNERS:
        raise LintCatalogError(f"unknown lint family {family!r}")
    severity = _parse_severity(_required_str(row, "severity"))
    applies = _required_str_tuple(row, "applies_to_kinds")
    tags = tuple(_parse_risk_tag(value) for value in _required_str_tuple(row, "risk_tags"))
    description = _required_str(row, "description")
    pattern = row.get("pattern")
    structure_check = row.get("structure_check")
    term_list = tuple(str(value) for value in row.get("term_list", ()) if str(value))
    if pattern is not None and not isinstance(pattern, str):
        raise LintCatalogError(f"rule {rule_id!r} pattern must be a string")
    if pattern is not None:
        try:
            re.compile(pattern)
        except re.error as exc:
            raise LintCatalogError(f"rule {rule_id!r} pattern is invalid") from exc
    if structure_check is not None and (
        not isinstance(structure_check, str) or structure_check not in _STRUCTURE_CHECKS
    ):
        raise LintCatalogError(f"rule {rule_id!r} has unknown structure_check {structure_check!r}")
    if pattern is None and structure_check is None and not term_list:
        raise LintCatalogError(f"rule {rule_id!r} must define pattern, structure_check, or term_list")
    return LintRule(rule_id, family, severity, applies, tags, description, pattern, structure_check, term_list)


def _lint_broken_links(rule: LintRule, artifact: Mapping[str, Any]) -> tuple[ArtifactReviewLintFinding, ...]:
    if rule.structure_check == "links_resolve_to_known_targets":
        return _check_links_resolve_to_known_targets(rule, artifact)
    return ()


def _lint_missing_provenance(rule: LintRule, artifact: Mapping[str, Any]) -> tuple[ArtifactReviewLintFinding, ...]:
    findings = list(_check_provenance_has_source_pair(rule, artifact))
    recipe = artifact.get("promotion_recipe")
    if isinstance(recipe, Mapping) and recipe.get("reversible") is False:
        findings.append(
            _finding(rule, "promotion_recipe", "non-reversible promotion recipe requires provenance review")
        )
    return tuple(findings)


def _lint_off_policy_claims(rule: LintRule, artifact: Mapping[str, Any]) -> tuple[ArtifactReviewLintFinding, ...]:
    text = "\n".join(_iter_text(artifact)).lower()
    return tuple(
        _finding(rule, "text", f"off-policy term found: {term}") for term in rule.term_list if term.lower() in text
    )


def _lint_weak_accessibility_metadata(
    rule: LintRule, artifact: Mapping[str, Any]
) -> tuple[ArtifactReviewLintFinding, ...]:
    if rule.structure_check == "images_have_alt_text":
        return _check_images_have_alt_text(rule, artifact)
    return ()


def _lint_stale_references(rule: LintRule, artifact: Mapping[str, Any]) -> tuple[ArtifactReviewLintFinding, ...]:
    if rule.structure_check == "dependency_revisions_are_known":
        return _check_dependency_revisions_are_known(rule, artifact)
    return ()


def _lint_brand_style_violations(rule: LintRule, artifact: Mapping[str, Any]) -> tuple[ArtifactReviewLintFinding, ...]:
    if not rule.pattern:
        return ()
    pattern = re.compile(rule.pattern)
    text = "\n".join(_iter_text(artifact))
    return tuple(_finding(rule, "text", f"unapproved brand color {match.group(0)}") for match in pattern.finditer(text))


def _lint_missing_evidence_anchors(
    rule: LintRule, artifact: Mapping[str, Any]
) -> tuple[ArtifactReviewLintFinding, ...]:
    if rule.structure_check == "at_least_one_evidence_ref":
        return _check_at_least_one_evidence_ref(rule, artifact)
    return ()


def _check_links_resolve_to_known_targets(
    rule: LintRule, artifact: Mapping[str, Any]
) -> tuple[ArtifactReviewLintFinding, ...]:
    known = {str(value) for value in artifact.get("known_links", ())}
    links = [str(value) for value in artifact.get("links", ())]
    broken = [link for link in links if link not in known or "broken" in link.lower()]
    return tuple(_finding(rule, f"links[{index}]", f"broken link {link}") for index, link in enumerate(broken))


def _check_provenance_has_source_pair(
    rule: LintRule, artifact: Mapping[str, Any]
) -> tuple[ArtifactReviewLintFinding, ...]:
    provenance = artifact.get("provenance")
    source = ""
    if isinstance(provenance, Mapping):
        source = str(provenance.get("source", ""))
    elif isinstance(provenance, Iterable) and not isinstance(provenance, str | bytes):
        source = next((str(value) for key, value in provenance if key == "source"), "")
    if source.strip():
        canonical_hash = str(artifact.get("artifact_sha256", "")).strip()
        provenance_hash = ""
        if isinstance(provenance, Mapping):
            provenance_hash = str(provenance.get("artifact_sha256", "") or provenance.get("source_sha256", "")).strip()
        if provenance_hash and canonical_hash and provenance_hash != canonical_hash:
            return (
                _finding(
                    rule, "provenance.artifact_sha256", "provenance hash does not match canonical artifact_sha256"
                ),
            )
        return ()
    return (_finding(rule, "provenance", "missing provenance source"),)


def _check_images_have_alt_text(rule: LintRule, artifact: Mapping[str, Any]) -> tuple[ArtifactReviewLintFinding, ...]:
    images = artifact.get("images", ())
    if not isinstance(images, Iterable) or isinstance(images, str | bytes):
        return ()
    findings = []
    for index, image in enumerate(images):
        if not isinstance(image, Mapping) or not str(image.get("alt", "")).strip():
            findings.append(_finding(rule, f"images[{index}].alt", "image missing alt text"))
    return tuple(findings)


def _check_dependency_revisions_are_known(
    rule: LintRule, artifact: Mapping[str, Any]
) -> tuple[ArtifactReviewLintFinding, ...]:
    known = {str(value) for value in artifact.get("known_revisions", ())}
    dependencies = artifact.get("dependencies", ())
    if not isinstance(dependencies, Iterable) or isinstance(dependencies, str | bytes):
        return ()
    findings = []
    for index, dependency in enumerate(dependencies):
        revision = dependency.get("revision") if isinstance(dependency, Mapping) else dependency
        if str(revision) not in known:
            findings.append(_finding(rule, f"dependencies[{index}]", f"unknown revision {revision!r}"))
    return tuple(findings)


def _check_at_least_one_evidence_ref(
    rule: LintRule, artifact: Mapping[str, Any]
) -> tuple[ArtifactReviewLintFinding, ...]:
    refs = artifact.get("evidence_refs", ())
    if isinstance(refs, Iterable) and not isinstance(refs, str | bytes) and any(str(ref).strip() for ref in refs):
        return ()
    return (_finding(rule, "evidence_refs", "missing evidence anchor"),)


def _iter_text(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for child in value.values():
            yield from _iter_text(child)
    elif isinstance(value, Iterable) and not isinstance(value, bytes):
        for child in value:
            yield from _iter_text(child)


def _finding(rule: LintRule, location: str, message: str) -> ArtifactReviewLintFinding:
    return ArtifactReviewLintFinding(rule.rule_id, rule.severity, rule.risk_tags, message, location)


def _required_str(row: Mapping[str, Any], key: str) -> str:
    value = row.get(key)
    if not isinstance(value, str) or not value:
        raise LintCatalogError(f"lint rule missing {key}")
    return value


def _required_str_tuple(row: Mapping[str, Any], key: str) -> tuple[str, ...]:
    value = row.get(key)
    if not isinstance(value, list) or not value or any(not isinstance(item, str) or not item for item in value):
        raise LintCatalogError(f"lint rule missing {key}")
    return tuple(value)


def _parse_severity(value: str) -> LintSeverity:
    try:
        return LintSeverity[value]
    except KeyError as exc:
        raise LintCatalogError(f"unknown lint severity {value!r}") from exc


def _parse_risk_tag(value: str) -> RiskTag:
    try:
        return RiskTag[value]
    except KeyError as exc:
        raise LintCatalogError(f"unknown risk tag {value!r}") from exc


__all__ = ["ArtifactLintEngine", "LintCatalogError", "LintRule", "load_lint_catalog"]
