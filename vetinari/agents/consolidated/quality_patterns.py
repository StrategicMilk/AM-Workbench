"""Security and quality heuristic patterns for the Inspector agent.

Houses the regex pattern tables and multi-perspective scan functions used
by InspectorAgent. Separating them here keeps quality_agent.py under the
550-line limit and makes the pattern library independently testable.

This is a pure-data / pure-function module with no side effects on import.
"""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

CompiledPattern = tuple[re.Pattern[str], str, str]


def _compile_patterns(patterns: list[tuple[str, str, str]], *, flags: int = 0) -> list[CompiledPattern]:
    compiled: list[CompiledPattern] = []
    for pattern, description, severity in patterns:
        try:
            compiled.append((re.compile(pattern, flags), description, severity))
        except re.error:
            logger.warning("Invalid regex pattern skipped at compile time: %s", pattern)
    return compiled


# ---------------------------------------------------------------------------
# Security heuristic patterns (preserved from SecurityAuditorAgent)
# ---------------------------------------------------------------------------

_SECURITY_PATTERNS: list[tuple[str, str, str]] = [
    # (pattern_regex, finding_name, severity)
    (r"subprocess\.(?:call|run|Popen)\(.*shell\s*=\s*True", "Shell injection risk", "HIGH"),
    (r"input\s*\(", "Unsanitized user input", "MEDIUM"),
    (r"password\s*=\s*['\"][^'\"]+['\"]", "Hardcoded password", "CRITICAL"),
    (r"api[_-]?key\s*=\s*['\"][^'\"]+['\"]", "Hardcoded API key", "CRITICAL"),
    (r"secret\s*=\s*['\"][^'\"]+['\"]", "Hardcoded secret", "CRITICAL"),
    (r"token\s*=\s*['\"][^'\"]+['\"]", "Hardcoded token", "HIGH"),
    (r"SELECT\s+.*\s+FROM\s+.*\+\s*", "Potential SQL injection", "HIGH"),
    (r"f['\"].*SELECT.*FROM.*\{", "Potential SQL injection (f-string)", "HIGH"),
    (r"\.format\(.*\).*SELECT", "Potential SQL injection (.format)", "HIGH"),
    (r"yaml\.load\((?!.*Loader)", "Unsafe YAML loading", "HIGH"),
    (r"marshal\.loads?\(", "Unsafe deserialization (marshal)", "HIGH"),
    (r"open\(.*['\"]w['\"].*\).*\n.*write\(.*input", "Write from user input", "MEDIUM"),
    (r"chmod\s*\(\s*0o?777", "World-writable permissions", "HIGH"),
    (r"verify\s*=\s*False", "SSL verification disabled", "HIGH"),
    (r"CORS\(.*origins\s*=\s*['\"]?\*", "Wildcard CORS", "MEDIUM"),
    (r"DEBUG\s*=\s*True", "Debug mode enabled", "MEDIUM"),
    (r"assert\s+", "Assert in production code", "LOW"),
    (r"except\s*:", "Bare except clause", "LOW"),
    (r"# ?TODO|# ?FIXME|# ?HACK|# ?XXX", "Unresolved code annotation", "LOW"),
    (r"\.env", "Potential dotenv reference", "INFO"),
    (r"tempfile\.mk(?:s?temp|dtemp)\((?!.*dir=)", "Temp file without explicit directory", "LOW"),
    (r"md5\(|sha1\(", "Weak hash algorithm", "MEDIUM"),
    (r"random\.random\(|random\.randint\(", "Non-cryptographic random", "LOW"),
    (r"urllib\.request\.urlopen\(", "Unvalidated URL open", "MEDIUM"),
    (r"exec\s*\(|compile\s*\(", "Dynamic code execution", "HIGH"),
    # CWE-434: Unrestricted file upload
    (r"\.save\(.*filename\)|\bsave_file\b.*request\.", "Unrestricted file upload (CWE-434)", "HIGH"),
    # CWE-611: XML External Entity (XXE)
    (r"xml\.etree\.ElementTree\.parse\(|lxml\.etree\.parse\(", "Potential XXE — use defusedxml (CWE-611)", "HIGH"),
    (r"xml\.sax\.parse\(|xml\.parsers\.expat", "Potential XXE via SAX/expat (CWE-611)", "HIGH"),
    # CWE-918: Server-Side Request Forgery (SSRF)
    (
        r"requests\.(?:get|post|put|delete|patch|head)\(.*(?:user|param|arg|input|query)",
        "Potential SSRF — validate URLs (CWE-918)",
        "HIGH",
    ),
    # CWE-502: Insecure deserialization
    (r"shelve\.open\(|jsonpickle\.decode\(|dill\.loads?\(", "Insecure deserialization (CWE-502)", "CRITICAL"),
    # CWE-732: Incorrect permission assignment
    (r"os\.chmod\(.*0o?[67][67][67]", "Overly permissive file mode (CWE-732)", "MEDIUM"),
    (r"umask\s*\(\s*0\s*\)", "Umask disabled — files world-accessible (CWE-732)", "HIGH"),
    # CWE-295: Improper certificate validation
    (r"ssl\._create_unverified_context|ssl\.CERT_NONE", "SSL cert validation disabled (CWE-295)", "HIGH"),
    # CWE-327: Use of broken cryptographic algorithm
    (r"DES\b|Blowfish|RC4|RC2|ARC4", "Weak/broken cipher algorithm (CWE-327)", "HIGH"),
    # CWE-798: Hardcoded credentials
    (r"(?:db_pass|database_password|mysql_pwd)\s*=\s*['\"]", "Hardcoded database password (CWE-798)", "CRITICAL"),
    # CWE-89: SQL injection (additional patterns)
    (r"%s.*(?:SELECT|INSERT|UPDATE|DELETE|DROP).*%\s*\(", "SQL injection via percent formatting (CWE-89)", "HIGH"),
    (r"cursor\.execute\(.*\+", "SQL injection via string concatenation (CWE-89)", "HIGH"),
    # CWE-22: Path traversal
    (r"open\(.*(?:request|user_input|params).*\)", "Potential path traversal (CWE-22)", "HIGH"),
    # CWE-400: Uncontrolled resource consumption
    (r"while\s+True.*(?:recv|read|accept)", "Unbounded I/O loop — potential DoS (CWE-400)", "MEDIUM"),
    # CWE-312: Cleartext storage/logging of sensitive data
    (
        r"log(?:ger)?\.(?:info|debug|warning|error).*(?:password|secret|token|api.?key)",
        "Logging sensitive data in cleartext (CWE-312)",
        "HIGH",
    ),
    # CWE-942: CSRF protection disabled
    (r"csrf_exempt|WTF_CSRF_ENABLED\s*=\s*False", "CSRF protection disabled (CWE-942)", "HIGH"),
    # CWE-1004: Sensitive cookie without HttpOnly
    (r"set_cookie\(.*secure\s*=\s*False", "Cookie without Secure flag (CWE-1004)", "MEDIUM"),
]


# ---------------------------------------------------------------------------
# AI anti-pattern heuristic checks (deterministic, supplement LLM review)
# ---------------------------------------------------------------------------

_AI_ANTIPATTERN_CHECKS: list[tuple[str, str, str]] = [
    # (pattern_regex, description, severity)
    (r"\bdatetime\.now\s*\(\s*\)", "Timezone-naive datetime.now() — use datetime.now(timezone.utc)", "HIGH"),
    (r"\bdatetime\.utcnow\s*\(", "Deprecated datetime.utcnow() — use datetime.now(timezone.utc)", "HIGH"),
    (
        r"""\blogger\.\w+\s*\(\s*["'](?:entering|exiting|starting function|leaving function)""",
        "Entry/exit logging noise — log meaningful state transitions, not function entry/exit",
        "LOW",
    ),
    (
        r"""(?:jsonify|return)\s*\(.*\bstr\s*\(\s*e\s*\)""",
        "str(e) leaked to API client — log error with logger.exception(), return generic message",
        "HIGH",
    ),
    (
        r"\bdef\s+to_dict\s*\(\s*self\s*\)",
        "Hand-written to_dict() on dataclass — use dataclasses.asdict() or .model_dump()",
        "MEDIUM",
    ),
    (
        r"\breturn\s+True\s+if\s+\S.*\s+else\s+False\b",
        "Redundant 'return True if X else False' — use 'return X'",
        "LOW",
    ),
    (r"""\bor\s+[\"\']{2}\b""", "Defensive 'or \"\"' — only use when value is genuinely Optional (str | None)", "LOW"),
    (r"\bor\s+\[\s*\]", "Defensive 'or []' — only use when value is genuinely Optional (list | None)", "LOW"),
    (
        r"""\byaml\.safe_load\s*\(|\bjson\.load\s*\(""",
        "Config file read — ensure this is not inside a request handler (cache at module level)",
        "MEDIUM",
    ),
]

_CORRECTNESS_PATTERNS: list[tuple[str, str, str]] = [
    (r"except\s*:", "Bare except catches all exceptions including KeyboardInterrupt", "MEDIUM"),
    (r"def \w+\(.*=\s*\[\s*\]", "Mutable default argument (list) shared across calls", "HIGH"),
    (r"def \w+\(.*=\s*\{\s*\}", "Mutable default argument (dict) shared across calls", "HIGH"),
    (r"==\s*None\b", "Use 'is None' instead of '== None' for identity check", "LOW"),
    (r"!=\s*None\b", "Use 'is not None' instead of '!= None' for identity check", "LOW"),
    (r"==\s*True\b", "Use 'if x:' instead of '== True' for boolean check", "LOW"),
    (r"==\s*False\b", "Use 'if not x:' instead of '== False' for boolean check", "LOW"),
    (r"except\s+Exception\s+as\s+\w+:\s*\n\s*pass", "Silently swallowed exception", "HIGH"),
    (r"range\(len\(", "Iterate directly over the sequence instead of range(len(...))", "LOW"),
    (r"(?<!\w)l\s*=\s*\[\s*\].*\n.*\.append", "Building list with append in loop; consider comprehension", "INFO"),
]

_PERFORMANCE_PATTERNS: list[tuple[str, str, str]] = [
    (r"for .+ in .+:\s*\n(?:\s+.*\n)*\s+for .+ in .+:", "Nested loop potential O(n^2) pattern", "MEDIUM"),
    (r"['\"] \+\s*\w|\w\s*\+ ['\"]", "String concatenation with +; use f-string or join()", "LOW"),
    (
        r"\.append\(.*\).*#.*comprehension|in \[.*for.*\].*\.append",
        "List append inside comprehension; use list comp directly",
        "LOW",
    ),
    (r"time\.sleep\(\s*[1-9]\d+", "Long sleep in production code blocks the thread", "MEDIUM"),
    (r"dict\(.*\)\s*$|list\(.*\)\s*$", "Unnecessary copy in loop; review if copy is needed", "INFO"),
    (r"\.split\(\).*len\(|len\(.*\.split\(\)", "Using split() for length estimation; use len(text)//4", "LOW"),
    (r"\.lower\(\).*in.*prompt|prompt.*\.lower\(\)", "Calling .lower() on full prompt in hot path", "LOW"),
    (r"SELECT.*FROM.*IN\s*\(SELECT", "Nested SELECT; potential N+1 query pattern", "HIGH"),
    (r"for .+ in .+:\s*\n\s+db\.|for .+ in .+:\s*\n\s+session\.", "DB query inside loop; potential N+1", "HIGH"),
]

_STANDARDS_PATTERNS: list[tuple[str, str, str]] = [
    (r"# ?TODO|# ?FIXME|# ?HACK|# ?XXX", "Unresolved code annotation (TODO/FIXME/HACK/XXX)", "LOW"),
    (r"^\s*print\s*\(", "print() in production code; use logging instead", "MEDIUM"),
    (r"def \w+\([^)]*\)\s*:", "Function missing return type annotation", "LOW"),
    (r"def \w+\([^)]*\)\s*->\s*Any\s*:", "Function returns Any; consider a specific return type", "INFO"),
    (r"#\s*type:\s*ignore", "type: ignore suppresses type checking; review carefully", "INFO"),
    (r"from __future__ import annotations", "", "INFO"),
    (r"import \*", "Wildcard import pollutes namespace and hides dependencies", "MEDIUM"),
    (r"\bstr\(e\)\b", "Converting exception to string; use logger.exception() instead", "LOW"),
    (
        r"logging\.info\(|logging\.warning\(|logging\.error\(|logging\.debug\(",
        "Using root logger; use logging.getLogger(__name__)",
        "LOW",
    ),
    (r"datetime\.utcnow\(\)", "datetime.utcnow() is deprecated; use datetime.now(timezone.utc)", "MEDIUM"),
    (
        r"datetime\.now\(\)(?!\s*\(timezone)",
        "datetime.now() without timezone; use datetime.now(timezone.utc)",
        "MEDIUM",
    ),
]

_SECURITY_PATTERN_REGEXES = _compile_patterns(_SECURITY_PATTERNS, flags=re.IGNORECASE)
_AI_ANTIPATTERN_REGEXES = _compile_patterns(_AI_ANTIPATTERN_CHECKS, flags=re.IGNORECASE)
_CORRECTNESS_REGEXES = _compile_patterns(_CORRECTNESS_PATTERNS)
_PERFORMANCE_REGEXES = _compile_patterns(_PERFORMANCE_PATTERNS)
_STANDARDS_REGEXES = _compile_patterns(_STANDARDS_PATTERNS)
_INDENTED_IMPORT_PATTERN = re.compile(r"^import\s+\w+|^from\s+\w+\s+import")


# ---------------------------------------------------------------------------
# Multi-perspective Inspector pass configs
# ---------------------------------------------------------------------------

INSPECTOR_PASS_CONFIGS: list[dict[str, str]] = [
    {
        "name": "correctness",
        "focus": "Logic errors, edge cases, off-by-one, null handling, type mismatches",
        "system_prompt": (
            "You are a correctness reviewer. Focus ONLY on: logic errors, "
            "edge cases, off-by-one errors, null/None handling, type mismatches, "
            "and incorrect return values. Ignore style, performance, and security."
        ),
    },
    {
        "name": "security",
        "focus": "OWASP patterns, secrets, injection, unsafe deserialization, auth bypass",
        "system_prompt": (
            "You are a security reviewer. Focus ONLY on: injection vulnerabilities "
            "(SQL, command, XSS), hardcoded secrets/credentials, unsafe deserialization, "
            "authentication bypass, insecure defaults, and missing input validation. "
            "Ignore style, performance, and logic correctness."
        ),
    },
    {
        "name": "performance",
        "focus": "O(n^2), blocking I/O, memory leaks, unnecessary allocations, N+1 queries",
        "system_prompt": (
            "You are a performance reviewer. Focus ONLY on: O(n^2) or worse algorithms, "
            "blocking I/O in async code, memory leaks, unnecessary object allocation in "
            "loops, N+1 query patterns, and missing caching opportunities. "
            "Ignore style, security, and correctness."
        ),
    },
    {
        "name": "standards",
        "focus": "Project conventions, naming, documentation, type annotations, imports",
        "system_prompt": (
            "You are a standards reviewer. Focus ONLY on: naming conventions, "
            "missing or incorrect type annotations, missing docstrings, import "
            "ordering, dead code, and project-specific conventions. "
            "Ignore security, performance, and correctness."
        ),
    },
]


# ---------------------------------------------------------------------------
# Heuristic scan functions
# ---------------------------------------------------------------------------


def run_security_scan(code: str) -> list[dict[str, Any]]:
    """Run regex-based security heuristic patterns against code.

    Checks all lines of *code* against ``_SECURITY_PATTERNS``. Each match
    produces a finding dict with severity, finding name, line number,
    evidence snippet, and source tag.

    Args:
        code: Source code string to scan.

    Returns:
        List of finding dicts with keys: severity, finding, line, evidence, source.
    """
    findings = []
    lines = code.split("\n")
    for line_num, line in enumerate(lines, 1):
        for pattern, finding_name, severity in _SECURITY_PATTERN_REGEXES:
            if pattern.search(line):
                findings.append({
                    "severity": severity,
                    "finding": finding_name,
                    "line": line_num,
                    "evidence": line.strip()[:120],
                    "source": "heuristic",
                })
    return findings


def run_antipattern_scan(code: str) -> list[dict[str, Any]]:
    """Scan code for deterministic AI code generation anti-patterns.

    Complements the LLM-driven review with regex-based checks for patterns
    that LLMs frequently generate. Skips comment-only lines.

    Args:
        code: The source code string to scan.

    Returns:
        List of finding dicts with severity, finding, line, evidence, and source fields.
    """
    findings = []
    lines = code.split("\n")
    for line_num, line in enumerate(lines, 1):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        for pattern, description, severity in _AI_ANTIPATTERN_REGEXES:
            if pattern.search(line):
                findings.append({
                    "severity": severity,
                    "finding": description,
                    "line": line_num,
                    "evidence": stripped[:120],
                    "source": "antipattern",
                })
                break  # One finding per line per scan pass
    return findings


def _run_correctness_scan(code: str) -> list[dict[str, Any]]:
    """Scan code for common correctness anti-patterns using regex.

    Checks for bare except clauses, mutable default arguments,
    identity comparisons using ``==`` instead of ``is`` for None/True/False,
    and other logic pitfalls that are reliably detectable without an LLM.

    Args:
        code: The source code string to scan.

    Returns:
        List of finding dicts with finding, severity, line, evidence, and source fields.
    """
    findings: list[dict[str, Any]] = []
    lines = code.split("\n")
    for line_num, line in enumerate(lines, 1):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        for pattern, description, severity in _CORRECTNESS_REGEXES:
            if pattern.search(line):
                findings.append({
                    "severity": severity,
                    "finding": description,
                    "line": line_num,
                    "evidence": stripped[:120],
                    "source": "correctness_heuristic",
                })
                break  # One finding per line per scan
    return findings


def _run_performance_scan(code: str) -> list[dict[str, Any]]:
    """Scan code for common performance anti-patterns using regex.

    Detects O(n²) patterns (nested loops over same collection), string
    concatenation in loops, list.append inside comprehensions, and other
    patterns known to cause performance issues at scale.

    Args:
        code: The source code string to scan.

    Returns:
        List of finding dicts with finding, severity, line, evidence, and source fields.
    """
    findings: list[dict[str, Any]] = []
    lines = code.split("\n")
    for line_num, line in enumerate(lines, 1):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        for pattern, description, severity in _PERFORMANCE_REGEXES:
            if pattern.search(line):
                findings.append({
                    "severity": severity,
                    "finding": description,
                    "line": line_num,
                    "evidence": stripped[:120],
                    "source": "performance_heuristic",
                })
                break  # One finding per line per scan
        else:
            # Indented import check — only flag imports inside a function/method body.
            # Module-level imports have no leading whitespace, so line == stripped.
            # We must not flag those — only lines with indentation are in-function imports.
            if line != stripped and _INDENTED_IMPORT_PATTERN.search(stripped):
                findings.append({
                    "severity": "LOW",
                    "finding": "Import inside a function body — move to top of file",
                    "line": line_num,
                    "evidence": stripped[:120],
                    "source": "performance_heuristic",
                })
    return findings


def _run_standards_scan(code: str) -> list[dict[str, Any]]:
    """Scan code for standards violations using regex.

    Checks for missing type annotations on def statements, unresolved
    TODO/FIXME/HACK comments, print() calls in production code, and
    other project-specific conventions from the repository quality contract.

    Args:
        code: The source code string to scan.

    Returns:
        List of finding dicts with finding, severity, line, evidence, and source fields.
    """
    # Patterns that must scan ALL lines including comments (e.g. TODO/FIXME annotations)
    _comment_inclusive = frozenset({"Unresolved code annotation (TODO/FIXME/HACK/XXX)"})

    findings: list[dict[str, Any]] = []
    lines = code.split("\n")
    for line_num, line in enumerate(lines, 1):
        stripped = line.strip()
        for pattern, description, severity in _STANDARDS_REGEXES:
            if not description:  # Skip placeholder entries
                continue
            # Skip comment lines for most patterns but not for TODO/FIXME checks
            if stripped.startswith("#") and description not in _comment_inclusive:
                continue
            if pattern.search(line):
                findings.append({
                    "severity": severity,
                    "finding": description,
                    "line": line_num,
                    "evidence": stripped[:120],
                    "source": "standards_heuristic",
                })
                break  # One finding per line per scan
    return findings


def run_multi_perspective_review(
    code: str,
    context: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Run 4 focused sequential Inspector passes and merge findings.

    Each pass uses a distinct heuristic scan focused on one dimension:
    correctness, security, performance, and standards. Findings from all
    passes are merged and deduplicated by location + issue key.

    All findings are returned regardless of severity so callers can make
    their own gating decisions. CRITICAL findings are tagged with
    ``severity="CRITICAL"`` in the returned list; callers that need a hard
    stop should filter on that field.

    Args:
        code: The code to review.
        context: Optional context (file path, task type, etc.).

    Returns:
        List of finding dicts with keys: pass_name, severity, finding,
        line, evidence, source. CRITICAL findings are included in the list
        rather than causing an exception.
    """
    all_findings: list[dict[str, Any]] = []
    seen_keys: set[str] = set()

    _pass_scanners: dict[str, Any] = {
        "correctness": _run_correctness_scan,
        "security": run_security_scan,
        "performance": _run_performance_scan,
        "standards": _run_standards_scan,
    }

    for pass_config in INSPECTOR_PASS_CONFIGS:
        pass_name = pass_config["name"]
        scanner = _pass_scanners.get(pass_name)
        if scanner is None:
            logger.warning("No heuristic scanner configured for pass=%s — skipping", pass_name)
            continue

        try:
            findings = scanner(code)
        except Exception:
            logger.warning(
                "Heuristic scan failed for pass=%s — skipping this pass",
                pass_name,
                exc_info=True,
            )
            continue

        for f in findings:
            key = f"{pass_name}:{f.get('finding', '')}:{f.get('line', 0)}"
            if key not in seen_keys:
                seen_keys.add(key)
                f["pass_name"] = pass_name
                all_findings.append(f)

    return all_findings
