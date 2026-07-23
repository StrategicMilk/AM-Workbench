"""Grammar library for constrained generation via llama.cpp GBNF format.

Pre-built GBNF grammars for common structured output formats, plus helpers for
auto-selecting grammars by task type, validating BNF, and truncating generated
text at grammar-valid boundaries (BudgetForcing-inspired).

This is step 2.5 of the inference pipeline: after a request is built but before
it is dispatched to llama-cpp-python, the adapter consults this module to attach
the right grammar when the caller has set task_type but not grammar explicitly.
"""

from __future__ import annotations

import logging
import re

from vetinari.context.window_manager import count_tokens

logger = logging.getLogger(__name__)


# -- Grammar format constants -------------------------------------------------

# Minimum valid grammar: at least one rule definition of the form `name ::= ...`
_RULE_PATTERN = re.compile(r"^\s*\w[\w-]*\s*::=", re.MULTILINE)

# -- GBNF Grammar definitions -------------------------------------------------
# All grammars use llama.cpp's GBNF dialect.
# Reference: https://github.com/ggerganov/llama.cpp/blob/master/grammars/README.md

# JSON object grammar: matches any JSON object { "key": value, ... }
_JSON_OBJECT_GRAMMAR = r"""
root   ::= "{" ws members ws "}"
members ::= pair ("," ws pair)*
pair   ::= string ws ":" ws value
value  ::= object | array | string | number | ("true" | "false" | "null")
object ::= "{" ws members ws "}" | "{" ws "}"
array  ::= "[" ws elements ws "]" | "[" ws "]"
elements ::= value ("," ws value)*
string ::= "\"" ([^"\\] | "\\" .)* "\""
number ::= "-"? ([0-9] | [1-9][0-9]*) ("." [0-9]+)? (([eE] [+-]? [0-9]+))?
ws     ::= [ \t\n\r]*
"""

# JSON array grammar: matches any JSON array [ value, ... ]
_JSON_ARRAY_GRAMMAR = r"""
root     ::= "[" ws elements ws "]" | "[" ws "]"
elements ::= value ("," ws value)*
value    ::= object | array | string | number | ("true" | "false" | "null")
object   ::= "{" ws members ws "}" | "{" ws "}"
members  ::= pair ("," ws pair)*
pair     ::= string ws ":" ws value
array    ::= "[" ws elements ws "]" | "[" ws "]"
string   ::= "\"" ([^"\\] | "\\" .)* "\""
number   ::= "-"? ([0-9] | [1-9][0-9]*) ("." [0-9]+)? (([eE] [+-]? [0-9]+))?
ws       ::= [ \t\n\r]*
"""

# Simplified YAML key-value document grammar.
# Targets the subset: key: value pairs, one per line, with optional nesting via indentation.
_YAML_DOCUMENT_GRAMMAR = r"""
root      ::= doc-line+
doc-line  ::= key-line | blank-line
key-line  ::= indent key ws ":" ws value newline
blank-line ::= ws newline
key       ::= [a-zA-Z_][a-zA-Z0-9_-]*
value     ::= scalar | multiline-indicator
scalar    ::= [^\n]*
multiline-indicator ::= ("|" | ">") [^\n]*
indent    ::= [ ]*
ws        ::= [ \t]*
newline   ::= "\n"
"""

# Structured review grammar: JSON object with score, findings array, and summary.
# Used by the Inspector agent when producing quality review outputs.
_STRUCTURED_REVIEW_GRAMMAR = r"""
root      ::= "{" ws "\"score\"" ws ":" ws number ws "," ws "\"findings\"" ws ":" ws findings ws "," ws "\"summary\"" ws ":" ws string ws "}"
findings  ::= "[" ws finding-list ws "]" | "[" ws "]"
finding-list ::= finding ("," ws finding)*
finding   ::= "{" ws "\"severity\"" ws ":" ws severity ws "," ws "\"message\"" ws ":" ws string ws "}"
severity  ::= "\"critical\"" | "\"high\"" | "\"medium\"" | "\"low\"" | "\"info\""
number    ::= "-"? ([0-9] | [1-9][0-9]*) ("." [0-9]+)?
string    ::= "\"" ([^"\\] | "\\" .)* "\""
ws        ::= [ \t\n\r]*
"""

# Plan DAG grammar: JSON object with tasks array, each task having id, description, dependencies.
# Used by the Foreman/Planner agent when producing structured execution plans.
_PLAN_DAG_GRAMMAR = r"""
root         ::= "{" ws "\"tasks\"" ws ":" ws tasks ws "}"
tasks        ::= "[" ws task-list ws "]" | "[" ws "]"
task-list    ::= task ("," ws task)*
task         ::= "{" ws "\"id\"" ws ":" ws string ws "," ws "\"description\"" ws ":" ws string ws "," ws "\"dependencies\"" ws ":" ws dep-list ws "}"
dep-list     ::= "[" ws string-list ws "]" | "[" ws "]"
string-list  ::= string ("," ws string)*
string       ::= "\"" ([^"\\] | "\\" .)* "\""
ws           ::= [ \t\n\r]*
"""

# -- Truncation boundary sets (used inside truncate_at_grammar_boundary) -----

# Grammar names that produce JSON object output (truncate at last `}`)
_JSON_OBJECT_GRAMMAR_NAMES: frozenset[str] = frozenset({"json_object", "structured_review", "plan_dag"})

# Grammar names that produce JSON array output (truncate at last `]`)
_JSON_ARRAY_GRAMMAR_NAMES: frozenset[str] = frozenset({"json_array"})

# -- Module-level registry ----------------------------------------------------

GRAMMAR_LIBRARY: dict[str, str] = {
    "json_object": _JSON_OBJECT_GRAMMAR.strip(),
    "json_array": _JSON_ARRAY_GRAMMAR.strip(),
    "yaml_document": _YAML_DOCUMENT_GRAMMAR.strip(),
    "structured_review": _STRUCTURED_REVIEW_GRAMMAR.strip(),
    "plan_dag": _PLAN_DAG_GRAMMAR.strip(),
}
"""Mapping from grammar name to GBNF grammar string.

Keys are stable identifiers used in ``TASK_TYPE_TO_GRAMMAR`` and returned
by ``get_grammar``. Values are ready-to-use GBNF strings for llama-cpp-python.
"""

TASK_TYPE_TO_GRAMMAR: dict[str, str] = {
    # Generic JSON tasks
    "json": "json_object",
    "json_object": "json_object",
    "json_array": "json_array",
    # Planning / orchestration
    "plan": "plan_dag",
    "planning": "plan_dag",
    "plan_dag": "plan_dag",
    # Quality review / inspection
    "review": "structured_review",
    "inspection": "structured_review",
    "quality_review": "structured_review",
    "structured_review": "structured_review",
    # YAML configuration
    "yaml": "yaml_document",
    "yaml_document": "yaml_document",
    "config": "yaml_document",
}
"""Mapping from task_type string to grammar name in ``GRAMMAR_LIBRARY``.

The mapping is intentional and explicit — adding a new task type here is the
only change required to enable grammar enforcement for that type.  The grammar
name MUST already exist as a key in ``GRAMMAR_LIBRARY``.
"""


# -- Public API ---------------------------------------------------------------


def get_grammar(name: str) -> str | None:
    """Look up a grammar string by its registry name.

    Args:
        name: Grammar name key, e.g. ``"json_object"`` or ``"plan_dag"``.

    Returns:
        GBNF grammar string if found, ``None`` otherwise.
    """
    grammar = GRAMMAR_LIBRARY.get(name)
    if grammar is None:
        logger.debug("Grammar '%s' not found in library — returning None", name)
    return grammar


def get_grammar_for_task_type(task_type: str) -> str | None:
    """Return the appropriate grammar for a given task type string.

    Consults ``TASK_TYPE_TO_GRAMMAR`` to find the grammar name, then looks it
    up in ``GRAMMAR_LIBRARY``.  Returns ``None`` if the task type is not mapped
    or the mapped grammar does not exist.

    Args:
        task_type: Task type identifier, e.g. ``"plan"``, ``"review"``, ``"json"``.

    Returns:
        GBNF grammar string if a mapping exists, ``None`` otherwise.
    """
    grammar_name = TASK_TYPE_TO_GRAMMAR.get(task_type)
    if grammar_name is None:
        logger.debug("No grammar mapping for task_type='%s' — proceeding without grammar", task_type)
        return None
    grammar = get_grammar(grammar_name)
    if grammar is None:
        logger.warning(
            "task_type='%s' maps to grammar='%s' but that grammar does not exist in the library",
            task_type,
            grammar_name,
        )
    return grammar


def validate_grammar(grammar: str) -> bool:
    """Check that a grammar string is plausibly valid GBNF.

    Performs lightweight structural checks:
    1. The string must be non-empty after stripping whitespace.
    2. At least one rule definition of the form ``name ::= ...`` must exist.
    3. Opening and closing brackets (``[``, ``{``, ``(``) must be balanced.

    This is a best-effort pre-flight check — it catches obvious malformed grammars
    before they are handed to llama-cpp-python where errors are harder to diagnose.
    It does NOT fully parse the GBNF specification.

    Args:
        grammar: GBNF grammar string to validate.

    Returns:
        ``True`` if the grammar passes all structural checks, ``False`` otherwise.
    """
    if not grammar or not grammar.strip():
        logger.debug("Grammar validation failed: empty grammar string")
        return False

    if not _RULE_PATTERN.search(grammar):
        logger.debug("Grammar validation failed: no rule definitions (name ::= ...) found")
        return False

    if not _brackets_balanced(grammar):
        logger.debug("Grammar validation failed: unbalanced brackets")
        return False

    return True


def _log_truncation(kind: str, original_len: int, result_len: int) -> None:
    """Emit the common grammar truncation debug event."""
    logger.debug(
        "truncate_at_grammar_boundary: %s truncated from %d to %d chars",
        kind,
        original_len,
        result_len,
    )


def _truncate_json_boundary(text: str, truncated: str, marker: str, label: str) -> str | None:
    """Return text truncated at the last complete JSON boundary if present."""
    boundary = _rfind_outside_strings(truncated, marker)
    if boundary < 0:
        return None
    result = truncated[: boundary + 1]
    _log_truncation(label, len(text), len(result))
    return result


def _truncate_yaml_boundary(text: str, truncated: str) -> str | None:
    """Return text truncated at the final complete YAML line if present."""
    last_newline = truncated.rfind("\n")
    if last_newline < 0:
        return None
    result = truncated[: last_newline + 1]
    _log_truncation("YAML", len(text), len(result))
    return result


def _truncate_plain_boundary(text: str, truncated: str) -> str:
    """Return plain text truncated at a sentence, newline, or hard boundary."""
    last_period = truncated.rfind(". ")
    if last_period >= 0:
        result = truncated[: last_period + 1]
        logger.debug(
            "truncate_at_grammar_boundary: plain-text truncated at sentence boundary from %d to %d chars",
            len(text),
            len(result),
        )
        return result

    last_newline = truncated.rfind("\n")
    if last_newline >= 0:
        result = truncated[: last_newline + 1]
        logger.debug(
            "truncate_at_grammar_boundary: plain-text truncated at newline from %d to %d chars",
            len(text),
            len(result),
        )
        return result

    logger.debug(
        "truncate_at_grammar_boundary: hard-limit truncation from %d to %d chars",
        len(text),
        len(truncated),
    )
    return truncated


def truncate_at_grammar_boundary(
    text: str,
    max_tokens: int,
    grammar_name: str | None = None,
) -> str:
    """Truncate generated text at a grammar-valid boundary within a token budget.

    Inspired by BudgetForcing: when a response exceeds the allocated token
    budget, truncation happens at the nearest safe structural boundary rather
    than mid-token or mid-structure.  This avoids delivering partial JSON,
    half-written YAML keys, or truncated sentences.

    Boundary heuristics (applied in order of specificity):
    - JSON grammars (``json_object``, ``json_array``, ``structured_review``,
      ``plan_dag``): truncate at the last complete top-level ``}`` or ``]``.
    - YAML grammar (``yaml_document``): truncate at the last complete newline.
    - Unknown / ``None`` grammar: truncate at the last sentence boundary
      (period + space or end of line), then fall back to hard character limit.

    Args:
        text: Generated text to truncate.
        max_tokens: Maximum number of tokens allowed.
        grammar_name: Optional grammar name that was used to produce ``text``.
            Guides boundary selection. Pass ``None`` for plain-text outputs.

    Returns:
        Truncated string that fits within the token budget and ends at a
        structural boundary where possible.
    """
    if not text:
        return text

    if count_tokens(text) <= max_tokens:
        return text

    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if count_tokens(text[:mid]) <= max_tokens:
            lo = mid
        else:
            hi = mid - 1
    truncated = text[:lo]

    if grammar_name in _JSON_OBJECT_GRAMMAR_NAMES:
        result = _truncate_json_boundary(text, truncated, "}", "JSON")
        if result is not None:
            return result

    if grammar_name in _JSON_ARRAY_GRAMMAR_NAMES:
        result = _truncate_json_boundary(text, truncated, "]", "JSON array")
        if result is not None:
            return result

    if grammar_name == "yaml_document":
        result = _truncate_yaml_boundary(text, truncated)
        if result is not None:
            return result

    return _truncate_plain_boundary(text, truncated)


# -- Private helpers ----------------------------------------------------------


def _rfind_outside_strings(text: str, target: str) -> int:
    """Return the index of the last occurrence of ``target`` that is outside a JSON string literal.

    Uses an explicit state machine that tracks whether the current character is
    inside a double-quoted JSON string (flipping ``in_string`` on unescaped
    ``"``).  This prevents ``}`` or ``]`` characters that appear *inside* a
    string value (e.g. ``{"key": "value with {braces}"}`` ) from being
    mistaken as structural boundaries.

    Args:
        text: JSON text to scan (may be a truncated budget slice).
        target: Single character to find (``"}"`` or ``"]"``).

    Returns:
        Index of the last occurrence of ``target`` outside any string literal,
        or ``-1`` if not found.
    """
    in_string = False  # True while inside a JSON double-quoted string
    last_found = -1
    i = 0
    while i < len(text):
        char = text[i]
        if in_string:
            if char == "\\":
                # Escape sequence — skip the next character unconditionally
                i += 2
                continue
            if char == '"':
                in_string = False
        else:
            if char == '"':
                in_string = True
            elif char == target:
                last_found = i
        i += 1
    return last_found


def _brackets_balanced(text: str) -> bool:
    r"""Return True if round, square, and curly brackets in text are balanced.

    Tracks two GBNF lexical contexts to avoid false bracket mismatches:

    1. **Double-quoted string literals** (``"..."``) — brackets inside are
       literal terminal characters, not structural syntax.  Escape sequences
       (``\\``) inside strings are consumed without further processing.
    2. **Character classes** (``[...]``) — the ``[`` that opens a class is a
       structural bracket, but a ``"`` inside a class (e.g. ``[^"\\\\]``) is
       a literal character, not a string delimiter.  We do not start a string
       context while inside a character class.

    Args:
        text: GBNF grammar string to check for balanced brackets.

    Returns:
        ``True`` if all ``(``, ``[``, ``{`` have matching ``)``, ``]``, ``}``
        in the correct nesting order.
    """
    stack: list[str] = []
    matching: dict[str, str] = {")": "(", "]": "[", "}": "{"}
    in_string = False  # True while inside a GBNF double-quoted literal
    in_charclass = False  # True while inside a [...] character class
    i = 0
    while i < len(text):
        char = text[i]
        if in_string:
            if char == "\\":
                # Escape sequence — skip the next character unconditionally
                i += 2
                continue
            if char == '"':
                in_string = False
        elif in_charclass:
            # Inside [...]: only ] ends the class; no string context starts here
            if char == "]":
                in_charclass = False
                if not stack or stack[-1] != "[":
                    return False
                stack.pop()
        else:
            if char == '"':
                in_string = True
            elif char == "[":
                # Opening a character class — push to stack and enter class mode
                stack.append("[")
                in_charclass = True
            elif char in "({":
                stack.append(char)
            elif char in ")]}":
                if not stack or stack[-1] != matching[char]:
                    return False
                stack.pop()
        i += 1

    return len(stack) == 0
