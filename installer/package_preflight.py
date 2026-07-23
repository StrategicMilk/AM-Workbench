"""Shared packaging preflight helpers."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class PreflightResult:
    passed: bool
    blockers: tuple[str, ...] = ()
    artifact_path: str | None = None
    tool: str | None = None
    argv: tuple[tuple[str, ...], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "blockers": list(self.blockers),
            "artifact_path": self.artifact_path,
            "tool": self.tool,
            "argv": [list(command) for command in self.argv],
        }


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def preflight_staging_dir(staging_dir: Path | str, *, repo_root_path: Path | str | None = None) -> PreflightResult:
    path = Path(staging_dir)
    blockers: list[str] = []
    if not path.is_absolute():
        blockers.append("staging_dir must be absolute; relative paths can hide traversal")
    if ".." in path.parts:
        blockers.append("staging_dir contains parent traversal")
    try:
        resolved = path.resolve()
        root = Path(repo_root_path).resolve() if repo_root_path is not None else repo_root().resolve()
        if not resolved.is_relative_to(root):
            blockers.append("staging_dir must resolve inside the project root")
        if not resolved.is_dir():
            blockers.append("staging_dir must exist and be a directory")
    except OSError as exc:
        blockers.append(f"staging_dir cannot be resolved: {exc}")
    return PreflightResult(not blockers, tuple(blockers))
