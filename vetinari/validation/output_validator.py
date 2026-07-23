"""Lightweight output validation helpers."""

from __future__ import annotations

import ast
import json
import logging
import re

from vetinari.security.fail_closed import UntrustedInputError, sanitize_untrusted_text

logger = logging.getLogger(__name__)


class Validator:
    """Validates agent outputs against quality and safety rules.

    Provides quick heuristic checks for text, JSON, and Python code outputs.
    For thorough verification use the verification pipeline.
    """

    def is_valid_text(self, text: str) -> bool:
        """Check whether *text* is non-empty and syntactically plausible.

        Returns ``True`` for valid JSON, syntactically correct Python code,
        or any non-empty text string.

        Returns:
            True when the text passes lightweight validation, otherwise False.
        """
        if not text or len(text.strip()) == 0:
            return False

        try:
            json.loads(text)
            return True
        except (json.JSONDecodeError, TypeError, ValueError):
            logging.getLogger(__name__).debug("suppressed non-fatal exception", exc_info=True)

        if self._looks_like_code(text):
            return self._validate_python_code(text)

        try:
            sanitize_untrusted_text(text)
        except UntrustedInputError:
            logger.warning("Text validation rejected prompt-control or unsafe text")
            return False
        return len(text.strip()) > 0

    @staticmethod
    def _looks_like_code(text: str) -> bool:
        cleaned = re.sub(r"```[\w]*\n?", "", text)
        cleaned = cleaned.strip()
        return bool(re.search(r"^(def|class|import|from|@|#|async|with|\s+=\s+)", cleaned, re.MULTILINE))

    @staticmethod
    def _validate_python_code(code: str) -> bool:
        cleaned = re.sub(r"```[\w]*\n", "\n", code)
        cleaned = re.sub(r"```$", "", cleaned)
        cleaned = cleaned.strip()

        if cleaned.startswith("{") and cleaned.endswith("}"):
            inner = cleaned[1:-1].strip()
            if not re.search(r"^(def|class|import|from|@|async|with|\s+=)", inner, re.MULTILINE):
                cleaned = inner

        try:
            ast.parse(cleaned)
            return True
        except (SyntaxError, ValueError):
            logger.warning("Code syntax check found errors; marking as invalid")
            return False


__all__ = ["Validator"]
