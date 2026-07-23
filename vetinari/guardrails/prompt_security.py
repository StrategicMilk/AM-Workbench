"""Prompt security scanning for guardrail entry points.

This module owns deterministic prompt-injection and retrieved-context vector
risk checks that run before optional ML scanners. The checks are intentionally
structure-based so they catch role-message payloads, paraphrased instruction
overrides, obfuscated payloads, and suspicious instructions embedded in
retrieval context.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from base64 import b64decode
from binascii import Error as BinasciiError
from dataclasses import dataclass

from vetinari.exceptions import SystemPromptLeakageError

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PromptSecurityFinding:
    """A deterministic prompt-security finding returned by guardrail scans.

    Attributes:
        code: Stable finding identifier for tests and callers.
        rail: Guardrail category that should receive the finding.
        severity: Risk severity assigned by the deterministic scanner.
        description: Human-readable explanation of the risk.
        matched_text: Short text fragment that triggered the finding.
    """

    code: str
    rail: str
    severity: str
    description: str
    matched_text: str = ""

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}("
            f"code={self.code!r}, "
            f"rail={self.rail!r}, "
            f"severity={self.severity!r}, "
            f"description={self.description!r}"
            ")"
        )


@dataclass(frozen=True, slots=True)
class _PromptSecurityPattern:
    """Compiled scanner pattern and the finding metadata it produces."""

    code: str
    rail: str
    severity: str
    description: str
    pattern: re.Pattern[str]

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}("
            f"code={self.code!r}, "
            f"rail={self.rail!r}, "
            f"severity={self.severity!r}, "
            f"description={self.description!r}"
            ")"
        )


@dataclass(frozen=True, slots=True)
class _TextVariant:
    """Prompt text variant derived from normalization or payload decoding."""

    label: str
    text: str


_FLAGS = re.IGNORECASE | re.DOTALL
_DECODED_PAYLOAD_MIN_CHARS = 16  # Avoid treating short IDs as encoded instructions.
_DECODED_PAYLOAD_MAX_BYTES = 4096  # Bound decode work to guardrail-sized snippets.
_MATCH_SNIPPET_CHARS = 160  # Keep finding evidence compact for API responses.
_BASE64_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9+/=_-])[A-Za-z0-9+/_-]{24,}={0,2}(?![A-Za-z0-9+/=_-])")
_HEX_TOKEN_RE = re.compile(r"(?<![0-9A-Fa-f])(?:[0-9A-Fa-f]{2}){8,}(?![0-9A-Fa-f])")
_RETRIEVED_CONTEXT_MARKER_RE = re.compile(
    r"\b(?:retrieved|retrieval|rag|source|chunk|document|context|vector[\s-]?store|embedding)\b",
    re.IGNORECASE,
)
_ROLE_PREFIX_RE = re.compile(r"^\s*(?:system|developer|assistant|tool|user)\s*[:=]", re.IGNORECASE | re.MULTILINE)


_PROMPT_INJECTION_PATTERNS: tuple[_PromptSecurityPattern, ...] = (
    _PromptSecurityPattern(
        code="prompt_injection.instruction_override",
        rail="jailbreak",
        severity="high",
        description="Potential prompt injection: instruction override request",
        pattern=re.compile(
            r"\b(?:ignore|disregard|forget|override|discard|skip|stop\s+following)\b.{0,80}"
            r"\b(?:instructions?|prompts?|rules?|guidelines?|polic(?:y|ies)|safety|guardrails?)\b",
            _FLAGS,
        ),
    ),
    _PromptSecurityPattern(
        code="prompt_injection.role_prefix",
        rail="jailbreak",
        severity="high",
        description="Potential prompt injection: role-message prefix detected",
        pattern=_ROLE_PREFIX_RE,
    ),
    _PromptSecurityPattern(
        code="prompt_injection.system_prompt_marker",
        rail="jailbreak",
        severity="high",
        description="Potential prompt injection: system or developer prompt marker detected",
        pattern=re.compile(r"\b(?:system|admin|developer)\s+prompt\s*[:=]", re.IGNORECASE),
    ),
    _PromptSecurityPattern(
        code="prompt_injection.chatml_role",
        rail="jailbreak",
        severity="high",
        description="Potential prompt injection: chat role control token detected",
        pattern=re.compile(r"<\|im_start\|>\s*(?:system|developer|assistant|tool|user)", re.IGNORECASE),
    ),
    _PromptSecurityPattern(
        code="prompt_injection.markdown_role_block",
        rail="jailbreak",
        severity="high",
        description="Potential prompt injection: role block delimiter detected",
        pattern=re.compile(r"###\s*(?:system|developer|assistant|tool|user)\s*###", re.IGNORECASE),
    ),
    _PromptSecurityPattern(
        code="prompt_injection.mode_switch",
        rail="jailbreak",
        severity="high",
        description="Potential prompt injection: unsafe role or mode switch request",
        pattern=re.compile(
            r"\b(?:you\s+are\s+now|pretend\s+(?:you\s+are|to\s+be)|act\s+as|respond\s+as)\b.{0,90}"
            r"\b(?:unrestricted|uncensored|developer|admin|system|no\s+safety|no\s+rules|no\s+restrictions)\b",
            _FLAGS,
        ),
    ),
    _PromptSecurityPattern(
        code="prompt_injection.safety_does_not_apply",
        rail="jailbreak",
        severity="high",
        description="Potential prompt injection: safety policy bypass framing",
        pattern=re.compile(
            r"\b(?:rules?|safety|guardrails?|polic(?:y|ies)|restrictions?)\b.{0,80}"
            r"\b(?:do\s+not|don't|no\s+longer|never)\b.{0,40}\b(?:apply|matter|exist|bind\s+you)\b",
            _FLAGS,
        ),
    ),
    _PromptSecurityPattern(
        code="prompt_injection.safety_filter_bypass",
        rail="jailbreak",
        severity="high",
        description="Potential prompt injection: safety filter bypass request",
        pattern=re.compile(
            r"\b(?:bypass|disable|evade|circumvent)\b.{0,80}"
            r"\b(?:safety|security|content|guardrail|policy)\b.{0,50}"
            r"\b(?:filters?|checks?|restrictions?|controls?|rules?|guardrails?)\b",
            _FLAGS,
        ),
    ),
    _PromptSecurityPattern(
        code="prompt_injection.prompt_extraction",
        rail="jailbreak",
        severity="high",
        description="Potential prompt injection: system or developer prompt extraction request",
        pattern=re.compile(
            r"\b(?:reveal|print|show|dump|expose|extract|send)\b.{0,70}"
            r"\b(?:system|developer|admin)\b.{0,50}\b(?:prompt|instructions?|message|policy)\b",
            _FLAGS,
        ),
    ),
    _PromptSecurityPattern(
        code="prompt_injection.treat_as_system",
        rail="jailbreak",
        severity="high",
        description="Potential prompt injection: untrusted text framed as a higher-priority message",
        pattern=re.compile(
            r"\b(?:treat|read|interpret|use)\b.{0,80}\b(?:as|like)\b.{0,50}"
            r"\b(?:system|developer|admin|tool)\b.{0,40}\b(?:message|prompt|instruction)\b",
            _FLAGS,
        ),
    ),
)

_VECTOR_CONTEXT_PATTERNS: tuple[_PromptSecurityPattern, ...] = (
    _PromptSecurityPattern(
        code="vector_context.hidden_instruction_payload",
        rail="vector_context",
        severity="high",
        description="Embedding/vector context risk: retrieved context carries hidden instructions",
        pattern=re.compile(
            r"\b(?:retrieved|retrieval|rag|source|chunk|document|context|vector[\s-]?store|embedding)\b"
            r".{0,180}(?:^\s*(?:system|developer|assistant|tool)\s*[:=]|<\|im_start\|>\s*"
            r"(?:system|developer|assistant|tool)|###\s*(?:system|developer|assistant|tool)\s*###)",
            _FLAGS | re.MULTILINE,
        ),
    ),
    _PromptSecurityPattern(
        code="vector_context.retrieval_instruction_override",
        rail="vector_context",
        severity="high",
        description="Embedding/vector context risk: retrieved context asks to override instructions",
        pattern=re.compile(
            r"\b(?:retrieved|retrieval|rag|source|chunk|document|context|vector[\s-]?store|embedding)\b.{0,180}"
            r"\b(?:ignore|disregard|forget|override|replace|bypass)\b.{0,90}"
            r"\b(?:instructions?|rules?|polic(?:y|ies)|safety|guardrails?|system\s+prompt)\b",
            _FLAGS,
        ),
    ),
    _PromptSecurityPattern(
        code="vector_context.tool_credential_extraction",
        rail="vector_context",
        severity="high",
        description="Embedding/vector context risk: retrieved context asks tools to extract credentials",
        pattern=re.compile(
            r"\b(?:retrieved|retrieval|rag|source|chunk|document|context|vector[\s-]?store|embedding)\b.{0,220}"
            r"\b(?:use|call|invoke|run|execute)\b.{0,90}"
            r"\b(?:tool|function|plugin|connector|mcp|browser|shell|terminal)\b.{0,140}"
            r"\b(?:credential|secret|token|api\s*key|password|environment\s+variable|env\s+var|system\s+prompt)\b",
            _FLAGS,
        ),
    ),
    _PromptSecurityPattern(
        code="vector_context.credential_exfiltration",
        rail="vector_context",
        severity="high",
        description="Embedding/vector context risk: retrieved context contains credential exfiltration instructions",
        pattern=re.compile(
            r"\b(?:extract|exfiltrate|leak|reveal|dump|send)\b.{0,100}"
            r"\b(?:credential|secret|token|api\s*key|password|environment\s+variable|env\s+var)\b.{0,220}"
            r"\b(?:retrieved|retrieval|rag|source|chunk|document|context|vector[\s-]?store|embedding)\b",
            _FLAGS,
        ),
    ),
    _PromptSecurityPattern(
        code="vector_context.poisoning_marker",
        rail="vector_context",
        severity="high",
        description="Embedding/vector context risk: vector-store poisoning marker detected",
        pattern=re.compile(
            r"\b(?:vector[\s-]?store|embedding|retrieval|rag)\b.{0,140}"
            r"\b(?:poison|poisoning|backdoor|implant|malicious\s+payload|retrieval\s+override)\b.{0,140}"
            r"\b(?:ignore|override|system|developer|assistant|tool|credential|secret|token)\b",
            _FLAGS,
        ),
    ),
)

PROMPT_INJECTION_PATTERN_COUNT = len(_PROMPT_INJECTION_PATTERNS)
VECTOR_CONTEXT_PATTERN_COUNT = len(_VECTOR_CONTEXT_PATTERNS)
_PROMPT_SECURITY_PATTERNS = _PROMPT_INJECTION_PATTERNS + _VECTOR_CONTEXT_PATTERNS


def scan_prompt_security(text: str) -> tuple[PromptSecurityFinding, ...]:
    """Scan prompt text for deterministic prompt and vector-context risks.

    Args:
        text: Prompt, retrieved context, or combined LLM input text to inspect.

    Returns:
        Tuple of deterministic findings. An empty tuple means no built-in
        prompt-security pattern matched; optional ML scanners may still run.
    """
    findings: list[PromptSecurityFinding] = []
    seen: set[tuple[str, str, str]] = set()
    has_retrieved_context_marker = _RETRIEVED_CONTEXT_MARKER_RE.search(text) is not None
    for variant in _text_variants(text):
        for scanner_pattern in _PROMPT_SECURITY_PATTERNS:
            match = scanner_pattern.pattern.search(variant.text)
            if not match:
                continue
            matched_text = _format_matched_text(variant.label, match.group(0))
            key = (scanner_pattern.code, variant.label, matched_text.casefold())
            if key in seen:
                continue
            seen.add(key)
            findings.append(
                PromptSecurityFinding(
                    code=scanner_pattern.code,
                    rail=scanner_pattern.rail,
                    severity=scanner_pattern.severity,
                    description=scanner_pattern.description,
                    matched_text=matched_text,
                )
            )
            if (
                has_retrieved_context_marker
                and scanner_pattern.rail == "jailbreak"
                and variant.label in {"base64", "hex", "base64_unicode_nfkc", "hex_unicode_nfkc"}
            ):
                vector_key = ("vector_context.hidden_instruction_payload", variant.label, matched_text.casefold())
                if vector_key not in seen:
                    seen.add(vector_key)
                    findings.append(
                        PromptSecurityFinding(
                            code="vector_context.hidden_instruction_payload",
                            rail="vector_context",
                            severity="high",
                            description="Embedding/vector context risk: retrieved context carries hidden instructions",
                            matched_text=matched_text,
                        )
                    )
    return tuple(findings)


def scan_for_indirect_injection(retrieved_text: str) -> tuple[PromptSecurityFinding, ...]:
    """Scan retrieved context for indirect prompt-injection patterns.

    Args:
        retrieved_text: Retrieved or tool-provided context text.

    Returns:
        Tuple of deterministic prompt-security findings.
    """
    return PromptInjectionDetector().scan_for_indirect_injection(retrieved_text)


def _text_variants(text: str) -> tuple[_TextVariant, ...]:
    variants: list[_TextVariant] = [_TextVariant("raw", text)]
    normalized = unicodedata.normalize("NFKC", text)
    if normalized != text:
        variants.append(_TextVariant("unicode_nfkc", normalized))

    for label, token_re in (("base64", _BASE64_TOKEN_RE), ("hex", _HEX_TOKEN_RE)):
        for match in token_re.finditer(text):
            decoded = _decode_obfuscated_token(label, match.group(0))
            if decoded is None:
                continue
            variants.append(_TextVariant(label, decoded))
            decoded_normalized = unicodedata.normalize("NFKC", decoded)
            if decoded_normalized != decoded:
                variants.append(_TextVariant(f"{label}_unicode_nfkc", decoded_normalized))
    return tuple(variants)


def _decode_obfuscated_token(kind: str, token: str) -> str | None:
    try:
        if kind == "base64":
            padded = token.replace("-", "+").replace("_", "/")
            padded += "=" * (-len(padded) % 4)
            decoded = b64decode(padded, validate=True)
        elif kind == "hex":
            decoded = bytes.fromhex(token)
        else:
            return None
    except (BinasciiError, ValueError):
        logger.warning("Exception handled by  decode obfuscated token fallback", exc_info=True)
        return None
    if not (_DECODED_PAYLOAD_MIN_CHARS <= len(decoded) <= _DECODED_PAYLOAD_MAX_BYTES):
        return None
    try:
        decoded_text = decoded.decode("utf-8")
    except UnicodeDecodeError:
        logger.warning("Exception handled by  decode obfuscated token fallback", exc_info=True)
        return None
    printable_chars = sum(1 for char in decoded_text if char.isprintable() or char.isspace())
    if printable_chars / max(len(decoded_text), 1) < 0.85:
        return None
    return decoded_text


def _format_matched_text(variant_label: str, matched_text: str) -> str:
    compact = re.sub(r"\s+", " ", matched_text).strip()
    if len(compact) > _MATCH_SNIPPET_CHARS:
        compact = f"{compact[:_MATCH_SNIPPET_CHARS]}..."
    if variant_label == "raw":
        return compact
    return f"{variant_label}:{compact}"


class PromptInjectionDetector:
    """Deterministic prompt-injection detector for direct and indirect injection paths.

    Wraps `scan_prompt_security` for both direct user-input scanning (LLM01 mitigation)
    and indirect injection via retrieved context (RAG poisoning / LLM01 indirect vector).
    """

    def scan(self, text: str) -> tuple[PromptSecurityFinding, ...]:
        """Scan text for direct prompt-injection patterns.

        Args:
            text: Raw user input or assistant content to inspect.

        Returns:
            Tuple of PromptSecurityFinding instances, empty when no patterns match.
        """
        return scan_prompt_security(text)

    def scan_for_indirect_injection(self, retrieved_text: str) -> tuple[PromptSecurityFinding, ...]:
        """Scan retrieved context for indirect prompt-injection patterns.

        Tags the text as retrieved context before scanning so vector-context
        patterns can match alongside direct-injection patterns.

        Args:
            retrieved_text: Text retrieved from a vector store or external source.

        Returns:
            Tuple of PromptSecurityFinding instances, empty when no patterns match.
        """
        tagged = f"retrieved context: {retrieved_text}"
        return scan_prompt_security(tagged)


class SystemPromptIsolationGuard:
    """Enforce system-prompt isolation by removing role-override attempts from messages.

    Implements the LLM07 (System Prompt Leakage / Role Injection) mitigation from
    OWASP LLM Top 10 2025 by stripping non-system messages that contain role-prefix
    injection patterns before the message list reaches the inference layer.
    """

    def apply(self, messages: list[dict[str, str]]) -> list[dict[str, str]]:
        """Strip messages that attempt to inject system-role content into non-system turns.

        Raises :class:`~vetinari.exceptions.SystemPromptLeakageError` when any
        non-system message contains a role-prefix injection pattern (LLM07).

        Args:
            messages: Ordered list of chat messages with `role` and `content` keys.

        Returns:
            Filtered list with role-injection attempts removed; system/developer
            turns are always preserved unchanged.

        Raises:
            SystemPromptLeakageError: When a role-injection attempt is detected
                in a non-system turn.
        """
        clean: list[dict[str, str]] = []
        for msg in messages:
            role = msg.get("role", "")
            content = msg.get("content", "")
            if role in {"system", "developer"}:
                clean.append(msg)
                continue
            if _ROLE_PREFIX_RE.search(content):
                logger.warning(
                    "SystemPromptIsolationGuard: removed role-injection attempt from %s turn",
                    role,
                )
                raise SystemPromptLeakageError(
                    f"Role-injection attempt detected in {role!r} turn — "
                    "message blocked to prevent system-prompt leakage (LLM07)"
                )
            clean.append(msg)
        return clean


__all__ = [
    "PROMPT_INJECTION_PATTERN_COUNT",
    "VECTOR_CONTEXT_PATTERN_COUNT",
    "PromptInjectionDetector",
    "PromptSecurityFinding",
    "SystemPromptIsolationGuard",
    "scan_for_indirect_injection",
    "scan_prompt_security",
]
