"""Grep-based code context extractor — finds callers, tests, and references for focused agent context.

Provides surgical code extraction via ripgrep (with Python re fallback)
to reduce token usage by 40-60% compared to reading whole files.

Pipeline role:
    Intake → **GrepContext** (targeted context extraction) → Planning → Execution → Verify → Learn
    Used alongside RepoMap to give agents precisely the code that is relevant
    to a task: the definition of a function, its callers, its tests, and any
    related symbols.  Unlike RepoMap (which gives breadth), GrepContext gives
    depth — detailed snippets around the specific symbols an agent needs.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import threading
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from vetinari.constants import GREP_TIMEOUT
from vetinari.security.redaction import redact_text

logger = logging.getLogger(__name__)


# Static patterns compiled at module level
_RG_LINE_RE = re.compile(r"^(.+?):(\d+)[:|-](.*)$")


@dataclass
class GrepMatch:
    """A single grep match with surrounding context."""

    file_path: str
    line_number: int
    line_content: str
    context_before: list[str] = field(default_factory=list)
    context_after: list[str] = field(default_factory=list)

    def __repr__(self) -> str:
        return f"GrepMatch(file_path={self.file_path!r}, line_number={self.line_number!r})"

    def redacted(self) -> GrepMatch:
        """Return a prompt-safe copy with sensitive values removed."""
        return GrepMatch(
            file_path=redact_text(self.file_path),
            line_number=self.line_number,
            line_content=redact_text(self.line_content),
            context_before=[redact_text(line) for line in self.context_before],
            context_after=[redact_text(line) for line in self.context_after],
        )


@lru_cache(maxsize=256)
def _compile_definition_pattern(name: str) -> re.Pattern[str]:
    """Compile and cache a def/class definition search pattern.

    Args:
        name: Function or class name to search for.

    Returns:
        Compiled regex matching a ``def`` or ``class`` header for *name*.
    """
    return re.compile(rf"^(def|class)\s+{re.escape(name)}\b")


@lru_cache(maxsize=256)
def _compile_keywords_pattern(keywords_key: str) -> re.Pattern[str]:
    """Compile and cache a keyword OR pattern for context extraction.

    Args:
        keywords_key: Pipe-joined, pre-escaped keyword string used as cache key.

    Returns:
        Compiled regex matching any of the keywords, case-insensitive.
    """
    return re.compile(keywords_key, re.IGNORECASE)


@lru_cache(maxsize=256)
def _compile_patterns_union(patterns_key: str) -> re.Pattern[str]:
    """Compile and cache a union of ripgrep fallback patterns.

    Args:
        patterns_key: Pipe-joined pattern string used as cache key.

    Returns:
        Compiled regex matching any of the patterns.
    """
    return re.compile(patterns_key)


class GrepContext:
    """Surgical code extraction via ripgrep for token-efficient agent context.

    Uses ripgrep when available (10-100x faster), falls back to Python re.
    """

    def __init__(self):
        self._rg_path = shutil.which("rg")
        self._use_rg = self._rg_path is not None

    @property
    def backend(self) -> str:
        """The name of the active search backend ('ripgrep' or 'python-re')."""
        return "ripgrep" if self._use_rg else "python-re"

    def extract_patterns(
        self,
        file_paths: list[str],
        patterns: list[str],
        context_lines: int = 3,
        max_matches: int = 20,
    ) -> list[GrepMatch]:
        """Extract lines matching any pattern with N lines of surrounding context.

        Args:
            file_paths: Files to search.
            patterns: Regex patterns to match.
            context_lines: Lines of context before/after each match.
            max_matches: Maximum matches to return.

        Returns:
            List of GrepMatch objects.
        """
        if not file_paths or not patterns:
            return []
        # Filter to existing files
        existing = [p for p in file_paths if Path(p).is_file()]
        if not existing:
            return []
        if self._use_rg:
            return self._rg_extract(existing, patterns, context_lines, max_matches)
        return self._python_extract(existing, patterns, context_lines, max_matches)

    def extract_definitions(
        self,
        file_path: str,
        names: list[str],
        max_lines: int = 30,
    ) -> str:
        """Extract function/class definitions by name with body (up to max_lines).

        Args:
            file_path: File to search.
            names: Function/class names to find.
            max_lines: Max lines per definition.

        Returns:
            Formatted string of definitions found.
        """
        if not Path(file_path).is_file():
            return ""
        lines = Path(file_path).read_text(encoding="utf-8", errors="replace").splitlines()
        results = []
        for name in names:
            pattern = _compile_definition_pattern(name)
            for i, line in enumerate(lines):
                if pattern.match(line.lstrip()):
                    end = min(i + max_lines, len(lines))
                    # Find end of definition by indentation
                    base_indent = len(line) - len(line.lstrip())
                    for j in range(i + 1, end):
                        stripped = lines[j].strip()
                        if (
                            stripped
                            and (len(lines[j]) - len(lines[j].lstrip())) <= base_indent
                            and not stripped.startswith(("#", '"""', "'''", "@"))
                        ):
                            end = j
                            break
                    block = redact_text("\n".join(lines[i:end]))
                    results.append(f"# {redact_text(file_path)}:{i + 1}\n{block}")
                    break
        return "\n\n".join(results)

    def extract_imports(self, file_path: str) -> str:
        """Extract only import statements from a file (~5% of file size).

        Args:
            file_path: Path to the file to extract imports from.

        Returns:
            Newline-joined string of all ``import`` and ``from ... import`` lines,
            or an empty string if the file does not exist.
        """
        if not Path(file_path).is_file():
            return ""
        lines = Path(file_path).read_text(encoding="utf-8", errors="replace").splitlines()
        imports = [redact_text(line) for line in lines if line.strip().startswith(("import ", "from "))]
        return "\n".join(imports)

    def extract_security_patterns(self, file_paths: list[str]) -> list[GrepMatch]:
        """Extract lines matching common security anti-patterns from a list of files.

        Searches for hardcoded secrets, unsafe eval/exec/pickle, SQL injection patterns,
        innerHTML usage, weak hash algorithms, subprocess shell=True, and credential files.

        Args:
            file_paths: Files to search for security-relevant patterns.

        Returns:
            GrepMatch objects for each matching line, with 2 lines of surrounding context.
        """
        SECURITY_PATTERNS = [
            r"password\s*=|api_key\s*=|secret\s*=|token\s*=",
            r"\beval\s*\(|\bexec\s*\(|pickle\.loads",
            r"execute\s*\(.*%|execute\s*\(.*\.format",
            r"innerHTML|document\.write",
            r"\bMD5\b|\bSHA1\b",
            r"subprocess.*shell\s*=\s*True",
            r"\.env|credentials|PRIVATE_KEY",
        ]
        return self.extract_patterns(file_paths, SECURITY_PATTERNS, context_lines=2)

    def extract_relevant_context(
        self,
        file_path: str,
        keywords: list[str],
        budget_chars: int = 2000,
    ) -> str:
        """Smart context extraction: find keyword matches, expand to enclosing scope.

        This is the primary method agents should use instead of reading whole files.

        Args:
            file_path: File to extract from.
            keywords: Keywords to search for.
            budget_chars: Maximum characters in output.

        Returns:
            Formatted context string within budget.
        """
        if not keywords or not Path(file_path).is_file():
            return ""

        lines = Path(file_path).read_text(encoding="utf-8", errors="replace").splitlines()
        if not lines:
            return ""

        # Find matching line numbers — use cached compile keyed by joined pattern string
        keywords_key = "|".join(re.escape(kw) for kw in keywords)
        pattern = _compile_keywords_pattern(keywords_key)
        match_lines = set()
        for i, line in enumerate(lines):
            if pattern.search(line):
                match_lines.add(i)

        if not match_lines:
            return ""

        # Expand each match to its enclosing function/class scope
        scopes = set()
        for line_idx in match_lines:
            scope_start = self._find_enclosing_scope(lines, line_idx)
            scope_end = self._find_scope_end(lines, scope_start)
            scopes.update(range(scope_start, min(scope_end + 1, len(lines))))

        # Build output within budget
        sorted_lines = sorted(scopes)
        parts = []
        total = 0
        prev = -2
        for idx in sorted_lines:
            if idx > prev + 1:
                marker = f"\n--- {file_path}:{idx + 1} ---\n"
                if total + len(marker) > budget_chars:
                    break
                parts.append(marker)
                total += len(marker)
            line_text = redact_text(lines[idx])
            if total + len(line_text) + 1 > budget_chars:
                break
            parts.append(line_text)
            total += len(line_text) + 1
            prev = idx

        return "\n".join(parts)

    def format_for_prompt(
        self,
        matches: list[GrepMatch],
        max_chars: int = 3000,
    ) -> str:
        """Format grep matches into a compact prompt-ready string.

        Each match block shows the file path and line number as a header,
        context lines indented with two spaces, and the matched line prefixed with ``>``.
        Blocks are included until the character budget is exhausted.

        Args:
            matches: GrepMatch objects to format, typically from ``extract_patterns()``.
            max_chars: Maximum total character count of the output.

        Returns:
            Newline-joined string of all match blocks that fit within the budget.
        """
        parts = []
        total = 0
        for raw_match in matches:
            m = raw_match.redacted()
            block = f"--- {m.file_path}:{m.line_number} ---\n"
            for ctx in m.context_before:
                block += f"  {ctx}\n"
            block += f"> {m.line_content}\n"
            for ctx in m.context_after:
                block += f"  {ctx}\n"
            if total + len(block) > max_chars:
                break
            parts.append(block)
            total += len(block)
        return "\n".join(parts)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _find_enclosing_scope(lines: list[str], line_idx: int) -> int:
        """Scan backwards to find the enclosing def/class."""
        for i in range(line_idx, -1, -1):
            stripped = lines[i].lstrip()
            if stripped.startswith(("def ", "class ", "async def ")):
                return i
        return max(0, line_idx - 3)

    @staticmethod
    def _find_scope_end(lines: list[str], scope_start: int) -> int:
        """Find end of a scope block by indentation."""
        if scope_start >= len(lines):
            return scope_start
        base_indent = len(lines[scope_start]) - len(lines[scope_start].lstrip())
        for i in range(scope_start + 1, min(scope_start + 60, len(lines))):
            stripped = lines[i].strip()
            if (
                stripped
                and (len(lines[i]) - len(lines[i].lstrip())) <= base_indent
                and not stripped.startswith(("#", '"""', "'''", "@", ")"))
            ):
                return i - 1
        return min(scope_start + 30, len(lines) - 1)

    def _rg_extract(
        self,
        file_paths: list[str],
        patterns: list[str],
        context_lines: int,
        max_matches: int,
    ) -> list[GrepMatch]:
        """Ripgrep-based extraction."""
        pattern = "|".join(patterns)
        cmd = [
            self._rg_path,
            "-n",
            f"-C{context_lines}",
            "-m",
            str(max_matches),
            "--no-heading",
            "--with-filename",
            "-e",
            pattern,
            *file_paths,
        ]
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=GREP_TIMEOUT,
            )
            return self._parse_rg_output(result.stdout, context_lines)
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
            logger.warning("Ripgrep failed, falling back to Python: %s", e)
            return self._python_extract(file_paths, patterns, context_lines, max_matches)

    @staticmethod
    def _parse_rg_output(output: str, context_lines: int) -> list[GrepMatch]:
        """Parse ripgrep output into GrepMatch objects."""
        matches = []
        if not output:
            return matches
        current_match = None
        for line in output.splitlines():
            # Match lines: file:line:content or file-line-content (context)
            m = _RG_LINE_RE.match(line)
            if not m:
                if line == "--" and current_match:  # separator between match groups
                    matches.append(current_match)
                    current_match = None
                continue
            filepath, lineno, content = m.group(1), int(m.group(2)), m.group(3)
            is_match = ":" in line[len(filepath) :][:3]  # rough heuristic for : vs -
            if is_match and (current_match is None or current_match.line_number != lineno):
                if current_match:
                    matches.append(current_match)
                current_match = GrepMatch(
                    file_path=filepath,
                    line_number=lineno,
                    line_content=content,
                )
            elif current_match:
                if lineno < current_match.line_number:
                    current_match.context_before.append(content)
                else:
                    current_match.context_after.append(content)
        if current_match:
            matches.append(current_match)
        return matches

    @staticmethod
    def _python_extract(
        file_paths: list[str],
        patterns: list[str],
        context_lines: int,
        max_matches: int,
    ) -> list[GrepMatch]:
        """Fallback for systems without ripgrep."""
        combined = _compile_patterns_union("|".join(patterns))
        matches = []
        for fp in file_paths:
            try:
                lines = Path(fp).read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                logger.warning("Could not read file %s for grep — skipping", fp)
                continue
            for i, line in enumerate(lines):
                if combined.search(line):
                    matches.append(
                        GrepMatch(
                            file_path=fp,
                            line_number=i + 1,
                            line_content=line,
                            context_before=lines[max(0, i - context_lines) : i],
                            context_after=lines[i + 1 : i + 1 + context_lines],
                        ),
                    )
                    if len(matches) >= max_matches:
                        return matches
        return matches


# Convenience singleton
_grep_context: GrepContext | None = None
_grep_context_lock = threading.Lock()


def get_grep_context() -> GrepContext:
    """Return the singleton GrepContext instance, creating it on first call.

    Returns:
        The shared GrepContext, which auto-detects ripgrep availability on creation.
    """
    global _grep_context
    if _grep_context is None:
        with _grep_context_lock:
            if _grep_context is None:
                _grep_context = GrepContext()
    return _grep_context
