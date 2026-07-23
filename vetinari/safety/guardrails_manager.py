"""Guardrails manager implementation."""

from __future__ import annotations

import logging
import sys
import threading
import time
from importlib.util import find_spec
from typing import Any

from vetinari.guardrails.prompt_security import PROMPT_INJECTION_PATTERN_COUNT, VECTOR_CONTEXT_PATTERN_COUNT
from vetinari.safety.guardrails_checks import (
    _SENSITIVE_DATA_PATTERNS,
    _TOXIC_PATTERNS,
    _check_prompt_security,
    _check_sensitive_data,
    _check_toxic,
)
from vetinari.safety.guardrails_types import GuardrailResult, RailContext, Violation

logger = logging.getLogger(__name__)


def _module_is_available(module_name: str) -> bool:
    """Return True when an optional guardrail tier is discoverable without importing it."""
    if module_name in sys.modules:
        return sys.modules[module_name] is not None
    try:
        return find_spec(module_name) is not None
    except (ModuleNotFoundError, ValueError):
        logger.debug("Optional guardrail module probe failed for %s", module_name, exc_info=True)
        return False


class GuardrailsManager:
    """Built-in regex-based guardrails with optional provider augmentation."""

    _instance: GuardrailsManager | None = None
    _class_lock = threading.Lock()

    def __new__(cls) -> GuardrailsManager:
        """Return the shared singleton manager."""
        if cls._instance is None:
            with cls._class_lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._setup()
        return cls._instance

    def _setup(self) -> None:
        """Initialize instance-local synchronization."""
        self._lock = threading.RLock()

    def check_input(self, text: str, context: str = RailContext.USER_FACING) -> GuardrailResult:
        """Check user input against safety rails.

        Args:
            text: Input text to validate.
            context: Rail context applied to optional scanners.

        Returns:
            GuardrailResult with allowed status and any violations.
        """
        start = time.monotonic()

        violations = _check_prompt_security(text)
        violations += _check_sensitive_data(text)
        violations += _check_toxic(text)

        latency = (time.monotonic() - start) * 1000

        if violations:
            logger.warning("Input blocked: %d violation(s) detected", len(violations))
            self._log_guardrail_check("input", "denied", violations)
            return GuardrailResult(
                allowed=False,
                content=text,
                violations=violations,
                latency_ms=latency,
            )

        for optional_check in (
            lambda: self._check_nemo_input(text, start),
            lambda: self._check_llm_guard_input(text, context, start),
        ):
            optional_result = optional_check()
            if optional_result is not None:
                return optional_result

        self._log_guardrail_check("input", "allowed")
        return GuardrailResult(allowed=True, content=text, violations=violations, latency_ms=latency)

    def check_output(self, text: str, context: str = RailContext.USER_FACING) -> GuardrailResult:
        """Check bot output against safety rails.

        Args:
            text: Output text to validate.
            context: Rail context for output handling.

        Returns:
            GuardrailResult with allowed status and filtered content when blocked.
        """
        if context == RailContext.CODE_EXECUTION:
            from vetinari.security import get_secret_scanner

            scanner = get_secret_scanner()
            redacted = scanner.redact(text)
            if redacted != text:
                violations = [
                    Violation(
                        rail="sensitive_data",
                        severity="high",
                        description="Secrets detected in code execution output - redacted",
                        matched_pattern="",
                    )
                ]
                logger.warning("Secrets detected in CODE_EXECUTION output - redacted before returning")
                return GuardrailResult(allowed=True, content=redacted, violations=violations)
            return GuardrailResult(allowed=True, content=text)

        start = time.monotonic()

        violations = _check_sensitive_data(text)
        violations += _check_toxic(text)

        latency = (time.monotonic() - start) * 1000

        if violations:
            logger.warning("Output flagged: %d violation(s) - sensitive data detected", len(violations))
            self._log_guardrail_check("output", "denied", violations)
            return GuardrailResult(
                allowed=False,
                content="[Content filtered for safety]",
                violations=violations,
                latency_ms=latency,
            )

        for optional_check in (
            lambda: self._check_nemo_output(text, start),
            lambda: self._check_llm_guard_output(text, context, start),
        ):
            optional_result = optional_check()
            if optional_result is not None:
                return optional_result

        self._log_guardrail_check("output", "allowed")
        return GuardrailResult(allowed=True, content=text, violations=violations, latency_ms=latency)

    @staticmethod
    def _log_guardrail_check(check_type: str, outcome: str, violations: list[Violation] | None = None) -> None:
        """Best-effort audit logging for guardrail decisions."""
        try:
            from vetinari.audit import get_audit_logger

            payload: dict[str, Any] = {"check_type": check_type, "outcome": outcome}
            if violations is not None:
                payload["violations"] = [v.description for v in violations]
            get_audit_logger().log_guardrail_check(**payload)
        except Exception:
            logger.warning("Failed to log %s-%s guardrail check", check_type, outcome)

    def _check_nemo_input(self, text: str, start: float) -> GuardrailResult | None:
        """Run optional NeMo input checks and return a blocking result if needed."""
        try:
            if not _module_is_available("vetinari.safety.nemo_provider"):
                raise ModuleNotFoundError("vetinari.safety.nemo_provider")
            from vetinari.safety.nemo_provider import get_nemo_provider, is_nemo_init_failed

            nemo = get_nemo_provider()
            if nemo is None and is_nemo_init_failed():
                violation = Violation(
                    "nemo_colang",
                    "high",
                    "NeMo Guardrails provider init failed - input blocked by fail-closed policy",
                    "",
                )
                return GuardrailResult(False, text, [violation], (time.monotonic() - start) * 1000)
            if nemo is None:
                return None
            result = nemo.check_input(text)
            if result.allowed:
                return None
            self._log_guardrail_check("input", "denied", result.violations)
            return GuardrailResult(False, text, result.violations, (time.monotonic() - start) * 1000)
        except ModuleNotFoundError:
            logger.debug("nemoguardrails not installed - NeMo tier skipped for input")
            return None
        except Exception:
            logger.warning("NeMo Guardrails check_input raised - input blocked", exc_info=True)
            return GuardrailResult(False, text, [], (time.monotonic() - start) * 1000)

    @staticmethod
    def _check_llm_guard_input(text: str, context: str, start: float) -> GuardrailResult | None:
        """Run optional LLM Guard input checks and return a blocking result if needed."""
        try:
            if not _module_is_available("vetinari.safety.llm_guard_scanner"):
                raise ModuleNotFoundError("vetinari.safety.llm_guard_scanner")
            from vetinari.safety.llm_guard_scanner import get_llm_guard_scanner

            scanner = get_llm_guard_scanner()
            scan_result = scanner.scan_input(text, context=context)
            if scan_result.is_safe:
                return None
            violation = Violation("llm_guard_input", "high", "LLM Guard scanner flagged input", "")
            return GuardrailResult(False, scan_result.sanitized_text, [violation], (time.monotonic() - start) * 1000)
        except ModuleNotFoundError:
            logger.debug("llm-guard not installed - ML input scan skipped")
            return None
        except Exception:
            logger.warning("LLM Guard scanner raised - input blocked", exc_info=True)
            return GuardrailResult(False, text, [], (time.monotonic() - start) * 1000)

    def _check_nemo_output(self, text: str, start: float) -> GuardrailResult | None:
        """Run optional NeMo output checks and return a blocking result if needed."""
        try:
            if not _module_is_available("vetinari.safety.nemo_provider"):
                raise ModuleNotFoundError("vetinari.safety.nemo_provider")
            from vetinari.safety.nemo_provider import get_nemo_provider, is_nemo_init_failed

            nemo = get_nemo_provider()
            if nemo is None and is_nemo_init_failed():
                violation = Violation(
                    "nemo_colang",
                    "high",
                    "NeMo Guardrails provider init failed - output blocked by fail-closed policy",
                    "",
                )
                return GuardrailResult(False, "[Content blocked by NeMo Guardrails fail-closed policy]", [violation])
            if nemo is None:
                return None
            result = nemo.check_output(text)
            if result.allowed:
                return None
            self._log_guardrail_check("output", "denied", result.violations)
            return GuardrailResult(False, "[Content filtered by NeMo Guardrails]", result.violations)
        except ModuleNotFoundError:
            logger.debug("nemoguardrails not installed - NeMo tier skipped for output")
            return None
        except Exception:
            logger.warning("NeMo Guardrails check_output raised - output blocked", exc_info=True)
            return GuardrailResult(False, "[Content filtering error - output blocked for safety]", [])

    @staticmethod
    def _check_llm_guard_output(text: str, context: str, start: float) -> GuardrailResult | None:
        """Run optional LLM Guard output checks and return a blocking result if needed."""
        try:
            if not _module_is_available("vetinari.safety.llm_guard_scanner"):
                raise ModuleNotFoundError("vetinari.safety.llm_guard_scanner")
            from vetinari.safety.llm_guard_scanner import get_llm_guard_scanner

            scanner = get_llm_guard_scanner()
            scan_result = scanner.scan_output(prompt="", output=text, context=context) if scanner.available else None
            if scan_result is None or scan_result.is_safe:
                return None
            return GuardrailResult(False, "[Content filtered by ML scanner]", [], (time.monotonic() - start) * 1000)
        except ModuleNotFoundError:
            logger.debug("llm-guard not installed - ML content scan skipped")
            return None
        except Exception:
            logger.warning("LLM Guard scanner raised - output blocked", exc_info=True)
            return GuardrailResult(False, "[Content filtering error - output blocked for safety]", [])

    def redact_pii(self, text: str) -> str:
        """Replace detected PII with ``[REDACTED]`` instead of blocking.

        Args:
            text: Text that may contain sensitive data.

        Returns:
            Text with sensitive matches replaced.
        """
        with self._lock:
            result = text
            for pattern in _SENSITIVE_DATA_PATTERNS:
                result = pattern.sub("[REDACTED]", result)
            return result

    def check_both(
        self,
        input_text: str,
        output_text: str,
        context: str = RailContext.USER_FACING,
    ) -> tuple[GuardrailResult, GuardrailResult]:
        """Run input and output checks and return both results.

        Args:
            input_text: Input text to validate.
            output_text: Output text to validate.
            context: Rail context applied to both checks.

        Returns:
            Pair of input and output guardrail results.
        """
        input_result = self.check_input(input_text, context=context)
        output_result = self.check_output(output_text, context=context)
        return input_result, output_result

    def get_rails_for_context(self, context_type: str) -> list[str]:
        """Return which rail categories apply for a given context type.

        Args:
            context_type: One of the RailContext constants.

        Returns:
            Rail category names for the context.
        """
        if context_type == RailContext.USER_FACING:
            return ["jailbreak", "vector_context", "toxic", "prompt_injection", "sensitive_data"]
        if context_type == RailContext.INTERNAL_AGENT:
            return ["jailbreak", "vector_context", "toxic", "prompt_injection", "sensitive_data"]
        if context_type == RailContext.CODE_EXECUTION:
            return ["jailbreak", "vector_context", "prompt_injection"]
        return []

    def get_stats(self) -> dict[str, Any]:
        """Return introspection statistics about the guardrails configuration."""
        return {
            "builtin_jailbreak_patterns": PROMPT_INJECTION_PATTERN_COUNT,
            "builtin_vector_context_patterns": VECTOR_CONTEXT_PATTERN_COUNT,
            "builtin_sensitive_patterns": len(_SENSITIVE_DATA_PATTERNS),
            "builtin_toxic_patterns": len(_TOXIC_PATTERNS),
        }

    def get_active_rails(self) -> dict[str, list[str]]:
        """Return active rail categories keyed by context type.

        Returns:
            Mapping of active RailContext values to rail category names.
        """
        active: dict[str, list[str]] = {}
        for context_type in (
            RailContext.USER_FACING,
            RailContext.INTERNAL_AGENT,
            RailContext.CODE_EXECUTION,
        ):
            rails = self.get_rails_for_context(context_type)
            if rails:
                active[context_type] = rails
        return active
