"""Validate support-matrix schema and freshness before release or weekly CI."""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypedDict, cast

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MATRIX_PATH = REPO_ROOT / "config" / "support_matrix.yaml"
DEFAULT_MAX_AGE_DAYS = 90


class MatrixCell(TypedDict, total=False):
    """A single cell in the support matrix, covering one hardware/os/workflow triple.

    All keys are optional at the TypedDict level because cells are loaded from
    untrusted YAML; the validator enforces presence at runtime.
    """

    hardware: str
    os: str
    workflow: str
    maturity: str
    proof_command: str
    known_limitation: str
    last_verified: str


class MatrixSchemaError(ValueError):
    """Raised when the support matrix does not satisfy its deterministic schema."""


def load_matrix(path: Path) -> dict[str, Any]:
    """Load a support matrix YAML file.

    Args:
        path: Matrix YAML path.

    Returns:
        Parsed matrix mapping.

    Raises:
        MatrixSchemaError: If the file is missing or does not parse to a mapping.
    """
    if not path.exists():
        raise MatrixSchemaError(f"Support matrix missing: {path}")
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise MatrixSchemaError("Support matrix root must be a mapping")
    return loaded


def parse_iso_date(value: object) -> datetime:
    """Parse an ISO date or datetime as UTC.

    Args:
        value: ISO date or datetime value.

    Returns:
        Timezone-aware UTC datetime.

    Raises:
        MatrixSchemaError: If the value is missing or invalid.
    """
    if not isinstance(value, str) or not value.strip():
        raise MatrixSchemaError("last_verified must be a non-empty ISO date string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise MatrixSchemaError(f"last_verified is not ISO-formatted: {value}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _axis_values(matrix: Mapping[str, Any], name: str) -> set[str]:
    axes = matrix.get("axes")
    if not isinstance(axes, Mapping):
        raise MatrixSchemaError("axes must be a mapping")
    values = axes.get(name)
    if not isinstance(values, Sequence) or isinstance(values, str) or not values:
        raise MatrixSchemaError(f"axes.{name} must be a non-empty list")
    axis_set = {str(value) for value in values}
    if len(axis_set) != len(values):
        raise MatrixSchemaError(f"axes.{name} contains duplicate values")
    return axis_set


def _validate_cell(
    index: int,
    cell: MatrixCell,
    hardware_values: set[str],
    os_values: set[str],
    workflow_values: set[str],
    maturity_values: set[str],
) -> tuple[str, str, str]:
    """Validate one matrix cell and return its (hardware, os, workflow) key.

    Args:
        index: Zero-based cell index, used in error messages.
        cell: The cell mapping to validate.
        hardware_values: Allowed hardware axis values.
        os_values: Allowed OS axis values.
        workflow_values: Allowed workflow axis values.
        maturity_values: Allowed maturity axis values.

    Returns:
        Tuple of (hardware, os, workflow) strings for duplicate detection.

    Raises:
        MatrixSchemaError: If any cell field fails validation.
    """
    hardware = cell.get("hardware")
    os_name = cell.get("os")
    workflow = cell.get("workflow")
    maturity = cell.get("maturity")

    if hardware not in hardware_values:
        raise MatrixSchemaError(f"cell {index} has unknown hardware: {hardware}")
    if os_name not in os_values:
        raise MatrixSchemaError(f"cell {index} has unknown os: {os_name}")
    if workflow not in workflow_values:
        raise MatrixSchemaError(f"cell {index} has unknown workflow: {workflow}")
    if maturity not in maturity_values:
        raise MatrixSchemaError(f"cell {index} has unknown maturity: {maturity}")

    proof = cell.get("proof_command")
    limitation = cell.get("known_limitation")
    has_proof = isinstance(proof, str) and bool(proof.strip())
    has_limitation = isinstance(limitation, str) and bool(limitation.strip())
    if has_proof == has_limitation:
        raise MatrixSchemaError(f"cell {index} must have exactly one non-empty proof_command or known_limitation")
    if maturity == "promotion_eligible" and not has_proof:
        raise MatrixSchemaError(f"cell {index} is promotion_eligible without proof_command")

    return (str(hardware), str(os_name), str(workflow))


def validate_schema(matrix: Mapping[str, Any]) -> None:
    """Validate support matrix axes, cells, maturity values, and proof/limitation exclusivity.

    Args:
        matrix: Parsed support matrix.

    Raises:
        MatrixSchemaError: If any deterministic schema rule fails.
    """
    hardware_values = _axis_values(matrix, "hardware")
    os_values = _axis_values(matrix, "os")
    workflow_values = _axis_values(matrix, "workflow")
    maturity_values = _axis_values(matrix, "maturity")
    cells = matrix.get("cells")
    if not isinstance(cells, list) or not cells:
        raise MatrixSchemaError("cells must be a non-empty list")

    seen: set[tuple[str, str, str]] = set()
    for index, raw_cell in enumerate(cells):
        if not isinstance(raw_cell, Mapping):
            raise MatrixSchemaError(f"cell {index} must be a mapping")
        cell = cast(MatrixCell, raw_cell)
        key = _validate_cell(index, cell, hardware_values, os_values, workflow_values, maturity_values)
        if key in seen:
            raise MatrixSchemaError(f"duplicate cell for {key}")
        seen.add(key)

    expected = {
        (hardware, os_name, workflow)
        for hardware in hardware_values
        for os_name in os_values
        for workflow in workflow_values
    }
    missing = sorted(expected - seen)
    if missing:
        raise MatrixSchemaError(f"missing axis combinations: {missing[:5]}")


def matrix_age_days(matrix: Mapping[str, Any], *, now: datetime | None = None) -> int:
    """Return whole days since matrix-level verification.

    Args:
        matrix: Parsed support matrix.
        now: Optional current time for tests.

    Returns:
        Non-negative whole-day age.
    """
    verified_at = parse_iso_date(matrix.get("last_verified"))
    now_utc = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return max(0, (now_utc - verified_at).days)


def run_check(path: Path, *, max_age_days: int, check_only: bool = False) -> int:
    """Run schema and freshness checks.

    Args:
        path: Matrix YAML path.
        max_age_days: Maximum accepted matrix age.
        check_only: Suppress output for CI.

    Returns:
        Process exit code: 0 fresh, 1 stale, 2 schema error.
    """
    try:
        matrix = load_matrix(path)
        validate_schema(matrix)
        age_days = matrix_age_days(matrix)
    except MatrixSchemaError as exc:
        if not check_only:
            print(f"Support matrix schema error: {exc}")
        return 2

    if age_days > max_age_days:
        if not check_only:
            print(
                f"Support matrix last verified {age_days} days ago (threshold {max_age_days} days). "
                "Run the proof commands in config/support_matrix.yaml and update last_verified."
            )
        return 1
    if not check_only:
        print(f"Support matrix fresh: last verified {age_days} days ago")
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser.

    Returns:
        Configured parser.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--matrix", type=Path, default=DEFAULT_MATRIX_PATH, help="Path to support_matrix.yaml")
    parser.add_argument("--max-age-days", type=int, default=DEFAULT_MAX_AGE_DAYS, help="Freshness threshold in days")
    parser.add_argument("--check-only", action="store_true", help="Suppress output and return only an exit code")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point.

    Args:
        argv: Optional argument list for tests.

    Returns:
        Process exit code.
    """
    args = build_parser().parse_args(argv)
    return run_check(args.matrix, max_age_days=args.max_age_days, check_only=args.check_only)


if __name__ == "__main__":
    raise SystemExit(main())
