# Copyright (c) 2026 Vetinari contributors.
"""Static Verifier - Tier 1 of the verification cascade.

Performs fast, deterministic checks on task output without any model calls:
  - Python syntax validity
  - Banned import detection
  - Hardcoded credential patterns
  - Code block presence when code is expected

Pipeline role: Called first by CascadeOrchestrator. When static checks pass,
the cascade can skip Tier 2 (entailment) and Tier 3 (LLM) for simple tasks.
"""

from __future__ import annotations

import ast
import logging
import re
from dataclasses import dataclass

logger = logging.getLogger(__name__)

DEVELOPER_WORKFLOW_CONTRACT_ID = "wave3-rcg0014p06-scope-followup-P09"
STATIC_VERIFIER_WORKFLOW_GUARDS: tuple[str, ...] = (
    "empty content cannot create a false failed static verdict",
    "banned imports are detected through AST parsing or regex fallback",
    "hardcoded credential patterns return failed checks",
    "code-presence checks reject prose-only fenced blocks for code tasks",
)


def developer_workflow_contract() -> dict[str, object]:
    """Return static-verifier workflow guarantees verified by this follow-up pack."""
    return {
        "pack": DEVELOPER_WORKFLOW_CONTRACT_ID,
        "surface": "vetinari/validation/static_verifier.py",
        "guards": STATIC_VERIFIER_WORKFLOW_GUARDS,
    }


# Patterns that indicate hardcoded credentials — should never appear in output
_CREDENTIAL_PATTERNS = [
    re.compile(r'password\s*=\s*["\'][^"\']{4,}["\']', re.IGNORECASE),
    re.compile(r'api_key\s*=\s*["\'][^"\']{8,}["\']', re.IGNORECASE),
    re.compile(r'secret\s*=\s*["\'][^"\']{8,}["\']', re.IGNORECASE),
    re.compile(r'token\s*=\s*["\'][^"\']{8,}["\']', re.IGNORECASE),
]

# Modules that are always unsafe to import in generated code
_BANNED_IMPORTS = frozenset({"ctypes", "importlib", "mmap", "msvcrt", "os", "socket", "subprocess", "winreg"})


@dataclass(frozen=True, slots=True)
class StaticCheckResult:
    """Result of a single static verification check.

    Attributes:
        passed: Whether the check passed.
        check_name: Short identifier for this check.
        finding: Human-readable description of the finding if the check failed,
            empty string when passed.
    """

    passed: bool
    check_name: str
    finding: str = ""

    def __repr__(self) -> str:
        """Show key fields for debugging.

        Returns:
            Compact representation containing check name and pass status.
        """
        return f"StaticCheckResult(check_name={self.check_name!r}, passed={self.passed!r})"


class StaticVerifier:
    r"""Tier 1 verifier — deterministic checks with zero model calls.

    Runs a battery of cheap, rule-based checks on the output text.  All checks
    are independent and run to completion; individual failures are collected so
    the caller knows exactly which rules were violated.

    Example::

        verifier = StaticVerifier()
        results = verifier.verify("def add(a, b):\n    return a + b\n")
        passed = all(r.passed for r in results)
    """

    def verify(self, content: str, task_description: str = "") -> list[StaticCheckResult]:
        """Run all static checks on *content*.

        Args:
            content: The text or code to check.
            task_description: Optional task description used to decide whether
                a code block is expected (improves code-presence check accuracy).

        Returns:
            List of :class:`StaticCheckResult` — one per check.  An empty list
            is returned for empty or non-string content (all checks skipped).
        """
        if not isinstance(content, str) or not content.strip():
            return []

        results: list[StaticCheckResult] = [
            self._check_syntax(content),
            self._check_banned_imports(content),
            self._check_credentials(content),
            self._check_code_presence(content, task_description),
        ]
        failed = [r for r in results if not r.passed]
        if failed:
            logger.debug(
                "StaticVerifier: %d/%d checks failed: %s",
                len(failed),
                len(results),
                [r.check_name for r in failed],
            )
        return results

    # ── individual checks ────────────────────────────────────────────────────

    @staticmethod
    def _check_syntax(content: str) -> StaticCheckResult:
        """Return a passing result when *content* contains no Python code.

        Also passes when the Python code it contains parses without a SyntaxError.

        Returns:
            Syntax check result.
        """
        # Strip markdown fences before parsing
        cleaned = re.sub(r"```[\w]*\n?", "\n", content)
        cleaned = re.sub(r"```\s*$", "", cleaned).strip()

        # Only run AST parse if the cleaned text looks like Python.
        # Recognise a broader set of constructs beyond def/class/import so that
        # loops, assignments, return statements, and context managers are caught.
        python_constructs = re.compile(
            r"^\s*(def|class|import|from|@|async\s+def|for\s|while\s|with\s|return\s|\w+\s*=)",
            re.MULTILINE,
        )
        if not python_constructs.search(cleaned):
            return StaticCheckResult(passed=True, check_name="syntax")

        try:
            ast.parse(cleaned)
            return StaticCheckResult(passed=True, check_name="syntax")
        except SyntaxError as exc:
            logger.warning("StaticVerifier: syntax check failed at line %s: %s", exc.lineno, exc.msg)
            return StaticCheckResult(
                passed=False,
                check_name="syntax",
                finding=f"SyntaxError at line {exc.lineno}: {exc.msg}",
            )

    @staticmethod
    def _check_banned_imports(content: str) -> StaticCheckResult:
        """Fail when *content* imports a module from the banned list.

        Returns:
            Banned-import check result.
        """
        try:
            tree = ast.parse(_strip_markdown_fences(content))
        except SyntaxError:
            imported_roots = _extract_import_roots_fallback(content)
        else:
            imported_roots: set[str] = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported_roots.update(alias.name.split(".")[0] for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported_roots.add(node.module.split(".")[0])

        for module_root in sorted(imported_roots):
            if module_root in _BANNED_IMPORTS:
                return StaticCheckResult(
                    passed=False,
                    check_name="banned_imports",
                    finding=f"Banned module imported: {module_root}",
                )
        return StaticCheckResult(passed=True, check_name="banned_imports")

    @staticmethod
    def _check_credentials(content: str) -> StaticCheckResult:
        """Fail when *content* contains a hardcoded credential pattern.

        Returns:
            Credential-pattern check result.
        """
        for pattern in _CREDENTIAL_PATTERNS:
            match = pattern.search(content)
            if match:
                # Redact the value before logging
                snippet = match.group(0)[:40]
                return StaticCheckResult(
                    passed=False,
                    check_name="credentials",
                    finding=f"Potential hardcoded credential: {snippet!r}",
                )
        return StaticCheckResult(passed=True, check_name="credentials")

    @staticmethod
    def _check_code_presence(content: str, task_description: str) -> StaticCheckResult:
        """Warn when the task asks for code but the response contains none.

        Returns:
            Code-presence check result.
        """
        if not task_description:
            return StaticCheckResult(passed=True, check_name="code_presence")

        desc_lower = task_description.lower()
        task_wants_code = any(
            kw in desc_lower for kw in ("implement", "write", "create", "build", "function", "class", "code", "def ")
        )
        if not task_wants_code:
            return StaticCheckResult(passed=True, check_name="code_presence")

        # A fenced block counts as code only when it has a recognized code language
        # tag or contains Python syntax.  Plain ``` or ```text/```markdown blocks
        # are prose, not code, so they must NOT satisfy code presence alone.
        python_constructs_bare = re.compile(
            r"^\s*(def|class|import|from|for\s|while\s|with\s|return\s|\w+\s*=)",
            re.MULTILINE,
        )
        has_fenced_code = bool(re.search(r"```(?:python|py|sh|bash|javascript|js|ts|typescript|java|cpp|c\b)", content))
        has_bare_python = bool(python_constructs_bare.search(content))
        has_code = has_fenced_code or has_bare_python
        if has_code:
            return StaticCheckResult(passed=True, check_name="code_presence")

        return StaticCheckResult(
            passed=False,
            check_name="code_presence",
            finding="Task requested code but response contains no code block or function definition",
        )


def _strip_markdown_fences(content: str) -> str:
    cleaned = re.sub(r"```[\w]*\n?", "\n", content)
    return re.sub(r"```\s*$", "", cleaned).strip()


def _extract_import_roots_fallback(content: str) -> set[str]:
    roots: set[str] = set()
    import_re = re.compile(r"^\s*import\s+(.+)$", re.MULTILINE)
    from_re = re.compile(r"^\s*from\s+([\w.]+)\s+import\b", re.MULTILINE)
    for match in import_re.finditer(content):
        for part in match.group(1).split(","):
            name = part.strip().split(" as ", 1)[0].strip()
            if name:
                roots.add(name.split(".")[0])
    roots.update(match.group(1).split(".")[0] for match in from_re.finditer(content))
    return roots
