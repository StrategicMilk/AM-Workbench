"""Shared helper methods for quality gate checks."""

from __future__ import annotations

import re

from vetinari.validation.gate_types import GateResult


class _GateCheckHelpers:
    """Shared scoring and source-inspection helpers for gate check mixins."""

    @staticmethod
    def _score_to_result(score: float, min_score: float) -> GateResult:
        """Convert a numeric score to a GateResult enum value.

        A score below ``min_score * 0.7`` is FAILED; between that and
        ``min_score`` is WARNING; at or above ``min_score`` is PASSED.

        Args:
            score: Numeric score (0.0-1.0).
            min_score: Minimum passing score from gate config.

        Returns:
            GateResult enum value.
        """
        if score >= min_score:
            return GateResult.PASSED
        if score >= min_score * 0.7:
            return GateResult.WARNING
        return GateResult.FAILED

    @staticmethod
    def _check_long_functions(code: str, max_lines: int = 50) -> list[str]:
        """Return names of functions that exceed max_lines.

        Args:
            code: Source code string to scan.
            max_lines: Line count above which a function is considered too long.

        Returns:
            List of function names that exceed the limit.
        """
        long_fns = []
        lines = code.split("\n")
        current_fn = None
        fn_start = 0

        for i, line in enumerate(lines):
            match = re.match(r"^(\s*)def\s+(\w+)\s*\(", line)
            if match:
                if current_fn is not None and (i - fn_start) > max_lines:
                    long_fns.append(current_fn)
                current_fn = match.group(2)
                fn_start = i

        # Check last function
        if current_fn is not None and (len(lines) - fn_start) > max_lines:
            long_fns.append(current_fn)

        return long_fns

    @staticmethod
    def _check_missing_docstrings(code: str) -> list[str]:
        """Return names of public functions missing docstrings.

        Args:
            code: Source code string to scan.

        Returns:
            List of public function names (not prefixed with ``_``) that
            have no docstring on the line(s) immediately following their
            ``def`` statement.
        """
        missing = []
        lines = code.split("\n")

        for i, line in enumerate(lines):
            match = re.match(r"^\s*def\s+(\w+)\s*\(", line)
            if match:
                fn_name = match.group(1)
                if fn_name.startswith("_"):
                    continue  # Skip private functions
                found_docstring = False
                for j in range(i + 1, min(i + 5, len(lines))):
                    stripped = lines[j].strip()
                    if not stripped:
                        continue
                    if stripped.startswith(('"""', "'''")):
                        found_docstring = True
                    break
                if not found_docstring:
                    missing.append(fn_name)

        return missing
