"""Shared primitives for supply-chain integrity rules."""

from __future__ import annotations

import abc
import ast
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import tomllib
import yaml

ABSOLUTE_DEV_PATH_RE = re.compile(r"^(/home/|/Users/|[A-Za-z]:[\\/]+Users[\\/])")
RuleSeverity = Literal["error", "warning"]


@dataclass(frozen=True, slots=True)
class IntegrityViolation:
    """One supply-chain integrity finding."""

    rule: str
    site: str
    detail: str
    severity: RuleSeverity

    def __repr__(self) -> str:
        """Return a compact finding identity for logs and failed assertions."""
        return (
            "IntegrityViolation("
            f"rule={self.rule!r}, site={self.site!r}, severity={self.severity!r}, detail={self.detail!r})"
        )


@dataclass(frozen=True, slots=True)
class IntegrityReport:
    """Typed result returned by the supply-chain integrity checker."""

    violations: tuple[IntegrityViolation, ...]
    checked_sites: int

    @property
    def ok(self) -> bool:
        """Return false when any error-severity violation is present."""
        return not any(v.severity == "error" for v in self.violations)


class IntegrityRule(abc.ABC):
    """Base class for stateless supply-chain integrity rules."""

    rule: str

    @abc.abstractmethod
    def check(self, project_root: Path) -> list[IntegrityViolation]:
        """Return all violations for this rule under ``project_root``."""


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _optional_missing(rule: str, path: Path) -> IntegrityViolation:
    return IntegrityViolation(rule, str(path), "optional input is missing", "warning")


def _load_yaml(path: Path, rule: str, *, optional: bool = False) -> tuple[object, list[IntegrityViolation]]:
    if not path.exists():
        if optional:
            return None, [_optional_missing(rule, path)]
        return None, [IntegrityViolation(rule, str(path), "mandatory YAML input is missing", "warning")]
    data = yaml.safe_load(_read_text(path))
    return data, []


def _load_json(path: Path, rule: str, *, optional: bool = False) -> tuple[object, list[IntegrityViolation]]:
    if not path.exists():
        if optional:
            return None, [_optional_missing(rule, path)]
        return None, [IntegrityViolation(rule, str(path), "mandatory JSON input is missing", "warning")]
    return json.loads(_read_text(path)), []


def _load_toml(
    path: Path, rule: str, *, optional: bool = False
) -> tuple[dict[str, object] | None, list[IntegrityViolation]]:
    if not path.exists():
        if optional:
            return None, [_optional_missing(rule, path)]
        return None, [IntegrityViolation(rule, str(path), "mandatory TOML input is missing", "warning")]
    with path.open("rb") as handle:
        return tomllib.load(handle), []


def _parse_python(
    path: Path, rule: str, *, optional: bool = False
) -> tuple[ast.Module | None, str, list[IntegrityViolation]]:
    if not path.exists():
        if optional:
            return None, "", [_optional_missing(rule, path)]
        return None, "", [IntegrityViolation(rule, str(path), "mandatory Python input is missing", "warning")]
    text = _read_text(path)
    return ast.parse(text, filename=str(path)), text, []


def _iter_string_values(value: object, path: str = "$") -> list[tuple[str, str]]:
    if isinstance(value, str):
        return [(path, value)]
    if isinstance(value, dict):
        found: list[tuple[str, str]] = []
        for key, child in value.items():
            found.extend(_iter_string_values(child, f"{path}.{key}"))
        return found
    if isinstance(value, list):
        found = []
        for index, child in enumerate(value):
            found.extend(_iter_string_values(child, f"{path}[{index}]"))
        return found
    return []


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _call_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return ""


def _target_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Tuple):
        return ",".join(_target_name(elt) for elt in node.elts)
    return ""


def _annotation_is_optional_str(node: ast.AST) -> bool:
    if isinstance(node, ast.Subscript) and _call_name(node.value) in {"Optional", "typing.Optional"}:
        return True
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        names = {_annotation_name(node.left), _annotation_name(node.right)}
        return "str" in names and "None" in names
    return False


def _annotation_name(node: ast.AST) -> str:
    if isinstance(node, ast.Constant) and node.value is None:
        return "None"
    return _call_name(node)


def _field_value_is_enforced(node: ast.AST | None) -> bool:
    if node is None:
        return False
    if isinstance(node, ast.Call) and _call_name(node.func) == "Field":
        if node.args and isinstance(node.args[0], ast.Constant):
            return node.args[0].value is not None
        if node.args and isinstance(node.args[0], ast.Constant) is False:
            return isinstance(node.args[0], ast.Constant) and node.args[0].value is Ellipsis
        for keyword in node.keywords:
            if keyword.arg in {"default", "default_factory"}:
                if isinstance(keyword.value, ast.Constant):
                    return keyword.value.value is not None
                return True
        return False
    if isinstance(node, ast.Constant):
        return node.value is not None
    return True
