"""Code-execution guardrails for sandboxed snippets."""

from __future__ import annotations

import ast
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

_BLOCKED_MODULES = frozenset({
    "os",
    "subprocess",
    "sys",
    "ctypes",
    "socket",
    "importlib",
    "builtins",
    "pty",
    "multiprocessing",
    "pickle",
    "shelve",
})
_BLOCKED_BUILTINS = frozenset({"eval", "exec", "compile", "__import__"})
_BLOCKED_REFLECTION_BUILTINS = frozenset({"getattr", "globals", "locals", "vars"})
_BLOCKED_ATTRIBUTES = frozenset({("os", "system"), ("subprocess", "run"), ("subprocess", "Popen")})


@dataclass(frozen=True, slots=True)
class CodeGuardrailResult:
    """Guardrail decision result."""

    passed: bool
    reason: str = ""

    @property
    def is_safe(self) -> bool:
        """Return whether this result permits execution."""
        return self.passed


class CodeExecutionGuardrail:
    """Fail-closed code-execution scanner wrapper."""

    def __init__(self) -> None:
        try:
            self._scanner = self._build_scanner()
            self._scanner_error = ""
        except Exception as exc:
            self._scanner = None
            self._scanner_error = str(exc)

    @staticmethod
    def _build_scanner() -> object:
        """Build the underlying scanner object.

        Returns:
            Scanner used by tests and lightweight runtime checks.
        """
        return _AstCodeScanner()

    def check(self, code: str) -> CodeGuardrailResult:
        """Check whether code is safe enough to execute.

        Args:
            code: Python code snippet.

        Returns:
            GuardrailResult that fails closed on dangerous tokens.
        """
        if self._scanner is None:
            return CodeGuardrailResult(False, self._scanner_error or "scanner unavailable")
        check = getattr(self._scanner, "check", None)
        if not callable(check):
            return CodeGuardrailResult(False, "scanner unavailable")
        reason = check(code)
        if reason:
            return CodeGuardrailResult(False, reason)
        if "rm -rf" in code.lower():
            return CodeGuardrailResult(False, "dangerous shell deletion token")
        return CodeGuardrailResult(True, "")


class _AstCodeScanner:
    """Small AST-backed scanner for high-risk execution primitives."""

    def check(self, code: str) -> str:
        """Return a block reason for unsafe code.

        Returns:
            Empty string when code passes the scanner; otherwise a bounded
            reason describing why execution is blocked.
        """
        try:
            tree = ast.parse(code)
        except SyntaxError as exc:
            logger.warning("code guardrail rejected invalid Python syntax", exc_info=True)
            return f"code syntax error: {exc.msg}"

        aliases: dict[str, str] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root = alias.name.split(".", 1)[0]
                    if root in _BLOCKED_MODULES:
                        return f"blocked import: {root}"
                    aliases[alias.asname or root] = root
            elif isinstance(node, ast.ImportFrom):
                root = (node.module or "").split(".", 1)[0]
                if root in _BLOCKED_MODULES:
                    return f"blocked import: {root}"
                for alias in node.names:
                    aliases[alias.asname or alias.name] = root
            elif isinstance(node, ast.Call):
                name = _call_name(node.func, aliases)
                if name in _BLOCKED_BUILTINS:
                    return f"blocked builtin: {name}"
                if name in _BLOCKED_REFLECTION_BUILTINS:
                    return f"blocked reflection builtin: {name}"
                if "." in name:
                    module, attr = name.rsplit(".", 1)
                    if (module, attr) in _BLOCKED_ATTRIBUTES:
                        return f"blocked call: {module}.{attr}"
        return ""


def _call_name(node: ast.AST, aliases: dict[str, str]) -> str:
    if isinstance(node, ast.Name):
        return aliases.get(node.id, node.id)
    if isinstance(node, ast.Attribute):
        parent = _call_name(node.value, aliases)
        return f"{parent}.{node.attr}" if parent else node.attr
    return ""
