"""Speculative decoding capability detection and configuration.

Speculative decoding accelerates inference by having a small *draft* model
generate candidate token sequences that the larger *target* model verifies in
a single forward pass.  When the draft model's predictions are accepted, the
target model produces multiple tokens per forward pass instead of one, yielding
1.5-3x throughput gains.

This module handles two concerns:

1. **Capability detection** — inspects the installed ``llama_cpp`` package at
   runtime to determine whether the ``Llama`` constructor accepts a
   ``draft_model`` keyword argument.  The result is cached using double-checked
   locking so the inspection only runs once per process.

2. **Configuration** — ``SpeculativeDecodingConfig`` is the structured config
   object read by ``LlamaCppProviderAdapter._get_speculative_config()`` and
   populated from ``VetinariSettings``.

Fallback chain when a draft model is unavailable or unsupported:
    draft_model kwarg present + draft_model_id configured
        → full speculative decoding
    draft_model kwarg present + no draft_model_id + use_prompt_lookup_fallback
        → PromptLookupDecoding (no extra VRAM, ~1.3-1.8x speedup)
    all else
        → standard inference, DEBUG log only, no error raised
"""

from __future__ import annotations

import inspect
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from types import ModuleType

from vetinari.utils.lazy_import import lazy_import

logger = logging.getLogger(__name__)


# Type alias for the import function accepted by _run_capability_detection.
# Matches lazy_import's signature: (module_name) -> (module_or_none, available).
_ImportFnType = Callable[[str], tuple[ModuleType | None, bool]]

# ── Module-level cache for capability detection ───────────────────────────────
# Populated exactly once per process by detect_speculative_capability().
# Guarded by double-checked locking (see quality-gates singleton pattern).

_capability_cache: SpeculativeDecodingCapability | None = None
_capability_lock: threading.Lock = threading.Lock()


# ── Data classes ─────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class SpeculativeDecodingCapability:
    """Result of runtime capability detection for speculative decoding.

    Attributes:
        supported: True when the installed llama_cpp exposes the
            ``draft_model`` parameter on ``Llama.__init__``.
        has_draft_model: True when a draft model has been configured in
            ``SpeculativeDecodingConfig``.  Always ``False`` when populated
            by ``detect_speculative_capability()`` alone — the adapter sets
            this field when building its config.
        draft_model_id: The configured draft model identifier, or ``None``.
        detection_method: Human-readable description of how capability was
            determined (e.g. ``"inspect.signature"`` or ``"llama_cpp_unavailable"``).
    """

    supported: bool  # True when llama_cpp.Llama.__init__ has draft_model param
    has_draft_model: bool  # True when a draft model ID is configured
    draft_model_id: str | None  # Configured draft model ID, or None
    detection_method: str  # How capability was determined

    def __repr__(self) -> str:
        return f"SpeculativeDecodingCapability(supported={self.supported}, draft_model_id={self.draft_model_id!r})"


@dataclass(frozen=True, slots=True)
class SpeculativeDecodingConfig:
    """User-facing configuration for speculative decoding.

    All fields have safe defaults so the feature is entirely opt-in.
    Populate from ``VetinariSettings`` via the adapter's
    ``_get_speculative_config()`` method.

    Attributes:
        enabled: Master switch — set to ``True`` to activate speculative
            decoding.  When ``False``, the adapter runs standard inference
            regardless of other settings.
        draft_model_id: Model identifier of the small/draft model used for
            token speculation.  Must be discoverable in the local models
            directory.  Leave ``None`` to rely on the PromptLookupDecoding
            fallback if ``use_prompt_lookup_fallback`` is ``True``.
        draft_n_tokens: Number of draft tokens the small model generates per
            speculation step.  Higher values improve throughput when the
            acceptance rate is high, but increase wasted work when it is low.
            Typical sweet spot: 5-10.
        use_prompt_lookup_fallback: When ``True`` and no ``draft_model_id``
            is configured (or the draft model cannot be loaded), fall back to
            ``LlamaPromptLookupDecoding``.  This requires no extra VRAM and
            yields ~1.3-1.8x speedup on structured/repetitive output.
    """

    enabled: bool = False  # Master switch — off by default
    draft_model_id: str | None = None  # Model ID for the draft/small model
    draft_n_tokens: int = 5  # Tokens to speculate per step (5 = balanced)
    use_prompt_lookup_fallback: bool = True  # Fall back to PromptLookupDecoding

    def __repr__(self) -> str:
        return (
            f"SpeculativeDecodingConfig(enabled={self.enabled!r}, "
            f"draft_model_id={self.draft_model_id!r}, "
            f"draft_n_tokens={self.draft_n_tokens!r})"
        )


# ── Capability detection ──────────────────────────────────────────────────────


def detect_speculative_capability(
    *,
    draft_model_id: str | None = None,
    _import_fn: _ImportFnType | None = None,
) -> SpeculativeDecodingCapability:
    """Detect whether the installed llama_cpp supports speculative decoding.

    Uses ``inspect.signature`` to check for the ``draft_model`` parameter on
    ``llama_cpp.Llama.__init__``.  The result is cached after the first call
    so repeated invocations are free.

    Args:
        draft_model_id: Optional model ID to embed in the returned capability
            object.  Does not affect the ``supported`` field.
        _import_fn: Override the import function used to load ``llama_cpp``.
            Defaults to the module-level ``lazy_import``.  Exposed for
            testing — production callers should never set this.

    Returns:
        A ``SpeculativeDecodingCapability`` describing what is available.
        When ``llama_cpp`` is not installed, ``supported`` is always ``False``
        and ``detection_method`` is ``"llama_cpp_unavailable"``.
    """
    global _capability_cache

    # When a custom import function is provided, always run fresh detection
    # (no caching).  This is used by tests to inject mocks without relying
    # on fragile module-namespace patching.
    if _import_fn is not None:
        return _run_capability_detection(
            draft_model_id=draft_model_id,
            _import_fn=_import_fn,
        )

    # Fast path — already cached
    if _capability_cache is not None and draft_model_id is None:
        return _capability_cache

    with _capability_lock:
        # Re-check under lock (double-checked locking)
        if _capability_cache is not None and draft_model_id is None:
            return _capability_cache

        result = _run_capability_detection(draft_model_id=draft_model_id)

        # Only cache when no caller-supplied draft_model_id so the cached
        # object remains generic (has_draft_model=False).
        if draft_model_id is None:
            _capability_cache = result

    return result


def _run_capability_detection(
    *,
    draft_model_id: str | None = None,
    _import_fn: _ImportFnType | None = None,
) -> SpeculativeDecodingCapability:
    """Run the actual capability detection without cache logic.

    Args:
        draft_model_id: Optional draft model ID to embed in the result.
        _import_fn: Override the import function used to load ``llama_cpp``.
            Defaults to the module-level ``lazy_import``.

    Returns:
        Fresh ``SpeculativeDecodingCapability`` instance.
    """
    import_fn = _import_fn if _import_fn is not None else lazy_import
    llama_cpp, available = import_fn("llama_cpp")

    if not available or llama_cpp is None:
        logger.debug("llama_cpp not available — speculative decoding not supported")
        return SpeculativeDecodingCapability(
            supported=False,
            has_draft_model=False,
            draft_model_id=None,
            detection_method="llama_cpp_unavailable",
        )

    try:
        llama_class = getattr(llama_cpp, "Llama", None)
        if llama_class is None:
            logger.debug("llama_cpp.Llama not found — speculative decoding not supported")
            return SpeculativeDecodingCapability(
                supported=False,
                has_draft_model=False,
                draft_model_id=None,
                detection_method="llama_class_missing",
            )

        sig = inspect.signature(llama_class.__init__)
        has_param = "draft_model" in sig.parameters

        logger.debug(
            "Speculative decoding capability check: draft_model param present=%s",
            has_param,
        )
        return SpeculativeDecodingCapability(
            supported=has_param,
            has_draft_model=draft_model_id is not None,
            draft_model_id=draft_model_id,
            detection_method="inspect.signature",
        )

    except (TypeError, ValueError) as exc:
        logger.warning(
            "Speculative decoding capability detection failed via inspect.signature — treating as unsupported: %s",
            exc,
        )
        return SpeculativeDecodingCapability(
            supported=False,
            has_draft_model=False,
            draft_model_id=None,
            detection_method="detection_error",
        )
