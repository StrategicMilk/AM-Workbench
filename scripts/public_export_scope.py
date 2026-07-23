"""Trusted positive-scope policy for public export paths."""

from __future__ import annotations

import hashlib
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import tomllib

SCRIPTS_ROOT = Path(__file__).resolve().parent
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from public_export_path_contract import validate_public_paths

SCOPE_POLICY_VERSION = 1
_PUBLIC_CLASSIFICATIONS = frozenset({"product", "runtime", "release"})
_MATCH_KINDS = frozenset({"exact", "prefix"})
_TOP_LEVEL_KEYS = frozenset({"version", "public_groups", "private_groups"})
_PUBLIC_GROUP_KEYS = frozenset({"classification", "closure", "exact", "prefix"})
_PRIVATE_GROUP_KEYS = frozenset({"closure", "exact", "prefix"})


@dataclass(frozen=True)
class ScopeRule:
    """One normalized public or private path rule."""

    path: str
    match: str
    disposition: str
    classification: str
    closure: str

    def matches(self, candidate: str) -> bool:
        """Return whether this rule classifies *candidate*."""
        return candidate == self.path if self.match == "exact" else candidate.startswith(self.path)

    @property
    def precedence(self) -> tuple[int, int]:
        """Prefer the longest match and then an exact rule."""
        return (len(self.path), 1 if self.match == "exact" else 0)


@dataclass(frozen=True)
class PublicExportScope:
    """Validated positive-scope publication policy."""

    version: int
    sha256: str
    rules: tuple[ScopeRule, ...]

    def classify(self, path: str) -> ScopeRule | None:
        """Return the most-specific rule for *path*, if one exists."""
        matches = [rule for rule in self.rules if rule.matches(path)]
        if not matches:
            return None
        return max(matches, key=lambda rule: rule.precedence)

    def public_path_errors(self, paths: list[str]) -> list[tuple[str, str]]:
        """Return errors for unclassified or explicitly private paths."""
        errors: list[tuple[str, str]] = []
        for path in paths:
            rule = self.classify(path)
            if rule is None:
                errors.append((path, "path is not positively classified by the trusted public-export scope"))
            elif rule.disposition != "public":
                errors.append((path, f"path is classified private by trusted scope rule {rule.path!r}"))
        return errors


def _string_list(value: Any, *, field: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"{field} must be a list of strings")
    return value


def _validate_rule_path(path: str, *, match: str, field: str) -> str:
    candidate = path[:-1] if match == "prefix" and path.endswith("/") else path
    if match == "prefix" and not path.endswith("/"):
        raise ValueError(f"{field} prefix path must end with '/'")
    errors = validate_public_paths([candidate])
    if errors:
        raise ValueError(f"{field} has a non-canonical path {path!r}: {errors[0][1]}")
    return path


def _group_rules(
    groups: Any,
    *,
    disposition: str,
) -> list[ScopeRule]:
    if not isinstance(groups, list):
        raise ValueError(f"{disposition}_groups must be an array of tables")
    rules: list[ScopeRule] = []
    for group_index, group in enumerate(groups):
        if not isinstance(group, dict):
            raise ValueError(f"{disposition}_groups[{group_index}] must be a table")
        allowed_keys = _PUBLIC_GROUP_KEYS if disposition == "public" else _PRIVATE_GROUP_KEYS
        unknown_keys = sorted(set(group) - allowed_keys)
        if unknown_keys:
            raise ValueError(f"{disposition}_groups[{group_index}] has unknown keys: {', '.join(unknown_keys)}")
        classification = group.get("classification", "private")
        closure = group.get("closure", "")
        if disposition == "public" and classification not in _PUBLIC_CLASSIFICATIONS:
            raise ValueError(
                f"public_groups[{group_index}].classification must be one of {sorted(_PUBLIC_CLASSIFICATIONS)}"
            )
        if disposition == "private":
            classification = "private"
        if not isinstance(closure, str) or not closure.strip():
            raise ValueError(f"{disposition}_groups[{group_index}].closure must be non-empty")
        group_rule_count = 0
        for match in sorted(_MATCH_KINDS):
            values = _string_list(
                group.get(match, []),
                field=f"{disposition}_groups[{group_index}].{match}",
            )
            for value in values:
                group_rule_count += 1
                rules.append(
                    ScopeRule(
                        path=_validate_rule_path(
                            value,
                            match=match,
                            field=f"{disposition}_groups[{group_index}].{match}",
                        ),
                        match=match,
                        disposition=disposition,
                        classification=classification,
                        closure=closure,
                    )
                )
        if group_rule_count == 0:
            raise ValueError(f"{disposition}_groups[{group_index}] must contain at least one exact or prefix rule")
    return rules


def load_public_export_scope_bytes(raw: bytes) -> PublicExportScope:
    """Load and validate trusted public-export scope TOML bytes."""
    payload = tomllib.loads(raw.decode("utf-8"))
    unknown_keys = sorted(set(payload) - _TOP_LEVEL_KEYS)
    if unknown_keys:
        raise ValueError(f"public-export scope has unknown top-level keys: {', '.join(unknown_keys)}")
    version = payload.get("version")
    if type(version) is not int or version != SCOPE_POLICY_VERSION:
        raise ValueError(f"public-export scope version must be {SCOPE_POLICY_VERSION}, got {version!r}")
    rules = [
        *_group_rules(payload.get("public_groups", []), disposition="public"),
        *_group_rules(payload.get("private_groups", []), disposition="private"),
    ]
    if not rules:
        raise ValueError("public-export scope must contain at least one rule")
    seen: dict[tuple[str, str], ScopeRule] = {}
    for rule in rules:
        key = (rule.match, rule.path)
        previous = seen.get(key)
        if previous is not None:
            raise ValueError(
                f"duplicate public-export scope rule {rule.match}:{rule.path!r} "
                f"({previous.disposition} and {rule.disposition})"
            )
        seen[key] = rule
    return PublicExportScope(
        version=version,
        sha256=hashlib.sha256(raw).hexdigest(),
        rules=tuple(rules),
    )


def load_public_export_scope(path: Path) -> PublicExportScope:
    """Load and validate a trusted public-export scope TOML file."""
    return load_public_export_scope_bytes(path.read_bytes())


__all__ = [
    "SCOPE_POLICY_VERSION",
    "PublicExportScope",
    "ScopeRule",
    "load_public_export_scope",
    "load_public_export_scope_bytes",
]
