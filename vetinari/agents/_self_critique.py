"""Foreman shard self-critique helpers."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

_RUNNABLE_COMMAND_HEADS = {
    "python",
    "py",
    "pytest",
    "ruff",
    "mypy",
    "rg",
    "grep",
    "git",
    "node",
    "npm",
    "pnpm",
    "yarn",
    "bash",
    "sh",
    "make",
    "uv",
    "pip",
}


def is_runnable_command(text: str) -> bool:
    """Return True when text begins with an allowed command executable.

    Returns:
        Whether the first shell token is in the local runnable-command allowlist.
    """
    stripped = text.strip().strip("`").strip('"').strip("'").strip()
    return bool(stripped) and stripped.split()[0].lower().rstrip(":") in _RUNNABLE_COMMAND_HEADS


ADR_RE = re.compile(r"\bADR-\d{4}\b")
FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.DOTALL)
PATH_TOKEN_RE = re.compile(r"`?([A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+)`?")
REFERENCE_SECTIONS = (
    "Wiring Proof",
    "Concurrency Contract",
    "Rule-Vocabulary Coverage",
    "State-Lock Contract",
)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_RUBRIC_PATH = _PROJECT_ROOT / "config" / "foreman_shard_critique.yaml"


@dataclass(frozen=True, slots=True)
class CritiqueResult:
    """Result of applying the Foreman shard critique rubric."""

    passed: bool
    kind: str
    failed_checks: list[str] = field(default_factory=list)
    attempt: int = 1

    def __repr__(self) -> str:
        return (
            "CritiqueResult("
            f"passed={self.passed!r}, kind={self.kind!r}, "
            f"failed_checks={self.failed_checks!r}, attempt={self.attempt!r})"
        )


class OperatorAttentionRequired(Exception):
    """Raised when Foreman must block dispatch after two critique failures.

    Callers raise this when a generated shard fails the same self-critique gate
    twice and needs an operator-facing WorkReceipt or equivalent attention
    signal. This exception is the dispatch-block signal and must not be
    swallowed by the Foreman dispatch path.
    """


def run_self_critique(shard_path: str | Path, attempt: int = 1) -> CritiqueResult:
    """Evaluate a generated shard file against the configured per-kind rubric.

    Args:
        shard_path: Path to the generated shard markdown file.
        attempt: One-based critique attempt number for telemetry.

    Returns:
        Critique result containing pass/fail status and failed check ids.
    """
    path = Path(shard_path)
    text = path.read_text(encoding="utf-8")
    frontmatter, body = _split_frontmatter(text)
    kind = str(frontmatter.get("kind") or "standard")
    rubric_map = _load_rubric()
    rubric = rubric_map.get(kind) or rubric_map["standard"]
    failed_checks: list[str] = []
    context = _ShardContext(path=path, frontmatter=frontmatter, body=body, full_text=text)
    for check_id in rubric.get("checks", []):
        check = _CHECKS.get(str(check_id))
        if check is None or not check(context, rubric):
            failed_checks.append(str(check_id))
    return CritiqueResult(
        passed=not failed_checks,
        kind=kind,
        failed_checks=failed_checks,
        attempt=attempt,
    )


@dataclass(frozen=True, slots=True)
class _ShardContext:
    path: Path
    frontmatter: dict[str, Any]
    body: str
    full_text: str

    def __repr__(self) -> str:
        return (
            "_ShardContext("
            f"path={self.path!s}, kind={self.frontmatter.get('kind')!r}, "
            f"body_chars={len(self.body)}, full_text_chars={len(self.full_text)})"
        )


def _load_rubric(path: Path | str | None = None) -> dict[str, Any]:
    rubric_path = Path(path) if path is not None else DEFAULT_RUBRIC_PATH
    with rubric_path.open("r", encoding="utf-8") as handle:
        rubric = yaml.safe_load(handle) or {}
    _validate_rubric_against_checks(rubric, rubric_path)
    return rubric


def _validate_rubric_against_checks(rubric: dict[str, Any], path: Path) -> None:
    """Reject rubrics that reference unknown check ids (Q-H2).

    Every check_id referenced by any kind in the rubric MUST be a registered
    check function in ``_CHECKS``. An unknown check_id silently treated as a
    failure made typos and removed-check-name configs indistinguishable from
    genuine rubric failures. Fail loud at load time instead.
    """
    if not isinstance(rubric, dict):
        return
    unknown: set[str] = set()
    for kind_block in rubric.values():
        if not isinstance(kind_block, dict):
            continue
        for check_id in kind_block.get("checks", []):
            check_id_str = str(check_id)
            if check_id_str not in _CHECKS:
                unknown.add(check_id_str)
    if unknown:
        raise ValueError(
            f"Rubric {path} references unknown check ids: {sorted(unknown)}. "
            f"Registered checks: {sorted(_CHECKS)}. "
            f"Either add the check to _CHECKS or fix the rubric YAML."
        )


def _split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    match = FRONTMATTER_RE.match(text)
    if not match:
        return {}, text
    frontmatter = yaml.safe_load(match.group(1)) or {}
    body = text[match.end() :]
    return frontmatter, body


def _list_value(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list | tuple):
        return [str(item) for item in value if str(item).strip()]
    return [str(value)]


def _acceptance_items(context: _ShardContext) -> list[str]:
    items: list[str] = []
    raw_acceptance = context.frontmatter.get("acceptance")
    if isinstance(raw_acceptance, list):
        for item in raw_acceptance:
            if isinstance(item, dict):
                for key, value in item.items():
                    items.append(f"{key}: {value}")
            elif str(item).strip():
                items.append(str(item))
    else:
        items.extend(_list_value(raw_acceptance))
    section = _section(context.body, "Acceptance")
    if section:
        items.extend(_bullet_items(section))
    if not items:
        items.extend(_list_value(context.frontmatter.get("verification_commands")))
    return items


def _bullet_items(text: str) -> list[str]:
    return [line.strip()[2:].strip() for line in text.splitlines() if line.strip().startswith("- ")]


def _section(body: str, heading: str) -> str:
    pattern = re.compile(rf"^##\s+{re.escape(heading)}\s*$([\s\S]*?)(?=^##\s|\Z)", re.MULTILINE)
    match = pattern.search(body)
    return match.group(1).strip() if match else ""


def _has_non_empty_section(body: str, heading: str) -> bool:
    return bool(_section(body, heading).strip())


def _field_or_section(context: _ShardContext, field_name: str, section_name: str) -> bool:
    return bool(str(context.frontmatter.get(field_name) or "").strip()) or _has_non_empty_section(
        context.body,
        section_name,
    )


def _acceptance_runnable(context: _ShardContext, _rubric: dict[str, Any]) -> bool:
    items = _acceptance_items(context)
    if not items:
        return False
    for item in items:
        stripped = item.strip()
        if stripped.lower().startswith("output: written finding doc at "):
            continue
        if not is_runnable_command(stripped):
            return False
    return True


def _files_in_scope_closed(context: _ShardContext, _rubric: dict[str, Any]) -> bool:
    scope = _list_value(context.frontmatter.get("files_in_scope") or context.frontmatter.get("owned_write_scope"))
    if not scope:
        return False
    return all("*" not in item and "?" not in item and item.strip() for item in scope)


def _references_resolve(context: _ShardContext, _rubric: dict[str, Any]) -> bool:
    for section_name in REFERENCE_SECTIONS:
        section = _section(context.body, section_name)
        if not section:
            continue
        for token in PATH_TOKEN_RE.findall(section):
            if token.startswith(("http://", "https://")):
                continue
            if not Path(token).exists():
                return False
    return True


def _anti_goals_named(context: _ShardContext, _rubric: dict[str, Any]) -> bool:
    return bool(_list_value(context.frontmatter.get("anti_goals")))


def _adr_citation_if_required(context: _ShardContext, rubric: dict[str, Any]) -> bool:
    if rubric.get("adr_required") is not True:
        return True
    refs = " ".join(_list_value(context.frontmatter.get("adr_refs")))
    return bool(ADR_RE.search(context.body) or ADR_RE.search(refs))


def _finding_doc_path(context: _ShardContext, _rubric: dict[str, Any]) -> bool:
    return bool(str(context.frontmatter.get("finding_doc_path") or "").strip()) or _has_non_empty_section(
        context.body,
        "Output",
    )


def _hypothesis_stated(context: _ShardContext, _rubric: dict[str, Any]) -> bool:
    return _field_or_section(context, "hypothesis", "Hypothesis")


def _outcome_artifact(context: _ShardContext, _rubric: dict[str, Any]) -> bool:
    return _field_or_section(context, "deletion_or_promotion_artifact", "Outcome Artifact")


def _invariant_named(context: _ShardContext, _rubric: dict[str, Any]) -> bool:
    return _field_or_section(context, "invariant", "Invariant")


def _regression_tests_declared(context: _ShardContext, _rubric: dict[str, Any]) -> bool:
    tests = _list_value(context.frontmatter.get("regression_tests") or context.frontmatter.get("regression_test_set"))
    return any("test" in item.lower() for item in tests) or "test" in _section(context.body, "Regression Tests").lower()


def _coexistence_stated(context: _ShardContext, _rubric: dict[str, Any]) -> bool:
    return _field_or_section(context, "coexistence_strategy", "Migration Contract")


def _cutover_criterion_stated(context: _ShardContext, _rubric: dict[str, Any]) -> bool:
    return (
        bool(str(context.frontmatter.get("cutover_criterion") or "").strip())
        or "cutover"
        in _section(
            context.body,
            "Migration Contract",
        ).lower()
    )


_CHECKS: dict[str, Callable[[_ShardContext, dict[str, Any]], bool]] = {
    "acceptance_runnable": _acceptance_runnable,
    "files_in_scope_closed": _files_in_scope_closed,
    "references_resolve": _references_resolve,
    "anti_goals_named": _anti_goals_named,
    "adr_citation_if_required": _adr_citation_if_required,
    "finding_doc_path": _finding_doc_path,
    "hypothesis_stated": _hypothesis_stated,
    "outcome_artifact": _outcome_artifact,
    "invariant_named": _invariant_named,
    "regression_tests_declared": _regression_tests_declared,
    "coexistence_stated": _coexistence_stated,
    "cutover_criterion_stated": _cutover_criterion_stated,
}
