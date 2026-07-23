"""Deterministic structural diff for Workbench artifact reviews."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class ChangedSection:
    """One changed structural path in an artifact comparison."""

    path: str
    before_value_repr: str
    after_value_repr: str
    change_kind: str

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"ChangedSection(path={self.path!r}, before_value_repr={self.before_value_repr!r}, after_value_repr={self.after_value_repr!r})"

    def to_dict(self) -> dict[str, Any]:
        """Return the artifact-review API JSON contract for this section."""
        return {
            "path": self.path,
            "before_value_repr": self.before_value_repr,
            "after_value_repr": self.after_value_repr,
            "change_kind": self.change_kind,
        }


@dataclass(frozen=True, slots=True)
class ArtifactDiff:
    """Stable structural diff result."""

    subject_id: str
    kind: str
    before_signature: str
    after_signature: str
    changed_sections: tuple[ChangedSection, ...]

    @property
    def is_noop(self) -> bool:
        """Return true only when signatures and section-level changes agree."""
        return self.before_signature == self.after_signature and not self.changed_sections

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"ArtifactDiff(subject_id={self.subject_id!r}, kind={self.kind!r}, before_signature={self.before_signature!r})"

    def to_dict(self) -> dict[str, Any]:
        """Return the artifact-review API JSON contract for this diff."""
        return {
            "subject_id": self.subject_id,
            "kind": self.kind,
            "before_signature": self.before_signature,
            "after_signature": self.after_signature,
            "changed_sections": [section.to_dict() for section in self.changed_sections],
            "is_noop": self.is_noop,
        }


def compute_artifact_diff(*, before: Any, after: Any, subject_id: str, kind: str) -> ArtifactDiff:
    """Return a deterministic structural diff without hiding textual rewrites."""
    return ArtifactDiff(
        subject_id=subject_id,
        kind=kind,
        before_signature=_signature(before),
        after_signature=_signature(after),
        changed_sections=tuple(_diff_paths(before, after)),
    )


def _signature(value: Any) -> str:
    payload = _jsonable(value)
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _diff_paths(before: Any, after: Any, path: str = "$") -> list[ChangedSection]:
    if isinstance(before, Mapping) and isinstance(after, Mapping):
        changes: list[ChangedSection] = []
        before_keys = set(before)
        after_keys = set(after)
        changes.extend(
            ChangedSection(f"{path}.{key}", _normalise_section(before[key]), "", "removed")
            for key in sorted(before_keys - after_keys, key=str)
        )
        changes.extend(
            ChangedSection(f"{path}.{key}", "", _normalise_section(after[key]), "added")
            for key in sorted(after_keys - before_keys, key=str)
        )
        for key in sorted(before_keys & after_keys, key=str):
            changes.extend(_diff_paths(before[key], after[key], f"{path}.{key}"))
        return changes
    if _is_sequence(before) and _is_sequence(after):
        if list(before) == list(after):
            return []
        if sorted(_normalise_section(item) for item in before) == sorted(_normalise_section(item) for item in after):
            return [ChangedSection(path, _normalise_section(before), _normalise_section(after), "reordered_collection")]
        changes = []
        for index in range(max(len(before), len(after))):
            child_path = f"{path}[{index}]"
            if index >= len(before):
                changes.append(ChangedSection(child_path, "", _normalise_section(after[index]), "added"))
            elif index >= len(after):
                changes.append(ChangedSection(child_path, _normalise_section(before[index]), "", "removed"))
            else:
                changes.extend(_diff_paths(before[index], after[index], child_path))
        return changes
    if before != after:
        return [ChangedSection(path, _normalise_section(before), _normalise_section(after), "value")]
    return []


def _is_sequence(value: Any) -> bool:
    return isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray)


def _normalise_section(value: Any) -> str:
    return json.dumps(_jsonable(value), sort_keys=True, ensure_ascii=False, default=str)


def _jsonable(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _jsonable(value[key]) for key in sorted(value, key=str)}
    if _is_sequence(value):
        return [_jsonable(item) for item in value]
    if value is None or isinstance(value, str | int | float | bool):
        return value
    return repr(value)


__all__ = ["ArtifactDiff", "ChangedSection", "compute_artifact_diff"]
