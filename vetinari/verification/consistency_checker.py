r"""Implementation consistency checker — detects semantically similar code using different patterns.

Scans Python source for common operations (file type checking, string matching,
collection membership, error handling) and flags cases where the same logical
operation uses different implementation patterns across the codebase.

Example inconsistency: one module uses ``if ext in {'.py', '.js'}`` while another
uses ``if filename.endswith(('.py', '.js'))`` and a third uses
``re.match(r'.*\\.(py|js)$', filename)``.

Part of the Inspector verification pipeline (US-014).
"""

from __future__ import annotations

import ast
import logging

from vetinari.verification.consistency_types import (
    ConsistencyIssue,
    PatternCategory,
    PatternInstance,
)

logger = logging.getLogger(__name__)

# -- AST visitor ----------------------------------------------------------------


class _PatternVisitor(ast.NodeVisitor):
    """AST visitor that extracts PatternInstance objects from a parsed module.

    Walks the AST and classifies expressions into PatternCategory buckets
    based on the node type and call/attribute structure. Each match produces
    one PatternInstance recording where the pattern appeared.

    Args:
        file_path: Source file path to embed in produced PatternInstance objects.
        source_lines: Original source split by newline, used to extract snippets.
    """

    def __init__(self, file_path: str, source_lines: list[str]) -> None:
        self._file_path = file_path
        self._source_lines = source_lines
        self.instances: list[PatternInstance] = []

    # -- helpers --

    def _snippet(self, lineno: int) -> str:
        """Return the source line at ``lineno`` (1-based), stripped.

        Args:
            lineno: 1-based line number.

        Returns:
            Stripped source line, or empty string when out of range.
        """
        idx = lineno - 1
        if 0 <= idx < len(self._source_lines):
            return self._source_lines[idx].strip()
        return ""

    def _add(
        self,
        category: PatternCategory,
        implementation: str,
        lineno: int,
    ) -> None:
        """Append a PatternInstance to the accumulated results.

        Args:
            category: Pattern category for this instance.
            implementation: Short label for the approach (e.g. "endswith", "in_set").
            lineno: 1-based line number of the pattern.
        """
        self.instances.append(
            PatternInstance(
                category=category,
                implementation=implementation,
                file_path=self._file_path,
                line_number=lineno,
                code_snippet=self._snippet(lineno),
            )
        )

    # -- FILE_TYPE_CHECK --------------------------------------------------------

    def _check_file_type_call(self, node: ast.Call) -> None:
        """Detect ``filename.endswith(...)`` calls used for file type checking.

        Args:
            node: An ast.Call node to inspect.
        """
        if not isinstance(node.func, ast.Attribute):
            return
        if node.func.attr != "endswith":
            return
        if not node.args:
            return
        arg = node.args[0]
        # endswith(('.py', '.js')) or endswith('.py') — look for extension-like strings
        if isinstance(arg, ast.Tuple):
            strings = [e for e in arg.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)]
            if any(s.value.startswith(".") for s in strings):
                self._add(PatternCategory.FILE_TYPE_CHECK, "endswith", node.lineno)
        elif isinstance(arg, ast.Constant) and isinstance(arg.value, str) and arg.value.startswith("."):
            self._add(PatternCategory.FILE_TYPE_CHECK, "endswith", node.lineno)

    def _check_file_type_compare(self, node: ast.Compare) -> None:
        """Detect ``ext in {'.py', '.js'}`` or ``ext in ['.py']`` for extension checks.

        Args:
            node: An ast.Compare node to inspect.
        """
        for op, comparator in zip(node.ops, node.comparators):
            if not isinstance(op, ast.In):
                continue
            if isinstance(comparator, (ast.Set, ast.List)):
                elts = comparator.elts
                strings = [e for e in elts if isinstance(e, ast.Constant) and isinstance(e.value, str)]
                if any(s.value.startswith(".") for s in strings):
                    kind = "in_set" if isinstance(comparator, ast.Set) else "in_list"
                    self._add(PatternCategory.FILE_TYPE_CHECK, kind, node.lineno)

    # -- STRING_MATCHING --------------------------------------------------------

    def _check_string_matching_call(self, node: ast.Call) -> None:
        """Detect ``x.startswith(...)`` and ``re.match/search(...)`` string matching.

        Args:
            node: An ast.Call node to inspect.
        """
        if isinstance(node.func, ast.Attribute):
            if node.func.attr == "startswith" and node.args:
                self._add(PatternCategory.STRING_MATCHING, "startswith", node.lineno)
            elif (
                node.func.attr in ("match", "search", "fullmatch")
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "re"
            ):
                self._add(PatternCategory.STRING_MATCHING, "regex", node.lineno)
        elif isinstance(node.func, ast.Attribute):
            pass  # already handled above

    def _check_string_membership(self, node: ast.Compare) -> None:
        """Detect ``x in some_string`` membership tests.

        Args:
            node: An ast.Compare node to inspect.
        """
        for op, comparator in zip(node.ops, node.comparators):
            # We can't know the type of `comparator` statically, but
            # if the left operand is a string constant this is string-in-string
            if (
                isinstance(op, ast.In)
                and isinstance(comparator, ast.Name)
                and isinstance(node.left, ast.Constant)
                and isinstance(node.left.value, str)
            ):
                self._add(PatternCategory.STRING_MATCHING, "in_string", node.lineno)

    # -- COLLECTION_MEMBERSHIP --------------------------------------------------

    def _check_collection_membership(self, node: ast.Compare) -> None:
        """Detect ``x in [...]`` vs ``x in {...}`` vs ``x in (...)`` membership tests.

        Excludes extension-pattern literals already captured by FILE_TYPE_CHECK.

        Args:
            node: An ast.Compare node to inspect.
        """
        for op, comparator in zip(node.ops, node.comparators):
            if not isinstance(op, ast.In):
                continue
            if isinstance(comparator, ast.Set):
                # Exclude file-extension sets (already captured by FILE_TYPE_CHECK)
                elts = comparator.elts
                strings = [e for e in elts if isinstance(e, ast.Constant) and isinstance(e.value, str)]
                if strings and all(s.value.startswith(".") for s in strings):
                    continue
                self._add(PatternCategory.COLLECTION_MEMBERSHIP, "in_set", node.lineno)
            elif isinstance(comparator, ast.List):
                elts = comparator.elts
                strings = [e for e in elts if isinstance(e, ast.Constant) and isinstance(e.value, str)]
                if strings and all(s.value.startswith(".") for s in strings):
                    continue
                self._add(PatternCategory.COLLECTION_MEMBERSHIP, "in_list", node.lineno)
            elif isinstance(comparator, ast.Tuple) and comparator.elts:
                # Non-empty tuple literal (not function arg) as membership target
                self._add(PatternCategory.COLLECTION_MEMBERSHIP, "in_tuple", node.lineno)

    # -- NULL_CHECK -------------------------------------------------------------

    def _check_null_check(self, node: ast.Compare) -> None:
        """Detect ``x is None`` vs ``x == None`` null checks.

        Args:
            node: An ast.Compare node to inspect.
        """
        for op, comparator in zip(node.ops, node.comparators):
            if not (isinstance(comparator, ast.Constant) and comparator.value is None):
                continue
            if isinstance(op, ast.Is):
                self._add(PatternCategory.NULL_CHECK, "is_none", node.lineno)
            elif isinstance(op, (ast.Eq, ast.NotEq)):
                self._add(PatternCategory.NULL_CHECK, "eq_none", node.lineno)

    def _check_implicit_none(self, node: ast.UnaryOp) -> None:
        """Detect ``not x`` used as an implicit None/falsy check.

        Args:
            node: An ast.UnaryOp node to inspect.
        """
        # Only flag when operand is a Name (variable), not a complex expression
        if isinstance(node.op, ast.Not) and isinstance(node.operand, ast.Name):
            self._add(PatternCategory.NULL_CHECK, "not_x", node.lineno)

    # -- ERROR_HANDLING ---------------------------------------------------------

    def _check_error_handling(self, node: ast.ExceptHandler) -> None:
        """Detect error-handling styles: bare except, broad Exception, or specific types.

        Classifies each except clause into one of three implementations:
        - ``bare`` — ``except:`` with no type (catches everything including KeyboardInterrupt)
        - ``broad`` — ``except Exception:`` or ``except BaseException:``
        - ``specific`` — ``except SomeConcreteError:`` or ``except (A, B):``

        Args:
            node: An ast.ExceptHandler node to inspect.
        """
        if node.type is None:
            # bare except:
            self._add(PatternCategory.ERROR_HANDLING, "bare", node.lineno)
        elif isinstance(node.type, ast.Name) and node.type.id in ("Exception", "BaseException"):
            self._add(PatternCategory.ERROR_HANDLING, "broad", node.lineno)
        else:
            # specific exception type(s) — ast.Name for single, ast.Tuple for multiple
            self._add(PatternCategory.ERROR_HANDLING, "specific", node.lineno)

    # -- IMPORT_STYLE -----------------------------------------------------------

    def _check_import_style(self, node: ast.Import | ast.ImportFrom) -> None:
        """Detect import style: ``import x`` vs ``from x import y``.

        Both forms are legitimate, but mixing them inconsistently within a file
        (e.g., sometimes ``import os`` and sometimes ``from os import path``)
        can signal style drift.

        Args:
            node: An ast.Import or ast.ImportFrom node to inspect.
        """
        if isinstance(node, ast.Import):
            self._add(PatternCategory.IMPORT_STYLE, "import_module", node.lineno)
        else:
            self._add(PatternCategory.IMPORT_STYLE, "from_import", node.lineno)

    # -- ITERATION_PATTERN ------------------------------------------------------

    def _check_for_append(self, node: ast.For) -> None:
        """Detect ``for ... append(...)`` accumulation loops.

        Looks for a for-loop whose body contains exactly one statement that
        is a call to ``<list>.append(...)``.

        Args:
            node: An ast.For node to inspect.
        """
        body = node.body
        if len(body) != 1:
            return
        stmt = body[0]
        if not isinstance(stmt, ast.Expr):
            return
        call = stmt.value
        if not isinstance(call, ast.Call):
            return
        if isinstance(call.func, ast.Attribute) and call.func.attr == "append":
            self._add(PatternCategory.ITERATION_PATTERN, "for_append", node.lineno)

    def _check_list_comprehension(self, node: ast.ListComp) -> None:
        """Detect list comprehensions as an iteration pattern.

        Args:
            node: An ast.ListComp node to inspect.
        """
        self._add(PatternCategory.ITERATION_PATTERN, "list_comprehension", node.lineno)

    def _check_map_call(self, node: ast.Call) -> None:
        """Detect ``map(func, iterable)`` calls used instead of comprehensions.

        Args:
            node: An ast.Call node to inspect.
        """
        if isinstance(node.func, ast.Name) and node.func.id == "map" and len(node.args) >= 2:
            self._add(PatternCategory.ITERATION_PATTERN, "map_call", node.lineno)

    # -- node visitors --

    def visit_Call(self, node: ast.Call) -> None:
        """Dispatch call-site checks for FILE_TYPE_CHECK, STRING_MATCHING, ITERATION_PATTERN.

        Args:
            node: An ast.Call node.
        """
        self._check_file_type_call(node)
        self._check_string_matching_call(node)
        self._check_map_call(node)
        self.generic_visit(node)

    def visit_Compare(self, node: ast.Compare) -> None:
        """Dispatch comparison checks for FILE_TYPE_CHECK, COLLECTION_MEMBERSHIP, NULL_CHECK.

        Args:
            node: An ast.Compare node.
        """
        self._check_file_type_compare(node)
        self._check_string_membership(node)
        self._check_collection_membership(node)
        self._check_null_check(node)
        self.generic_visit(node)

    def visit_UnaryOp(self, node: ast.UnaryOp) -> None:
        """Dispatch unary-op checks for NULL_CHECK (``not x``).

        Args:
            node: An ast.UnaryOp node.
        """
        self._check_implicit_none(node)
        self.generic_visit(node)

    def visit_For(self, node: ast.For) -> None:
        """Dispatch for-loop checks for ITERATION_PATTERN.

        Args:
            node: An ast.For node.
        """
        self._check_for_append(node)
        self.generic_visit(node)

    def visit_ListComp(self, node: ast.ListComp) -> None:
        """Dispatch list-comprehension checks for ITERATION_PATTERN.

        Args:
            node: An ast.ListComp node.
        """
        self._check_list_comprehension(node)
        self.generic_visit(node)

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        """Dispatch except-clause checks for ERROR_HANDLING.

        Args:
            node: An ast.ExceptHandler node.
        """
        self._check_error_handling(node)
        self.generic_visit(node)

    def visit_Import(self, node: ast.Import) -> None:
        """Dispatch import-statement checks for IMPORT_STYLE.

        Args:
            node: An ast.Import node.
        """
        self._check_import_style(node)
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        """Dispatch from-import checks for IMPORT_STYLE.

        Args:
            node: An ast.ImportFrom node.
        """
        self._check_import_style(node)
        self.generic_visit(node)


# -- Inconsistency detection helpers -------------------------------------------

# Categories where mixing different implementations within a single file is
# an inconsistency, vs categories where both forms appear legitimately together.
_SINGLE_FILE_INCONSISTENT_CATEGORIES: frozenset[PatternCategory] = frozenset({
    PatternCategory.FILE_TYPE_CHECK,
    PatternCategory.COLLECTION_MEMBERSHIP,
    PatternCategory.NULL_CHECK,
    PatternCategory.ITERATION_PATTERN,
    PatternCategory.STRING_MATCHING,
    PatternCategory.ERROR_HANDLING,
    PatternCategory.IMPORT_STYLE,
})

# Preferred implementation for each category (used in suggested_pattern message).
_PREFERRED: dict[PatternCategory, str] = {
    PatternCategory.FILE_TYPE_CHECK: "endswith",
    PatternCategory.COLLECTION_MEMBERSHIP: "in_set",
    PatternCategory.NULL_CHECK: "is_none",
    PatternCategory.ITERATION_PATTERN: "list_comprehension",
    PatternCategory.STRING_MATCHING: "startswith",
    PatternCategory.ERROR_HANDLING: "specific",
    PatternCategory.IMPORT_STYLE: "from_import",
}


def _build_issues(
    instances: list[PatternInstance],
    min_distinct: int = 2,
) -> list[ConsistencyIssue]:
    """Group instances by category and flag categories with 2+ distinct implementations.

    Args:
        instances: All PatternInstance objects to consider.
        min_distinct: Minimum number of distinct implementations required to flag
            an inconsistency. Defaults to 2.

    Returns:
        List of ConsistencyIssue objects, one per inconsistent category.
    """
    # Group instances by category
    by_category: dict[PatternCategory, list[PatternInstance]] = {}
    for inst in instances:
        if inst.category not in _SINGLE_FILE_INCONSISTENT_CATEGORIES:
            continue
        by_category.setdefault(inst.category, []).append(inst)

    issues: list[ConsistencyIssue] = []
    for category, insts in by_category.items():
        distinct_impls = {i.implementation for i in insts}
        if len(distinct_impls) < min_distinct:
            continue

        preferred = _PREFERRED.get(category, "")
        impl_list = ", ".join(sorted(distinct_impls))
        message = (
            f"Inconsistent {category.value} implementations found: {impl_list}. "
            f"Standardise on one approach across the codebase."
        )

        issues.append(
            ConsistencyIssue(
                category=category,
                instances=tuple(insts),
                severity="medium",
                message=message,
                suggested_pattern=preferred,
            )
        )

    return issues


# -- Public API ----------------------------------------------------------------


def extract_patterns(
    source_code: str,
    file_path: str = "<unknown>",
) -> list[PatternInstance]:
    """Parse Python source and extract detected implementation patterns.

    Args:
        source_code: Source object or text processed by the operation.
        file_path: Filesystem path read or written by the operation.

    Returns:
        Value produced for the caller.
    """
    try:
        tree = ast.parse(source_code, filename=file_path)
    except SyntaxError as exc:
        logger.warning("Could not parse %s — skipping pattern extraction: %s", file_path, exc)
        return []

    source_lines = source_code.splitlines()
    visitor = _PatternVisitor(file_path=file_path, source_lines=source_lines)
    visitor.visit(tree)

    logger.debug(
        "extract_patterns: %s -> %d instances across %d categories",
        file_path,
        len(visitor.instances),
        len({i.category for i in visitor.instances}),
    )
    return visitor.instances


def check_consistency(
    source_code: str,
    file_path: str = "<unknown>",
) -> list[ConsistencyIssue]:
    """Check one source file for internally inconsistent patterns.

    Args:
        source_code: Source object or text processed by the operation.
        file_path: Filesystem path read or written by the operation.

    Returns:
        Value produced for the caller.
    """
    instances = extract_patterns(source_code, file_path)
    if not instances:
        return []

    issues = _build_issues(instances)

    if issues:
        logger.info(
            "check_consistency: %s -> %d inconsistency issue(s) found",
            file_path,
            len(issues),
        )

    return issues


def check_consistency_across_files(
    sources: dict[str, str],
) -> list[ConsistencyIssue]:
    """Check a set of source files for cross-file implementation inconsistencies.

    Extracts patterns from every file, then flags any PatternCategory where
    two or more distinct implementation approaches appear across the corpus.
    This catches the case where one file uses ``endswith`` while another uses
    ``in {'.py'}`` — both files are individually consistent but the codebase
    is not.

    Args:
        sources: Mapping of ``{file_path: source_code}`` to analyse together.

    Returns:
        List of ConsistencyIssue objects with ``severity="medium"``.
        Issues reference instances from all files that contributed conflicting
        implementations.
    """
    all_instances: list[PatternInstance] = []
    for file_path, source_code in sources.items():
        all_instances.extend(extract_patterns(source_code, file_path))

    if not all_instances:
        return []

    issues = _build_issues(all_instances)

    if issues:
        logger.info(
            "check_consistency_across_files: %d file(s) -> %d cross-file issue(s)",
            len(sources),
            len(issues),
        )

    return issues
