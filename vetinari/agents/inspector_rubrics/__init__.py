"""Per-kind Inspector rubric loader."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from vetinari.boundary_guards import require_nonempty
from vetinari.types import ShardKind

_RUBRIC_DIR = Path(__file__).parent


def validate_rubric(rubric: dict[str, Any]) -> bool:
    """Validate ad-hoc Inspector rubric payloads used by tests and tooling.

    Returns:
        ``True`` when required rubric fields are non-empty.
    """
    criteria = rubric.get("criteria", [])
    allowed_values = rubric.get("allowed_values", [])
    require_nonempty(" ".join(str(item) for item in criteria), field_name="criteria")
    require_nonempty(" ".join(str(item) for item in allowed_values), field_name="allowed_values")
    return True


def load_rubric(kind: str | ShardKind) -> dict[str, Any]:
    """Load and validate the Inspector rubric for a shard kind.

    Args:
        kind: Shard kind value such as ``"standard"`` or
            :class:`vetinari.types.ShardKind`.

    Returns:
        Parsed YAML rubric dictionary.

    Raises:
        ValueError: If ``kind`` is unknown or the YAML shape is malformed.
    """
    try:
        shard_kind = ShardKind(kind)
    except ValueError as exc:
        raise ValueError(f"unknown Inspector rubric kind: {kind!r}") from exc

    path = _RUBRIC_DIR / f"{shard_kind.value}.yaml"
    if not path.exists():
        raise ValueError(f"missing Inspector rubric file for kind: {shard_kind.value}")

    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"malformed Inspector rubric {path.name}: root must be a mapping")
    if data.get("kind") != shard_kind.value:
        raise ValueError(f"malformed Inspector rubric {path.name}: kind does not match filename")
    if not isinstance(data.get("grading_criteria"), list):
        raise ValueError(f"malformed Inspector rubric {path.name}: grading_criteria must be a list")
    if "rejection_policy" not in data:
        raise ValueError(f"malformed Inspector rubric {path.name}: missing rejection_policy")

    for criterion in data["grading_criteria"]:
        if not isinstance(criterion, dict):
            raise ValueError(f"malformed Inspector rubric {path.name}: criteria must be mappings")
        has_key = "evidence_key" in criterion
        has_any = "evidence_key_any_of" in criterion
        if has_key == has_any:
            raise ValueError(
                f"malformed Inspector rubric {path.name}: criterion {criterion.get('id')!r} "
                "must set exactly one of evidence_key or evidence_key_any_of"
            )
    return data


__all__ = ["load_rubric", "validate_rubric"]
