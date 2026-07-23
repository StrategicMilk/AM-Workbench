"""Validation helpers for AM Workbench reference-product catalogs."""

from __future__ import annotations

from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

from vetinari.privacy.envelope import privacy_receipt
from vetinari.security.ssrf_guard_ext import SSRFGuardError, validate_outbound_url

REQUIRED_AXES = {
    "ai_ml_workstation_products",
    "model_labs",
    "prompt_engineering_tools",
    "operator_uis",
}


class ReferenceProductValidationError(AssertionError):
    """Raised when the reference-product set cannot be trusted by audit lanes."""


def reference_product_set_privacy_receipt(catalog_id: str = "reference-products") -> dict[str, Any]:
    """Return the operational privacy receipt for reference-product evidence."""
    return privacy_receipt(
        privacy_class="operational",
        source="reference_product_set",
        erasure_token=f"reference_product_set:{catalog_id}",
        redaction_applied=False,
    )


def load_yaml(path: Path) -> dict[str, Any]:
    """Load YAML as a mapping or fail closed.

    Args:
        path: YAML file path.

    Returns:
        Parsed YAML mapping.

    Raises:
        ReferenceProductValidationError: If the file is missing, corrupt, or not a mapping.
    """
    if not path.exists():
        raise ReferenceProductValidationError(f"missing YAML: {path}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ReferenceProductValidationError(f"corrupt YAML: {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ReferenceProductValidationError(f"YAML root must be a mapping: {path}")
    return data


def require_text(value: Any, label: str) -> str:
    """Return non-empty text or raise a catalog validation error.

    Args:
        value: Candidate value.
        label: Human-readable field label.

    Returns:
        Non-empty string value.

    Raises:
        ReferenceProductValidationError: If the value is not non-empty text.
    """
    if not isinstance(value, str) or not value.strip():
        raise ReferenceProductValidationError(f"{label} must be non-empty text")
    return value


def require_https_url(value: Any, label: str) -> str:
    """Return an HTTPS URL or raise a catalog validation error.

    Args:
        value: Candidate URL value.
        label: Human-readable field label.

    Returns:
        Valid HTTPS URL.

    Raises:
        ReferenceProductValidationError: If the value is not a valid HTTPS URL.
    """
    url = require_text(value, label)
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ReferenceProductValidationError(f"{label} must be an https URL")
    try:
        validate_outbound_url(url, resolve_hostname=False)
    except SSRFGuardError as exc:
        raise ReferenceProductValidationError(f"{label} is not a safe public URL: {exc}") from exc
    return url


def parse_snapshot_date(value: Any, label: str, *, allow_future: bool = False, today: date | None = None) -> date:
    """Parse and validate a snapshot date.

    Args:
        value: Candidate ISO date value.
        label: Human-readable field label.
        allow_future: Whether future dates are valid for this field.
        today: Optional current date override for deterministic tests.

    Returns:
        Parsed date.

    Raises:
        ReferenceProductValidationError: If the value is missing, malformed, or disallowed.
    """
    text = require_text(value, label)
    try:
        parsed = date.fromisoformat(text)
    except ValueError as exc:
        raise ReferenceProductValidationError(f"{label} must be an ISO date") from exc
    current = today if today is not None else date.today()
    if not allow_future and parsed > current:
        raise ReferenceProductValidationError(f"{label} must not be in the future")
    return parsed


def category_dimensions(path: Path) -> set[str]:
    """Return category leadership dimensions from a rubric file.

    Args:
        path: Category leadership rubric path.

    Returns:
        Dimension identifiers.

    Raises:
        ReferenceProductValidationError: If the rubric is missing or malformed.
    """
    rubric = load_yaml(path)
    dimensions = rubric.get("dimensions")
    if not isinstance(dimensions, dict) or not dimensions:
        raise ReferenceProductValidationError("category rubric dimensions must be a non-empty mapping")
    return set(dimensions)


def validate_reference_products(data: dict[str, Any], dimensions: set[str]) -> None:
    """Validate reference-product catalog shape and coverage.

    Args:
        data: Parsed reference-product catalog.
        dimensions: Required category rubric dimensions.

    Raises:
        ReferenceProductValidationError: If any catalog contract is incomplete.
    """
    if data.get("catalog_id") != "reference-products":
        raise ReferenceProductValidationError("catalog_id must be reference-products")
    if data.get("category") != "local-first AI workstation":
        raise ReferenceProductValidationError("category must be canonical")
    parse_snapshot_date(data.get("snapshot_date"), "snapshot_date")
    parse_snapshot_date(data.get("stale_after"), "stale_after", allow_future=True)

    axes = data.get("axes")
    if not isinstance(axes, dict) or set(axes) != REQUIRED_AXES:
        raise ReferenceProductValidationError("axes must define the four required reference axes")

    declared_dimensions = data.get("rubric_dimensions")
    if not isinstance(declared_dimensions, list) or set(declared_dimensions) != dimensions:
        raise ReferenceProductValidationError("rubric_dimensions must mirror category leadership dimensions")

    products = data.get("products")
    if not isinstance(products, list) or not products:
        raise ReferenceProductValidationError("products must be a non-empty list")

    counts: Counter[str] = Counter()
    seen: set[tuple[str, str]] = set()
    for index, product in enumerate(products):
        if not isinstance(product, dict):
            raise ReferenceProductValidationError(f"products[{index}] must be a mapping")
        product_name = require_text(product.get("product_name"), f"products[{index}].product_name")
        axis = require_text(product.get("axis"), f"{product_name}.axis")
        if axis not in REQUIRED_AXES:
            raise ReferenceProductValidationError(f"{product_name}.axis is not a known reference axis")
        key = (axis, product_name.casefold())
        if key in seen:
            raise ReferenceProductValidationError(f"duplicate product in axis: {axis}/{product_name}")
        seen.add(key)
        counts[axis] += 1

        require_text(product.get("vendor"), f"{product_name}.vendor")
        require_https_url(product.get("evidence_url"), f"{product_name}.evidence_url")
        parse_snapshot_date(product.get("snapshot_date"), f"{product_name}.snapshot_date")
        require_text(product.get("rationale"), f"{product_name}.rationale")

        scores = product.get("scores")
        if not isinstance(scores, dict) or set(scores) != dimensions:
            raise ReferenceProductValidationError(f"{product_name}.scores must cover every rubric dimension")
        for dimension, score in scores.items():
            if score not in {1, 2, 3}:
                raise ReferenceProductValidationError(f"{product_name}.{dimension} score must be 1, 2, or 3")

    for axis, axis_data in axes.items():
        if not isinstance(axis_data, dict):
            raise ReferenceProductValidationError(f"{axis} axis metadata must be a mapping")
        minimum_entries = axis_data.get("minimum_entries")
        if not isinstance(minimum_entries, int) or minimum_entries < 10:
            raise ReferenceProductValidationError(f"{axis}.minimum_entries must be at least 10")
        if counts[axis] < minimum_entries:
            raise ReferenceProductValidationError(f"{axis} has too few reference products")
