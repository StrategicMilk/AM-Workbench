"""Deterministic guardrail checks used before optional providers."""

from __future__ import annotations

import re

from vetinari.guardrails.prompt_security import PromptSecurityFinding, scan_prompt_security
from vetinari.safety.guardrails_types import Violation

_JAILBREAK_PATTERNS = [
    re.compile(r"ignore\s+(all\s+)?(previous|prior|above)\s+(instructions|prompts)", re.IGNORECASE),
    re.compile(r"you\s+are\s+now\s+(in\s+)?(\w+\s+)?mode", re.IGNORECASE),
    re.compile(r"pretend\s+(you\s+are|to\s+be)\s+", re.IGNORECASE),
    re.compile(r"(system|admin)\s*prompt\s*[:=]", re.IGNORECASE),
    re.compile(r"disregard\s+(your|all|the)\s+(rules|instructions|guidelines)", re.IGNORECASE),
    re.compile(r"bypass\s+(safety|security|content)\s+(filter|check|restriction)", re.IGNORECASE),
]

_PROMPT_INJECTION_PATTERNS = [
    re.compile(r"\bignore\s+(all\s+)?(previous|prior|above)\s+(developer|system)\s+messages?\b", re.IGNORECASE),
    re.compile(r"\breveal\s+(the\s+)?(system|developer|hidden)\s+(prompt|instructions|message)\b", re.IGNORECASE),
    re.compile(r"\breveal\s+(the\s+)?hidden\s+(system|developer)\s+(prompt|instructions|message)\b", re.IGNORECASE),
    re.compile(r"\bprint\s+(the\s+)?(system|developer|hidden)\s+(prompt|instructions|message)\b", re.IGNORECASE),
    re.compile(r"\b(system|developer)\s+override\b", re.IGNORECASE),
    re.compile(r"\bdisable\s+(all\s+)?(safety|security|content)\s+(rules|checks|filters|guardrails)\b", re.IGNORECASE),
    re.compile(r"\boverride\s+(the\s+)?(system|developer|safety)\s+(prompt|instructions|rules)\b", re.IGNORECASE),
    re.compile(r"\bact\s+as\s+if\s+(you\s+have\s+)?no\s+(rules|restrictions|guardrails|safety)\b", re.IGNORECASE),
]

_SENSITIVE_DATA_PATTERNS = [
    re.compile(r"(?:api[_-]?key|secret[_-]?key|access[_-]?token)\s*[:=]\s*\S+", re.IGNORECASE),
    re.compile(r"(?:password|passwd|pwd)\s*[:=]\s*\S+", re.IGNORECASE),
    re.compile(r"sk-[a-zA-Z0-9]{20,}"),
    re.compile(r"ghp_[a-zA-Z0-9]{36}"),
    re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    re.compile(r"-----BEGIN\s+(RSA\s+)?PRIVATE\s+KEY-----"),
    re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
    re.compile(r"\b(?:\d{4}[-\s]?){3}\d{4}\b"),
    re.compile(r"\b(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"DefaultEndpointsProtocol=https;AccountName=\w+"),
    re.compile(r"eyJ[a-zA-Z0-9_-]+\.eyJ[a-zA-Z0-9_-]+\.[a-zA-Z0-9_-]+"),
    re.compile(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}:\d{1,5}\b"),
]


def _finding_to_violation(finding: PromptSecurityFinding) -> Violation:
    """Convert a prompt-security finding to the public guardrail violation type."""
    return Violation(
        rail=finding.rail,
        severity=finding.severity,
        description=finding.description,
        matched_pattern=finding.matched_text,
    )


def _check_jailbreak(text: str) -> list[Violation]:
    """Scan text for prompt injection and jailbreak patterns."""
    violations = []
    for pattern in _JAILBREAK_PATTERNS:
        match = pattern.search(text)
        if match:
            violations.append(
                Violation(
                    rail="jailbreak",
                    severity="high",
                    description="Potential jailbreak/prompt injection detected",
                    matched_pattern=match.group(0),
                ),
            )
    return violations


def _check_prompt_injection(text: str) -> list[Violation]:
    """Scan text for direct attempts to override or extract prompt instructions."""
    violations = []
    for pattern in _PROMPT_INJECTION_PATTERNS:
        match = pattern.search(text)
        if match:
            violations.append(
                Violation(
                    rail="prompt_injection",
                    severity="high",
                    description="Potential prompt injection attempt detected",
                    matched_pattern=match.group(0),
                ),
            )
    return violations


def _check_vector_context(text: str) -> list[Violation]:
    """Scan text for retrieved-context vector and embedding weakness markers."""
    return [
        _finding_to_violation(finding) for finding in scan_prompt_security(text) if finding.rail == "vector_context"
    ]


def _check_prompt_security(text: str) -> list[Violation]:
    """Scan text once for built-in prompt and vector-context findings."""
    violations = [_finding_to_violation(finding) for finding in scan_prompt_security(text)]
    existing = {(violation.rail, violation.matched_pattern.casefold()) for violation in violations}
    for violation in _check_jailbreak(text):
        key = (violation.rail, violation.matched_pattern.casefold())
        if key not in existing:
            existing.add(key)
            violations.append(violation)
    return violations


def _check_sensitive_data(text: str) -> list[Violation]:
    """Scan text for sensitive data patterns such as API keys, passwords, and PII."""
    violations = []
    for pattern in _SENSITIVE_DATA_PATTERNS:
        match = pattern.search(text)
        if match:
            violations.append(
                Violation(
                    rail="sensitive_data",
                    severity="high",
                    description="Potential sensitive data detected in output",
                    matched_pattern=match.group(0)[:20] + "...",
                ),
            )
    return violations


_TOXIC_PATTERNS: list[tuple[re.Pattern[str], str, str]] = [
    (
        re.compile(
            r"\b(kill|murder|exterminate|genocide|slaughter)\s+(all\s+)?(the\s+)?"
            r"(jews?|muslims?|christians?|blacks?|whites?|latinos?|asians?|gays?|"
            r"lesbians?|trans\w*|immigrants?|refugees?)\b",
            re.IGNORECASE,
        ),
        "high",
        "Hate speech - targeted violence against a group",
    ),
    (
        re.compile(
            r"\b(all|those|these)\s+(jews?|muslims?|christians?|blacks?|whites?|"
            r"latinos?|asians?|gays?|lesbians?|trans\w*|immigrants?|refugees?)\s+"
            r"(should|must|deserve\s+to)\s+(die|be\s+killed|be\s+eliminated|be\s+removed)\b",
            re.IGNORECASE,
        ),
        "high",
        "Hate speech - incitement to eliminate a group",
    ),
    (
        re.compile(
            r"\b(ni+g+[e]?r|ch[i1]nk|sp[i1]c|k[i1]ke|f[a4]gg?[o0]t|tr[a4]nn[y]?)\b",
            re.IGNORECASE,
        ),
        "medium",
        "Hate speech - racial or homophobic slur",
    ),
    (
        re.compile(
            r"\b(bomb|shoot|stab|attack|blow\s+up)\s+(the\s+)?(school|hospital|"
            r"church|mosque|synagogue|government|police|congress|parliament|crowd|crowd)\b",
            re.IGNORECASE,
        ),
        "high",
        "Violence incitement - attack on a specific target",
    ),
    (
        re.compile(
            r"\bhow\s+to\s+(make|build|construct|create)\s+a\s+(bomb|explosive|ied|"
            r"bioweapon|chemical\s+weapon|dirty\s+bomb)\b",
            re.IGNORECASE,
        ),
        "high",
        "Violence incitement - weapons manufacturing instructions",
    ),
    (
        re.compile(
            r"\b(mass\s+(shooting|stabbing|killing)|domestic\s+terrorism|lone\s+wolf\s+attack)\b",
            re.IGNORECASE,
        ),
        "high",
        "Violence incitement - mass violence planning language",
    ),
    (
        re.compile(
            r"\b(how\s+to|best\s+way\s+to|methods?\s+(for|of))\s+"
            r"(commit\s+suicide|kill\s+(myself|yourself)|end\s+my\s+life|self[\s-]?harm)\b",
            re.IGNORECASE,
        ),
        "high",
        "Self-harm - instructions for self-injury or suicide",
    ),
    (
        re.compile(
            r"\b(suicide|self[\s-]?harm|cut\s+(myself|yourself)|overdose)\s+"
            r"(methods?|instructions?|guide|tips?|ways?)\b",
            re.IGNORECASE,
        ),
        "high",
        "Self-harm - self-injury method request",
    ),
    (
        re.compile(
            r"\bi\s+(want\s+to|am\s+going\s+to|will)\s+(kill|hurt|harm)\s+(myself|me)\b",
            re.IGNORECASE,
        ),
        "medium",
        "Self-harm - first-person expression of self-harm intent",
    ),
]


def _check_toxic(text: str) -> list[Violation]:
    """Check text for toxic content patterns."""
    violations = []
    for pattern, severity, description in _TOXIC_PATTERNS:
        match = pattern.search(text)
        if match:
            violations.append(
                Violation(
                    rail="toxic",
                    severity=severity,
                    description=description,
                    matched_pattern=match.group(0)[:40],
                ),
            )
    return violations
