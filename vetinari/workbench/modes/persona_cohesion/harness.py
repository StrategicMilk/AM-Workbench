"""Persona-cohesion harness contracts loaded without import-time I/O."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1


class PersonaCohesionHarnessError(RuntimeError):
    """Raised when persona-cohesion harness inputs cannot be trusted."""

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class PersonaCohesionCase:
    """One persona-cohesion runtime case."""

    case_id: str
    persona_id: str
    prompt: str
    expected_traits: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.case_id.strip():
            raise PersonaCohesionHarnessError("case_id is required", reason="missing_case_id")
        if not self.persona_id.strip():
            raise PersonaCohesionHarnessError("persona_id is required", reason="missing_persona_id")
        if not self.prompt.strip():
            raise PersonaCohesionHarnessError("prompt is required", reason="missing_prompt")
        if not self.expected_traits or any(not trait.strip() for trait in self.expected_traits):
            raise PersonaCohesionHarnessError("expected_traits are required", reason="missing_traits")

    def __repr__(self) -> str:
        """Return a compact case summary for harness validation failures."""
        return (
            f"PersonaCohesionCase(case_id={self.case_id!r}, persona_id={self.persona_id!r}, "
            f"expected_traits={self.expected_traits!r})"
        )


def load_persona_cohesion_cases(path: str | Path) -> tuple[PersonaCohesionCase, ...]:
    """Load persona-cohesion cases only when the runtime harness is invoked.

    Returns:
        Persona-cohesion cases parsed from the runtime harness file.

    Raises:
        PersonaCohesionHarnessError: If the file is missing, unreadable, corrupt, or malformed.
    """
    case_path = Path(path)
    try:
        payload = json.loads(case_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise PersonaCohesionHarnessError("persona-cohesion case file is missing", reason="missing") from exc
    except OSError as exc:
        raise PersonaCohesionHarnessError("persona-cohesion case file is unreadable", reason="unreadable") from exc
    except json.JSONDecodeError as exc:
        raise PersonaCohesionHarnessError("persona-cohesion case file is corrupt", reason="corrupt") from exc

    if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION:
        raise PersonaCohesionHarnessError("persona-cohesion case file has wrong schema", reason="wrong_schema")
    raw_cases = payload.get("cases")
    if not isinstance(raw_cases, list) or not raw_cases:
        raise PersonaCohesionHarnessError("persona-cohesion case file has no cases", reason="empty")
    return tuple(_case_from_mapping(item) for item in raw_cases)


def _case_from_mapping(raw_case: dict[str, Any]) -> PersonaCohesionCase:
    if not isinstance(raw_case, dict):
        raise PersonaCohesionHarnessError("persona-cohesion case is malformed", reason="malformed_case")
    try:
        return PersonaCohesionCase(
            case_id=str(raw_case["case_id"]),
            persona_id=str(raw_case["persona_id"]),
            prompt=str(raw_case["prompt"]),
            expected_traits=tuple(str(value) for value in raw_case["expected_traits"]),
        )
    except (KeyError, TypeError) as exc:
        raise PersonaCohesionHarnessError("persona-cohesion case is malformed", reason="malformed_case") from exc


__all__ = [
    "SCHEMA_VERSION",
    "PersonaCohesionCase",
    "PersonaCohesionHarnessError",
    "load_persona_cohesion_cases",
]
