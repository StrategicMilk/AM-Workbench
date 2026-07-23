"""NeMo Guardrails provider — Colang-based safety layer for Vetinari.

Wraps NVIDIA NeMo Guardrails (``nemoguardrails``) to apply Colang DSL policies
as a mid-tier check in the safety stack.  The check order is:

    regex (fast) → NeMo / Colang (policy) → LLM Guard (ML)

NeMo is an optional dependency.  When not installed the provider returns
``None`` from ``get_nemo_provider()`` and ``GuardrailsManager`` skips the
NeMo tier silently.  All exceptions inside the provider fail CLOSED
(``allowed=False``) so a flaky NeMo installation can never open a hole in
the safety stack.

Usage::

    from vetinari.safety.nemo_provider import get_nemo_provider

    provider = get_nemo_provider()
    if provider is not None:
        result = provider.check_input("user prompt here")
        if not result.allowed:
            ...
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections.abc import Callable
from pathlib import Path

from vetinari.safety.guardrails import GuardrailResult, Violation
from vetinari.safety.guardrails_checks import (
    _check_jailbreak,
    _check_prompt_injection,
    _check_sensitive_data,
    _check_toxic,
)
from vetinari.utils.lazy_import import lazy_import

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Block-phrase detection for NeMo responses
# ---------------------------------------------------------------------------
# NeMo signals a policy block by returning a canned "I can't help" response.
# These phrases are checked case-insensitively against the generate() output.
# Module-level constant avoids re-creating the tuple on every call.

_BLOCK_PHRASES = (
    "i'm not able to",
    "i cannot",
    "i can't help",
    "i am not able to",
    "i'm unable to",
    "i refuse",
    "this request has been blocked",
    "content policy",
    "cannot assist",
)

# ---------------------------------------------------------------------------
# Lazy-load nemoguardrails — optional dependency
# ---------------------------------------------------------------------------

_nemoguardrails, _NEMO_AVAILABLE = lazy_import("nemoguardrails")

# ---------------------------------------------------------------------------
# Config path
# ---------------------------------------------------------------------------

_GUARDRAILS_CONFIG_DIR = Path(__file__).resolve().parents[1] / "config" / "runtime" / "guardrails"
_EXECUTE_ACTION_RE = re.compile(r"\bexecute\s+([A-Za-z_][A-Za-z0-9_]*)\b")


def _action_text(*, text: str | None = None, user_input: str | None = None, bot_message: str | None = None) -> str:
    """Return the first Colang action text argument NeMo supplied."""
    return text if text is not None else user_input if user_input is not None else bot_message or ""


def _check_jailbreak_action(*, user_input: str | None = None, text: str | None = None, **_: object) -> bool:
    return bool(_check_jailbreak(_action_text(user_input=user_input, text=text)))


def _check_prompt_injection_action(*, user_input: str | None = None, text: str | None = None, **_: object) -> bool:
    return bool(_check_prompt_injection(_action_text(user_input=user_input, text=text)))


def _check_sensitive_data_action(
    *, text: str | None = None, bot_message: str | None = None, user_input: str | None = None, **_: object
) -> bool:
    return bool(_check_sensitive_data(_action_text(text=text, bot_message=bot_message, user_input=user_input)))


def _check_toxic_action(
    *, text: str | None = None, user_input: str | None = None, bot_message: str | None = None, **_: object
) -> bool:
    return bool(_check_toxic(_action_text(text=text, user_input=user_input, bot_message=bot_message)))


_ACTION_REGISTRY: dict[str, Callable[..., bool]] = {
    "check_jailbreak": _check_jailbreak_action,
    "check_prompt_injection": _check_prompt_injection_action,
    "check_sensitive_data": _check_sensitive_data_action,
    "check_toxic": _check_toxic_action,
}


# ---------------------------------------------------------------------------
# NeMoGuardrailsProvider
# ---------------------------------------------------------------------------


class NeMoGuardrailsProvider:
    """Colang-based safety provider backed by NVIDIA NeMo Guardrails.

    Loads Colang DSL policies from ``config/guardrails/`` and applies them to
    input and output text via ``LLMRails``.  All unexpected exceptions fail
    CLOSED — the caller is blocked, not allowed through.

    Instances should be obtained via ``get_nemo_provider()`` rather than
    constructed directly.

    Raises:
        ImportError: At construction time if ``nemoguardrails`` is not installed.
        RuntimeError: If the Colang config directory does not exist or cannot
            be loaded.
    """

    def __init__(self, config_dir: Path | None = None) -> None:
        """Initialise the provider and load Colang config.

        Args:
            config_dir: Path to the directory containing ``config.yml`` and
                ``*.co`` Colang files.  Defaults to
                ``<repo_root>/config/guardrails/``.

        Raises:
            ImportError: If ``nemoguardrails`` is not installed.
            RuntimeError: If the config directory is missing or the config
                cannot be parsed.
        """
        if not _NEMO_AVAILABLE or _nemoguardrails is None:
            raise ImportError("nemoguardrails is not installed. Install it with: pip install 'vetinari[guardrails]'")

        resolved_dir = config_dir or _GUARDRAILS_CONFIG_DIR

        if not resolved_dir.exists():
            raise RuntimeError(
                f"NeMo Guardrails config directory not found: {resolved_dir}. "
                "Ensure config/guardrails/ exists with config.yml and *.co files."
            )

        logger.info("Loading NeMo Guardrails config from %s", resolved_dir)

        try:
            rails_config_cls = _nemoguardrails.RailsConfig
            rails_cls = _nemoguardrails.LLMRails
            assert_colang_actions_registered(resolved_dir)
            self._rails_config = rails_config_cls.from_path(str(resolved_dir))
            self._rails = rails_cls(self._rails_config)
            self._register_actions()
        except Exception as exc:
            raise RuntimeError(f"Failed to load NeMo Guardrails config from {resolved_dir}: {exc}") from exc

        logger.info("NeMo Guardrails provider initialised successfully")

    def _register_actions(self) -> None:
        """Register Python implementations for Colang execute actions."""
        for action_name, handler in _ACTION_REGISTRY.items():
            self._rails.register_action(handler, action_name)

    # ------------------------------------------------------------------
    # Input checking
    # ------------------------------------------------------------------

    def check_input(self, text: str) -> GuardrailResult:
        """Apply Colang input rails to user-provided text.

        Runs the NeMo ``generate`` pipeline in check-only mode.  If NeMo
        blocks the message (returns an alternative/canned response indicating
        a policy hit), the result is ``allowed=False``.  Any exception from
        the NeMo stack also fails CLOSED.

        Args:
            text: The user input text to check.

        Returns:
            GuardrailResult with ``allowed=True`` if the text passes all
            Colang input policies, or ``allowed=False`` if blocked.
        """
        start = time.monotonic()
        try:
            response = self._rails.generate(messages=[{"role": "user", "content": text}])
            # NeMo returns a blocked/canned response when a rail fires.
            # The canonical signal is that the response content differs from
            # normal generation — specifically NeMo sets the response to the
            # bot_refuse_to_respond or similar canned action output.
            # We detect this by checking whether the rails flagged the message.
            blocked = self._is_blocked_response(response)
            activated_rails = self._extract_activated_rails(response)
            latency = (time.monotonic() - start) * 1000

            if blocked:
                logger.warning("NeMo Guardrails blocked input — Colang policy triggered")
                return GuardrailResult(
                    allowed=False,
                    content=text,
                    violations=[
                        Violation(
                            rail="nemo_colang",
                            severity="high",
                            description="NeMo Guardrails Colang policy triggered on input",
                            matched_pattern="",
                        )
                    ],
                    latency_ms=latency,
                    activated_rails=activated_rails,
                )

            return GuardrailResult(allowed=True, content=text, latency_ms=latency, activated_rails=activated_rails)

        except Exception as exc:
            # Fail closed — NeMo errors must never allow content through.
            latency = (time.monotonic() - start) * 1000
            logger.warning(
                "NeMo Guardrails check_input raised an unexpected error — input blocked (fail-closed): %s",
                exc,
            )
            return GuardrailResult(
                allowed=False,
                content=text,
                violations=[
                    Violation(
                        rail="nemo_colang",
                        severity="high",
                        description="NeMo Guardrails internal error — blocked for safety",
                        matched_pattern="",
                    )
                ],
                latency_ms=latency,
            )

    # ------------------------------------------------------------------
    # Output checking
    # ------------------------------------------------------------------

    def check_output(self, text: str) -> GuardrailResult:
        """Apply Colang output rails to bot-generated text.

        Runs the NeMo pipeline in output-check mode.  Any policy hit or
        internal exception fails CLOSED.

        Args:
            text: The bot output text to check.

        Returns:
            GuardrailResult with ``allowed=True`` if the text passes all
            Colang output policies, or ``allowed=False`` if blocked.
        """
        start = time.monotonic()
        try:
            # For output checking, we provide context so NeMo evaluates
            # the assistant message against output rails.
            response = self._rails.generate(
                messages=[
                    {"role": "user", "content": ""},
                    {"role": "assistant", "content": text},
                ]
            )
            blocked = self._is_blocked_response(response)
            activated_rails = self._extract_activated_rails(response)
            latency = (time.monotonic() - start) * 1000

            if blocked:
                logger.warning("NeMo Guardrails blocked output — Colang output policy triggered")
                return GuardrailResult(
                    allowed=False,
                    content="[Content filtered by NeMo Guardrails]",
                    violations=[
                        Violation(
                            rail="nemo_colang",
                            severity="high",
                            description="NeMo Guardrails Colang output policy triggered",
                            matched_pattern="",
                        )
                    ],
                    latency_ms=latency,
                    activated_rails=activated_rails,
                )

            return GuardrailResult(allowed=True, content=text, latency_ms=latency, activated_rails=activated_rails)

        except Exception as exc:
            latency = (time.monotonic() - start) * 1000
            logger.warning(
                "NeMo Guardrails check_output raised an unexpected error — output blocked (fail-closed): %s",
                exc,
            )
            return GuardrailResult(
                allowed=False,
                content="[Content filtering error — output blocked for safety]",
                violations=[
                    Violation(
                        rail="nemo_colang",
                        severity="high",
                        description="NeMo Guardrails internal error — blocked for safety",
                        matched_pattern="",
                    )
                ],
                latency_ms=latency,
            )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_activated_rails(response: object) -> list[str]:
        """Extract the list of activated rail names from a NeMo generate() response.

        NeMo populates ``response["metadata"]["activated_rails"]`` with the
        names of any Colang flows that fired during evaluation.  Returns an
        empty list when the field is absent or the response is not a dict.

        Args:
            response: The raw return value from ``LLMRails.generate()``.

        Returns:
            List of activated rail name strings, or empty list if none fired.
        """
        if not isinstance(response, dict):
            return []
        metadata = response.get("metadata", {})
        if not isinstance(metadata, dict):
            return []
        rails = metadata.get("activated_rails", [])
        if not isinstance(rails, list):
            return []
        return [str(r) for r in rails]

    @staticmethod
    def _is_blocked_response(response: object) -> bool:
        """Determine whether a NeMo generate() response indicates a policy block.

        NeMo signals a block by returning a canned "I can't help with that"
        style response via ``bot refuse to respond`` or similar actions in
        Colang.  The response object is either a string or a dict with a
        ``content`` key.

        Args:
            response: The raw return value from ``LLMRails.generate()``.

        Returns:
            ``True`` if the response indicates a policy block, ``False`` if
            the content passed through normally.
        """
        if isinstance(response, dict):
            content = str(response.get("content", "")).lower()
        elif isinstance(response, str):
            content = response.lower()
        else:
            content = str(response).lower()

        return any(phrase in content for phrase in _BLOCK_PHRASES)


# ---------------------------------------------------------------------------
# Singleton with double-checked locking
# ---------------------------------------------------------------------------

_nemo_instance: NeMoGuardrailsProvider | None = None
_nemo_lock = threading.Lock()

# Set to True when nemoguardrails is installed but provider init fails.
# Callers use this to distinguish "not installed" (silent skip) from
# "installed but broken" (degraded-safety violation).
_nemo_init_failed: bool = False


def get_nemo_provider(config_dir: Path | None = None) -> NeMoGuardrailsProvider | None:
    """Return the singleton NeMoGuardrailsProvider, or None if not available.

    Uses double-checked locking.  Returns ``None`` when ``nemoguardrails`` is
    not installed (silent skip) or when the provider failed to initialise
    (``_nemo_init_failed`` is set to ``True`` so callers can mark results as
    having a degraded safety check rather than silently skipping the tier).

    Args:
        config_dir: Override the Colang config directory.  Only used on the
            first call — subsequent calls return the cached instance.

    Returns:
        The shared NeMoGuardrailsProvider instance, or ``None`` if
        ``nemoguardrails`` is not installed or the config cannot be loaded.
    """
    global _nemo_instance, _nemo_init_failed

    if not _NEMO_AVAILABLE:
        return None

    if _nemo_instance is None:
        with _nemo_lock:
            if _nemo_instance is None:
                try:
                    _nemo_instance = NeMoGuardrailsProvider(config_dir=config_dir)
                except Exception as exc:
                    logger.warning(
                        "NeMo Guardrails provider could not be initialised — NeMo tier disabled: %s",
                        exc,
                    )
                    _nemo_init_failed = True
                    return None

    return _nemo_instance


def is_nemo_init_failed() -> bool:
    """Return True if NeMo Guardrails was installed but failed to initialise.

    Distinguishes a broken install (init raised) from a clean missing-dep
    situation.  GuardrailsManager uses this to mark results as degraded rather
    than silently skipping the NeMo tier.

    Returns:
        True if the provider init raised during the last attempt, False otherwise.
    """
    return _nemo_init_failed


def reset_nemo_provider() -> None:
    """Destroy the singleton so the next call to ``get_nemo_provider()`` recreates it.

    Also clears the ``_nemo_init_failed`` flag so tests can simulate a fresh
    provider state.

    Intended for testing only.
    """
    global _nemo_instance, _nemo_init_failed
    with _nemo_lock:
        _nemo_instance = None
        _nemo_init_failed = False


def colang_execute_actions(config_dir: Path) -> set[str]:
    """Return every Colang execute action referenced by ``*.co`` files.

    Returns:
        Set of action names referenced by Colang ``execute`` statements.
    """
    actions: set[str] = set()
    for path in config_dir.rglob("*.co"):
        for line in path.read_text(encoding="utf-8").splitlines():
            executable_line = line.split("#", 1)[0]
            actions.update(_EXECUTE_ACTION_RE.findall(executable_line))
    return actions


def registered_action_names() -> set[str]:
    """Return provider action names wired into NeMo rails."""
    return set(_ACTION_REGISTRY)


def assert_colang_actions_registered(config_dir: Path | None = None) -> None:
    """Fail closed when Colang references an unregistered Python action.

    Raises:
        RuntimeError: If any referenced Colang action lacks a Python handler.
    """
    root = config_dir or _GUARDRAILS_CONFIG_DIR
    missing = sorted(colang_execute_actions(root) - registered_action_names())
    if missing:
        raise RuntimeError(f"NeMo Colang action(s) lack Python registrations: {', '.join(missing)}")
