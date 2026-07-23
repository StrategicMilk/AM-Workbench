"""Vetinari Security Package.

Exports the agent permission system, secret scanning utilities, and
SSRF-safe URL validation for all vetinari modules.
"""

from __future__ import annotations

import ipaddress as _ipaddress
import logging as _logging
import re as _re
import socket as _socket
import threading as _threading
from dataclasses import dataclass as _dataclass
from urllib.parse import urlparse as _urlparse

from vetinari.security.agent_permissions import (
    AgentAction,
    AgentPermissionPolicy,
    AgentPermissions,
)
from vetinari.security.fail_closed import (
    PathTraversalError,
    SandboxUnavailableError,
    SchemaOpenError,
    UntrustedInputError,
    assert_closed_schema,
    confine_to_root,
    require_sandbox_or_raise,
    sanitize_untrusted_text,
)
from vetinari.security.prompt_sanitizer import sanitize_prompt, sanitize_prompt_record
from vetinari.security.secret_scanner import SecretFinding, scan_for_secrets

_logger = _logging.getLogger(__name__)

# Hostnames that are known cloud metadata endpoints, container-host aliases, or
# loopback aliases that Python's ipaddress module does not parse as IPs —
# and therefore must be explicitly named here rather than caught by the IP range check.
_BLOCKED_HOSTNAMES: frozenset[str] = frozenset({
    "localhost",  # Loopback alias — not parsed by ipaddress.ip_address()
    "metadata.google.internal",
    "169.254.169.254",  # AWS/GCP/Azure IMDS endpoint (also caught by link-local check)
    "metadata.internal",
    "host.docker.internal",  # Docker bridge alias — resolves to host on most platforms
})

__all__ = [
    "AgentAction",
    "AgentPermissionPolicy",
    "AgentPermissions",
    "PathTraversalError",
    "SandboxUnavailableError",
    "SchemaOpenError",
    "SecretFinding",
    "SecretPattern",
    "SecretScanner",
    "UntrustedInputError",
    "assert_closed_schema",
    "confine_to_root",
    "get_secret_scanner",
    "new_secret_scanner",
    "require_sandbox_or_raise",
    "sanitize_dict_for_memory",
    "sanitize_for_memory",
    "sanitize_prompt",
    "sanitize_prompt_record",
    "sanitize_untrusted_text",
    "scan_for_secrets",
    "validate_url_no_ssrf",
]


@_dataclass
class SecretPattern:
    """Represents a regex pattern for detecting secrets."""

    name: str
    pattern: str
    description: str


class SecretScanner:
    """Scans text for secrets and sanitizes dictionaries containing sensitive data.

    Wraps ``vetinari.learning.secrets_filter`` for pattern-based detection and
    adds key-name-based redaction for dictionaries, custom pattern support, and
    Bearer/JWT/GitHub/AWS/OpenAI token detection.
    """

    _PATH_TRAVERSAL_RE = _re.compile(r"(?:\.\./|\.\.\\)+")

    # JWT: three base64url segments separated by dots
    _JWT_RE = _re.compile(
        r"(eyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)",
    )
    # Bearer token (may be partial JWT or opaque token, 20+ chars)
    _BEARER_RE = _re.compile(r"Bearer\s+([A-Za-z0-9_.-]{20,})")

    # GitHub token prefixes: ghp_, ghu_, ghs_, gho_, ghr_
    _GITHUB_TOKEN_RE = _re.compile(r"(gh[pousr]_[A-Za-z0-9]{20,})")

    # AWS access key: AKIA + 16 uppercase alphanumeric
    _AWS_KEY_RE = _re.compile(r"(AKIA[A-Z0-9]{16})")

    # OpenAI API key: sk-proj-... or sk-... (case insensitive, 3+ chars)
    _OPENAI_KEY_RE = _re.compile(r"(sk-(?:proj-)?[A-Za-z0-9]{3,})", _re.IGNORECASE)

    # Field names whose values should always be redacted in dicts
    _SENSITIVE_KEYS = frozenset({
        "password",
        "passwd",
        "pwd",
        "secret",
        "token",
        "api_key",
        "apikey",
        "api_secret",
        "client_secret",
        "access_key",
        "secret_key",
        "private_key",
        "auth",
        "authorization",
        "credential",
        "credentials",
        "access_token",
        "refresh_token",
        "oauth_token",
        "bearer",
    })

    def __init__(self) -> None:
        self._custom_patterns: list[SecretPattern] = []

    def add_pattern(self, pattern: SecretPattern) -> None:
        """Register a custom secret pattern for detection.

        Args:
            pattern: A ``SecretPattern`` with *name*, *pattern* (regex), and *description*.
        """
        self._custom_patterns.append(pattern)

    # -- Detection -------------------------------------------------------------

    def scan(self, text: str) -> dict[str, list[str]]:
        """Scan text for secrets, returning ``{pattern_name: [matched_strings]}``.

        Args:
            text: Text to scan.

        Returns:
            Dict mapping pattern names to lists of matched strings.
        """
        from vetinari.learning.secrets_filter import scan_text

        detections = scan_text(text)
        result: dict[str, list[str]] = {}
        for det in detections:
            label = det.pattern_label if hasattr(det, "pattern_label") else str(det)
            result.setdefault(label, []).append(
                det.matched_text if hasattr(det, "matched_text") else str(det),
            )

        # Full JWT tokens (3-part base64url)
        for m in self._JWT_RE.finditer(text):
            result.setdefault("jwt_token", []).append(m.group(1))

        # Bearer tokens (may be partial JWT or opaque)
        for m in self._BEARER_RE.finditer(text):
            result.setdefault("jwt_token", []).append(m.group(1))

        # GitHub tokens
        for m in self._GITHUB_TOKEN_RE.finditer(text):
            result.setdefault("github_token", []).append(m.group(1))

        # AWS access keys
        for m in self._AWS_KEY_RE.finditer(text):
            result.setdefault("aws_access_key", []).append(m.group(1))

        # OpenAI API keys
        for m in self._OPENAI_KEY_RE.finditer(text):
            result.setdefault("openai_api_key", []).append(m.group(1))

        # Path traversal
        traversal_matches = self._PATH_TRAVERSAL_RE.findall(text)
        if traversal_matches:
            result["path_traversal"] = list(set(traversal_matches))

        # Custom patterns
        for cp in self._custom_patterns:
            for m in _re.finditer(cp.pattern, text):
                result.setdefault(cp.name, []).append(m.group(0))

        return result

    # -- Redaction / sanitization -----------------------------------------------

    def redact(self, text: str) -> str:
        """Redact detected secrets in *text*, replacing them with ``[REDACTED]``.

        Returns:
            Copy of *text* with all matched credential patterns substituted by ``[REDACTED]``.
        """
        from vetinari.learning.secrets_filter import redact_secrets

        result = redact_secrets(text)
        result = self._JWT_RE.sub("[REDACTED]", result)
        result = self._BEARER_RE.sub("Bearer [REDACTED]", result)
        result = self._GITHUB_TOKEN_RE.sub("[REDACTED]", result)
        result = self._AWS_KEY_RE.sub("[REDACTED]", result)
        result = self._OPENAI_KEY_RE.sub("[REDACTED]", result)

        for cp in self._custom_patterns:
            result = _re.sub(cp.pattern, "[REDACTED]", result)
        return result

    def sanitize(self, text: str) -> str:
        """Redact secrets and remove path-traversal sequences from *text*.

        Args:
            text: Text to sanitize.

        Returns:
            Sanitized text.
        """
        return self._PATH_TRAVERSAL_RE.sub("", self.redact(text))

    # Suffixes that make a compound key sensitive (e.g. user_token, db_password)
    _SENSITIVE_SUFFIXES = (
        "_password",
        "_passwd",
        "_pwd",
        "_secret",
        "_token",
        "_api_key",
        "_apikey",
        "_access_key",
        "_secret_key",
        "_private_key",
        "_credential",
        "_credentials",
        "_key",
    )

    def _is_sensitive_key(self, key: str) -> bool:
        """Return ``True`` if *key* is a known sensitive field name.

        Matches exact keys (``password``, ``api_key``) and compound keys
        ending with a sensitive suffix (``user_token``, ``db_password``).

        Args:
            key: Dictionary key to test.
        """
        normalised = key.lower().replace("-", "_") if isinstance(key, str) else ""
        if normalised in self._SENSITIVE_KEYS:
            return True
        return any(normalised.endswith(suffix) for suffix in self._SENSITIVE_SUFFIXES)

    def sanitize_dict(self, data: dict) -> dict:
        """Sanitize a dict by redacting sensitive-key values and secret patterns.

        Recurses into nested dicts, lists, and tuples so that secrets are
        redacted regardless of how deeply they are nested.  Tuples are
        preserved as tuples in the output (not converted to lists).

        Args:
            data: Dictionary to sanitize.

        Returns:
            New dictionary with sensitive values redacted.
        """
        result: dict = {}
        for k, v in data.items():
            if self._is_sensitive_key(k):
                result[k] = "[REDACTED]"
            elif isinstance(v, dict):
                result[k] = self.sanitize_dict(v)
            elif isinstance(v, list):
                result[k] = self._sanitize_list(v)
            elif isinstance(v, tuple):
                result[k] = tuple(
                    self.sanitize_dict(item)
                    if isinstance(item, dict)
                    else self.redact(item)
                    if isinstance(item, str)
                    else item
                    for item in v
                )
            elif isinstance(v, str):
                result[k] = self.redact(v)
            else:
                result[k] = v
        return result

    def _sanitize_list(self, items: list) -> list:
        """Sanitize each element in a list, redacting secrets in strings."""
        out: list = []
        for item in items:
            if isinstance(item, dict):
                out.append(self.sanitize_dict(item))
            elif isinstance(item, str):
                out.append(self.redact(item))
            elif isinstance(item, list):
                out.append(self._sanitize_list(item))
            else:
                out.append(item)
        return out


# -- Singleton ---------------------------------------------------------------

_scanner: SecretScanner | None = None
_scanner_lock = _threading.Lock()


def get_secret_scanner() -> SecretScanner:
    """Get or create the global secret scanner instance (thread-safe singleton).

    Mutating this scanner with ``add_pattern()`` affects every consumer that
    receives the singleton. Use ``new_secret_scanner()`` when per-consumer
    custom pattern isolation is required.

    Returns:
        The process-wide SecretScanner, constructed once and reused across all callers.
    """
    global _scanner
    if _scanner is None:
        with _scanner_lock:
            if _scanner is None:
                _scanner = SecretScanner()
    return _scanner


def new_secret_scanner() -> SecretScanner:
    """Create a fresh secret scanner with isolated custom patterns."""
    return SecretScanner()


def sanitize_for_memory(content: str) -> str:
    """Sanitize content before storing in memory."""
    return get_secret_scanner().sanitize(content)


def sanitize_dict_for_memory(data: dict) -> dict:
    """Sanitize a dictionary before storing in memory."""
    return get_secret_scanner().sanitize_dict(data)


def _normalize_hostname_to_ip(hostname: str) -> str:
    """Convert alternative IP representations to dotted-decimal for safe parsing.

    Python's ``ipaddress.ip_address()`` only accepts standard dotted-decimal (IPv4)
    or colon-hex (IPv6) notation.  Attackers can bypass naive checkers by encoding
    the loopback address as a decimal integer (2130706433), hex (0x7f000001), or
    octal-octet notation (0177.0.0.1) — all of which ``ipaddress`` would reject with
    ``ValueError``, causing the caller to treat them as DNS names and allow them.

    This helper detects each encoding and converts it to canonical form so the
    ``ipaddress`` module can apply its normal private/loopback/link-local checks.

    Args:
        hostname: The raw hostname extracted from the URL (lowercase, no port).

    Returns:
        A canonicalised hostname string ready for ``ipaddress.ip_address()``.
        Returns the original hostname unchanged if no alternative encoding is detected.

    Raises:
        SecurityError: If the hostname matches an alternative-IP encoding pattern but
            cannot be parsed (e.g. numeric value out of IPv4 range).
    """
    from vetinari.exceptions import SecurityError

    # Decimal integer IP: e.g. 2130706433 == 127.0.0.1
    # A pure decimal hostname with no dots is unambiguously an encoded IPv4 address.
    if hostname.isdigit():
        int_val = int(hostname)
        if 0 <= int_val <= 0xFFFFFFFF:
            return str(_ipaddress.ip_address(int_val))
        raise SecurityError(f"Numeric hostname {hostname!r} is out of the valid IPv4 range")

    # Hex-encoded IPv4: e.g. 0x7f000001 == 127.0.0.1
    if hostname.lower().startswith("0x"):
        try:
            int_val = int(hostname, 16)
            return str(_ipaddress.ip_address(int_val))
        except (ValueError, OverflowError) as exc:
            raise SecurityError(f"Hex hostname {hostname!r} could not be parsed as an IP address") from exc

    # Octal-octet notation: e.g. 0177.0.0.1 — each octet begins with a leading zero
    # and contains only digits (so it is ambiguously decimal or octal in some parsers).
    parts = hostname.split(".")
    if len(parts) == 4 and any(p.startswith("0") and len(p) > 1 and p.isdigit() for p in parts):
        try:
            decimal_parts = [str(int(p, 8)) for p in parts]
            return ".".join(decimal_parts)
        except (ValueError, OverflowError) as exc:
            raise SecurityError(f"Octal-notation hostname {hostname!r} could not be parsed as an IP address") from exc

    return hostname


def _resolve_dns_ips(hostname: str) -> list[_ipaddress.IPv4Address | _ipaddress.IPv6Address]:
    """Resolve a DNS hostname to IP address objects for SSRF range checks."""
    from vetinari.exceptions import SecurityError

    try:
        results = _socket.getaddrinfo(hostname, None, 0, _socket.SOCK_STREAM)
    except _socket.gaierror as exc:
        raise SecurityError(f"Hostname {hostname!r} could not be resolved") from exc

    addresses: list[_ipaddress.IPv4Address | _ipaddress.IPv6Address] = []
    for result in results:
        sockaddr = result[4]
        if not sockaddr:
            continue
        try:
            addresses.append(_ipaddress.ip_address(sockaddr[0]))
        except ValueError as exc:
            raise SecurityError(f"Resolved hostname {hostname!r} returned an invalid IP") from exc

    if not addresses:
        raise SecurityError(f"Hostname {hostname!r} did not resolve to any IP addresses")
    return addresses


def validate_url_no_ssrf(url: str) -> str:
    """Validate a URL is not targeting internal or private networks.

    Blocks private IPs, link-local addresses, loopback, and cloud metadata
    endpoints to prevent SSRF attacks.  Handles alternative IP encodings
    (decimal integer, hex, octal octet) that Python's ``ipaddress`` module
    does not natively parse, preventing bypass via encoding tricks.

    Only ``http`` and ``https`` schemes are permitted.

    Args:
        url: The URL to validate.

    Returns:
        The validated URL string (unchanged) when it passes all checks.

    Raises:
        SecurityError: If the URL targets a blocked address space, uses a
            disallowed scheme, or contains an unrecognised IP encoding.
    """
    from vetinari.exceptions import SecurityError

    parsed = _urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise SecurityError(f"URL scheme '{parsed.scheme}' not allowed — only http/https")

    hostname = parsed.hostname
    if not hostname:
        raise SecurityError("URL has no hostname")

    # Explicit hostname blocklist — catches aliases that ipaddress cannot parse.
    if hostname in _BLOCKED_HOSTNAMES:
        raise SecurityError(f"Hostname '{hostname}' is blocked")

    # Normalise alternative IP encodings before passing to ipaddress so that
    # decimal, hex, and octal representations are checked against private ranges.
    hostname_for_check = _normalize_hostname_to_ip(hostname)

    try:
        addr = _ipaddress.ip_address(hostname_for_check)
        if addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved:
            raise SecurityError(f"URL targets non-routable address: {hostname}")
    except ValueError:
        # ipaddress.ip_address() raised — hostname is a DNS name, not a raw IP.
        # Resolve it and apply the same address-space policy to every result.
        for resolved_addr in _resolve_dns_ips(hostname):
            if (
                resolved_addr.is_private
                or resolved_addr.is_loopback
                or resolved_addr.is_link_local
                or resolved_addr.is_reserved
            ):
                raise SecurityError(
                    f"URL hostname {hostname!r} resolves to non-routable address: {resolved_addr}"
                ) from None
        _logger.debug("Hostname %r resolved to public DNS addresses", hostname)

    return url
