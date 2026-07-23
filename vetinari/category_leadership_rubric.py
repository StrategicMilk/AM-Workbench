"""Validation helpers for the AM Workbench category leadership rubric."""

from __future__ import annotations

from pathlib import Path

import yaml

REQUIRED_DIMENSIONS = {
    "time_to_value",
    "perceived_performance",
    "feature_parity",
    "discoverability",
    "vocabulary",
    "ecosystem_readiness",
    "compliance",
    "trust_verifiability",
    "anti_pattern_resistance",
}

REQUIRED_TIERS = {"baseline", "credible", "category_leading"}


def load_rubric(path: Path) -> dict:
    """Load the category leadership rubric as a fail-closed YAML mapping.

    Args:
        path: Rubric YAML path.

    Returns:
        Parsed rubric mapping.

    Raises:
        AssertionError: If the path is missing, corrupt, or not a mapping.
    """
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise AssertionError(f"rubric missing: {path}") from exc
    except yaml.YAMLError as exc:
        raise AssertionError(f"rubric corrupt YAML: {path}: {exc}") from exc
    assert isinstance(payload, dict), "rubric must be a YAML mapping"
    return payload


def validate_rubric(payload: dict) -> list[str]:
    """Return validation errors for category leadership rubric coverage.

    Args:
        payload: Parsed rubric mapping.

    Returns:
        Validation error strings. An empty list means the rubric is complete.
    """
    errors: list[str] = []
    if payload.get("schema_version") != 1:
        errors.append("schema_version must be 1")
    if payload.get("category") != "local-first AI workstation":
        errors.append("category must be canonical")
    tiers = payload.get("tiers")
    if not isinstance(tiers, dict) or set(tiers) != REQUIRED_TIERS:
        errors.append("tiers must define baseline, credible, and category_leading")
    dimensions = payload.get("dimensions")
    if not isinstance(dimensions, dict):
        return [*errors, "dimensions must be a mapping"]
    missing_dimensions = REQUIRED_DIMENSIONS - set(dimensions)
    if missing_dimensions:
        errors.append(f"missing dimensions: {sorted(missing_dimensions)}")
    for name, dimension in dimensions.items():
        if not isinstance(dimension, dict):
            errors.append(f"{name} must be a mapping")
            continue
        if not str(dimension.get("question") or "").strip():
            errors.append(f"{name} missing question")
        procedure = dimension.get("measurement_procedure")
        if not isinstance(procedure, list) or not procedure or not all(str(item).strip() for item in procedure):
            errors.append(f"{name} missing measurement procedure")
        thresholds = dimension.get("tier_thresholds")
        if not isinstance(thresholds, dict):
            errors.append(f"{name} missing tier thresholds")
            continue
        errors.extend(
            f"{name} missing {tier} threshold" for tier in REQUIRED_TIERS if not str(thresholds.get(tier) or "").strip()
        )
    return errors
