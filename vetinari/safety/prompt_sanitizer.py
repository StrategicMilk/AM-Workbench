"""Prompt injection protection — sanitizes untrusted text before it reaches LLM prompts.

Untrusted content enters the pipeline from two sources:
1. Task descriptions provided by users or upstream systems.
2. Worker agent output that the Inspector evaluates.

Both sources can contain adversarial phrases designed to override the agent's
instructions (e.g., "IGNORE PREVIOUS INSTRUCTIONS"). This module provides
deterministic sanitization for both cases before the text is interpolated into
system or user prompts.

This is a security module in the pipeline: it runs before LLM inference, not
after. It does NOT attempt semantic analysis — that is intentionally left to the
LLM and the guardrails layer. This module handles the deterministic, pattern-based
threat class only.

This is step 0 of the pipeline: **Sanitization** → Intake → Planning → Execution → Quality Gate → Assembly.
"""

from __future__ import annotations

import hashlib
import logging
import re

from vetinari.security.redaction import redact_text

logger = logging.getLogger(__name__)


# -- Injection prefix patterns -------------------------------------------------
# Ordered by specificity: more specific patterns first so they match before
# their prefixes do. All patterns are case-insensitive. These cover the most
# common prompt injection vectors; they are not exhaustive. Novel injections
# that don't match these patterns will still reach the LLM but are mitigated
# by the explicit delimiter wrapping applied to all untrusted content.
_INJECTION_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"ignore\s+(all\s+)?previous\s+instructions?", re.IGNORECASE),
    re.compile(r"disregard\s+(all\s+)?previous\s+instructions?", re.IGNORECASE),
    re.compile(r"forget\s+(all\s+)?previous\s+instructions?", re.IGNORECASE),
    re.compile(r"override\s+instructions?", re.IGNORECASE),
    re.compile(r"new\s+instructions?:", re.IGNORECASE),
    re.compile(r"^system\s*:", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^human\s*:", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^assistant\s*:", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^user\s*:", re.IGNORECASE | re.MULTILINE),
    re.compile(r"```\s*system\b", re.IGNORECASE),
    re.compile(r"###\s*SYSTEM###", re.IGNORECASE),
    re.compile(r"<\|im_start\|>", re.IGNORECASE),
    re.compile(r"<\|im_end\|>", re.IGNORECASE),
    re.compile(r"\[INST\]", re.IGNORECASE),
    re.compile(r"\[/INST\]", re.IGNORECASE),
]

# Delimiter pair used to clearly mark untrusted content boundaries in prompts.
# The opening tag includes a warning to the model; the closing tag is a plain
# boundary marker. Both are chosen to be unlikely to appear in legitimate content.
_UNTRUSTED_OPEN = "<<<UNTRUSTED_USER_CONTENT_BEGIN>>>"
_UNTRUSTED_CLOSE = "<<<UNTRUSTED_USER_CONTENT_END>>>"


def sanitize_task_description(text: str, *, is_code: bool = False) -> str:
    """Sanitize a task description before it is embedded in an agent prompt.

    Strips known injection prefixes and wraps the result in explicit delimiters
    that mark the content as user-provided data. Legitimate task descriptions
    pass through unchanged (modulo whitespace normalisation of matched segments).

    Code content (e.g. ``task.context["code"]``) receives lighter sanitization:
    role-prefix patterns (``system:``, ``assistant:``) are still stripped because
    they are not valid Python/JS/etc., but code-fencing injection attempts
    (````` ```system `````) are preserved so the Inspector can evaluate the
    suspicious code. Set ``is_code=True`` for code content.

    Args:
        text: The untrusted input string.
        is_code: When True, only strip role-override patterns, not code-fence
            injection markers. Defaults to False.

    Returns:
        Sanitized string wrapped in ``<<<UNTRUSTED_USER_CONTENT_BEGIN>>>`` /
        ``<<<UNTRUSTED_USER_CONTENT_END>>>`` delimiters.

    Note:
        FSA-0346 delimiter collision: if input content already contains the
        delimiter byte sequences, the sanitizer strips those inner markers
        before wrapping. This preserves one trusted outer ADR-0097 boundary
        while accepting that upstream preview truncation is the remaining
        collision-risk limiter.
    """
    if not text:
        return text

    cleaned = text
    patterns_to_apply = _INJECTION_PATTERNS if not is_code else _get_role_only_patterns()

    for pattern in patterns_to_apply:
        if pattern.search(cleaned):
            logger.warning(
                "Prompt injection attempt detected and stripped — pattern=%r text_sha256=%s text_chars=%d",
                pattern.pattern,
                hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest(),
                len(text),
            )
            # Replace the matched phrase with a neutral placeholder that preserves
            # sentence structure so downstream parsing is not confused.
            cleaned = pattern.sub("[REDACTED]", cleaned)

    cleaned = cleaned.replace(_UNTRUSTED_OPEN, "").replace(_UNTRUSTED_CLOSE, "")
    cleaned = redact_text(cleaned)
    return f"{_UNTRUSTED_OPEN}\n{cleaned}\n{_UNTRUSTED_CLOSE}"


def sanitize_worker_output(text: str) -> str:
    """Sanitize Worker agent output before the Inspector embeds it in a prompt.

    Worker output is code or prose produced by the Worker agent. It reaches the
    Inspector as the subject of evaluation. If the Worker's LLM was itself
    manipulated, or if the Worker processed untrusted user content, its output
    may contain injection payloads aimed at the Inspector.

    This function strips role-override injection markers but intentionally
    preserves code content (injection-looking strings inside code blocks are
    legitimate code the Inspector must evaluate). The content is then wrapped in
    ``<<<UNTRUSTED_USER_CONTENT_BEGIN>>>`` / ``<<<UNTRUSTED_USER_CONTENT_END>>>``
    delimiters so the Inspector's LLM knows it is evaluating external content,
    not receiving additional instructions.

    Args:
        text: Worker output string (code, prose, or structured data as text).

    Returns:
        Sanitized string wrapped in untrusted content delimiters.
    """
    return sanitize_task_description(text, is_code=True)


def _get_role_only_patterns() -> list[re.Pattern[str]]:
    """Return the subset of injection patterns that target role-override prefixes.

    Code content may legitimately contain phrases like ````` ```system ````` as
    part of a code fence. Those are kept intact so the Inspector can evaluate them.
    This lighter set only removes chat-role override tokens that have no valid
    purpose in code.

    Returns:
        List of compiled regex patterns covering role-override injection only.
    """
    # Indices into _INJECTION_PATTERNS that are role-prefix patterns, safe to
    # apply even inside code. The remaining patterns (ignore-prev-instructions,
    # code-fence injection, special tokens) are skipped for code content.
    role_prefix_indices = {5, 6, 7, 8, 11, 12, 13, 14}
    return [p for i, p in enumerate(_INJECTION_PATTERNS) if i in role_prefix_indices]


def is_content_delimited(text: str) -> bool:
    """Check whether text has already been wrapped in untrusted content delimiters.

    Used by callers to avoid double-wrapping content that passed through
    sanitization earlier in the pipeline.

    Args:
        text: Text to inspect.

    Returns:
        True if both opening and closing delimiters are present.
    """
    return _UNTRUSTED_OPEN in text and _UNTRUSTED_CLOSE in text
