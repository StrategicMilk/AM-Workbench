"""Shared pattern result types for consistency checking."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class PatternCategory(Enum):
    """Categories of implementation patterns that can be inconsistently applied."""

    FILE_TYPE_CHECK = "file_type_check"  # .endswith vs in set vs regex for extensions
    STRING_MATCHING = "string_matching"  # in vs startswith vs regex for string checks
    COLLECTION_MEMBERSHIP = "collection_membership"  # set vs list vs tuple for membership
    ERROR_HANDLING = "error_handling"  # broad except vs specific vs contextlib
    NULL_CHECK = "null_check"  # is None vs == None vs not x
    ITERATION_PATTERN = "iteration_pattern"  # for+append vs comprehension vs map
    IMPORT_STYLE = "import_style"  # import x vs from x import y


@dataclass(frozen=True, slots=True)
class PatternInstance:
    """A single occurrence of an implementation pattern in source code.

    Attributes:
        category: Which pattern category this belongs to.
        implementation: Short label for the specific approach used (e.g. "endswith").
        file_path: Path to the source file containing this instance.
        line_number: 1-based line number of the pattern occurrence.
        code_snippet: The actual source code (1-3 lines) illustrating the pattern.
    """

    category: PatternCategory
    implementation: str
    file_path: str
    line_number: int
    code_snippet: str

    def __repr__(self) -> str:
        """Show key identifying fields for debugging."""
        return (
            f"PatternInstance(category={self.category.value!r}, "
            f"impl={self.implementation!r}, "
            f"file={self.file_path!r}, line={self.line_number})"
        )


@dataclass(frozen=True, slots=True)
class ConsistencyIssue:
    """An inconsistency found when the same logical operation uses different patterns.

    Attributes:
        category: The pattern category where inconsistency was detected.
        instances: All conflicting pattern instances (2 or more).
        severity: Always "medium" per US-014 acceptance criteria.
        message: Human-readable description of the inconsistency.
        suggested_pattern: The recommended implementation to standardise on.
    """

    category: PatternCategory
    instances: tuple[PatternInstance, ...]
    severity: str = "medium"  # Always medium per AC
    message: str = ""
    suggested_pattern: str = ""

    def __repr__(self) -> str:
        """Show key identifying fields for debugging."""
        impls = {i.implementation for i in self.instances}
        return (
            f"ConsistencyIssue(category={self.category.value!r}, "
            f"implementations={sorted(impls)!r}, "
            f"severity={self.severity!r})"
        )
