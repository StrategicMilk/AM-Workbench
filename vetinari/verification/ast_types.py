"""Shared result types for AST analysis."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class SymbolKind(Enum):
    """Kind of top-level symbol extracted from source."""

    FUNCTION = "function"
    METHOD = "method"
    CLASS = "class"
    MODULE_VAR = "module_var"


@dataclass(frozen=True, slots=True)
class SymbolDef:
    """A symbol (function, class, variable) defined in source code.

    Attributes:
        name: Fully qualified name (e.g. "MyClass.my_method").
        kind: Whether this is a function, method, class, or module variable.
        line_start: 1-based starting line number.
        line_end: 1-based ending line number.
        args: Parameter names for functions/methods; empty for classes/vars.
        decorators: Decorator names applied to this symbol.
        docstring: First line of the docstring, or None if absent.
    """

    name: str
    kind: SymbolKind
    line_start: int
    line_end: int
    args: tuple[str, ...] = ()
    decorators: tuple[str, ...] = ()
    docstring: str | None = None

    def line_count(self) -> int:
        """Number of source lines this symbol spans.

        Returns:
            Positive integer line count (inclusive of start and end).
        """
        return self.line_end - self.line_start + 1

    def __repr__(self) -> str:
        """Show kind, name, and line range for debugging."""
        return f"SymbolDef({self.kind.value} {self.name!r}, lines {self.line_start}-{self.line_end})"


@dataclass(frozen=True, slots=True)
class CallRelation:
    """A call from one symbol to another detected in the AST.

    Attributes:
        caller: Name of the function/method that makes the call.
        callee: Name of the function/method being called.
        line: Line number where the call occurs.
    """

    caller: str
    callee: str
    line: int


@dataclass(frozen=True, slots=True)
class ImportDep:
    """An import dependency found in the source.

    Attributes:
        module: The module being imported (e.g. "os.path", "vetinari.types").
        names: Specific names imported (e.g. ("Path",)); empty for bare imports.
        is_internal: True if the import is from the vetinari package.
        line: Line number of the import statement.
    """

    module: str
    names: tuple[str, ...]
    is_internal: bool
    line: int

    def __repr__(self) -> str:
        return f"ImportDep(module={self.module!r}, is_internal={self.is_internal!r}, line={self.line})"


@dataclass(frozen=True, slots=True)
class ComplexityHotspot:
    """A function or method that exceeds complexity thresholds.

    Attributes:
        name: Symbol name of the complex function.
        line_count: Number of source lines the function spans.
        branch_count: Number of if/elif/for/while/try branches.
        line: Starting line number.
        reason: Human-readable description of why this is flagged.
    """

    name: str
    line_count: int
    branch_count: int
    line: int
    reason: str

    def __repr__(self) -> str:
        return f"ComplexityHotspot(name={self.name!r}, line_count={self.line_count}, branch_count={self.branch_count})"


@dataclass(frozen=True, slots=True)
class AstAnalysisResult:
    """Complete AST analysis result for a single source file.

    Attributes:
        file_path: Path to the analysed file.
        symbols: All defined symbols (functions, classes, variables).
        calls: Call relationships between symbols.
        imports: Import dependencies.
        dead_code: Symbols defined but never referenced within the file.
        hotspots: Complexity hotspots exceeding thresholds.
        total_lines: Total number of lines in the source file.
    """

    file_path: str
    symbols: tuple[SymbolDef, ...]
    calls: tuple[CallRelation, ...]
    imports: tuple[ImportDep, ...]
    dead_code: tuple[str, ...]
    hotspots: tuple[ComplexityHotspot, ...]
    total_lines: int

    def __repr__(self) -> str:
        """Show summary counts for debugging."""
        return (
            f"AnalysisResult({self.file_path!r}, "
            f"symbols={len(self.symbols)}, calls={len(self.calls)}, "
            f"dead={len(self.dead_code)}, hotspots={len(self.hotspots)})"
        )
