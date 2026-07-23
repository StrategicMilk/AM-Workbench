"""Watch mode data models."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from vetinari.utils.serialization import dataclass_to_dict

DIRECTIVE_PATTERNS = [
    re.compile(r"#\s*@vetinari\s+(.+)", re.IGNORECASE),
    re.compile(r"//\s*@vetinari\s+(.+)", re.IGNORECASE),
    re.compile(r"/\*\s*@vetinari\s+(.+?)\s*\*/", re.IGNORECASE),
]


@dataclass(frozen=True, slots=True)
class FileChange:
    """A detected file change."""

    path: str
    change_type: str  # "modified", "created", "deleted"
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    size: int = 0

    def __repr__(self) -> str:
        """Show key identifying fields for debugging."""
        return f"FileChange(path={self.path!r}, change_type={self.change_type!r})"


@dataclass
class VetinariDirective:
    """A @vetinari directive found in source code."""

    file_path: str
    line_number: int
    directive: str  # The command after @vetinari
    full_line: str

    def __repr__(self) -> str:
        """Show key identifying fields for debugging."""
        return (
            f"VetinariDirective(file_path={self.file_path!r},"
            f" line_number={self.line_number!r}, directive={self.directive!r})"
        )

    @property
    def action(self) -> str:
        """Extract action verb from directive."""
        parts = self.directive.strip().split()
        return parts[0].lower() if parts else ""

    @property
    def target(self) -> str:
        """Extract target from directive."""
        parts = self.directive.strip().split(None, 1)
        return parts[1] if len(parts) > 1 else ""


@dataclass(frozen=True, slots=True)
class DirectiveReport:
    """A structured report entry written when a directive is processed.

    Each entry captures who asked for what, where, and when so that
    downstream tooling (CI, the dashboard, editors) can consume the
    report file without parsing log output.
    """

    action: str
    file_path: str
    line_number: int
    target: str
    full_line: str
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    priority: str = "normal"  # "high" for fix, "normal" for review/test

    def __repr__(self) -> str:
        """Show key identifying fields for debugging."""
        return f"DirectiveReport(action={self.action!r}, file_path={self.file_path!r}, priority={self.priority!r})"

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-compatible dictionary."""
        return dataclass_to_dict(self)


@dataclass(frozen=True, slots=True)
class WatchConfig:
    """Configuration for watch mode."""

    watch_dir: str = "."
    poll_interval: float = 2.0  # seconds
    include_patterns: list[str] = field(default_factory=lambda: ["*.py", "*.js", "*.ts", "*.jsx", "*.tsx"])
    exclude_patterns: list[str] = field(
        default_factory=lambda: [
            "__pycache__",
            ".git",
            "node_modules",
            ".venv",
            "*.pyc",
            ".vetinari",
        ],
    )
    max_file_size: int = 1_000_000  # 1MB max for scanning
    scan_directives: bool = True

    def __repr__(self) -> str:
        """Show key identifying fields for debugging."""
        return f"WatchConfig(watch_dir={self.watch_dir!r}, poll_interval={self.poll_interval!r})"
