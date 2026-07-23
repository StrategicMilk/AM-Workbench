"""Trusted chat template registry for GGUF models.

Prevents chat template injection by maintaining a curated list of known-good
templates and quarantining models whose embedded template does not match.

This is a defense-in-depth measure: GGUF files can embed arbitrary Jinja2
templates in their metadata, and a malicious template could alter model
behavior or leak context.  The registry validates templates before use and
falls back to safe defaults when a mismatch is detected.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any

from vetinari.boundary_guards import route_enum_error
from vetinari.security.redaction import redact_text

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Known-good template hashes (SHA-256 of the raw template string)
# ---------------------------------------------------------------------------

# Fallback templates keyed by model family.  Used when the model's embedded
# template fails validation.
_FALLBACK_TEMPLATES: dict[str, str] = {
    "chatml": (
        "{% for message in messages %}"
        "<|im_start|>{{ message.role }}\n{{ message.content }}<|im_end|>\n"
        "{% endfor %}"
        "<|im_start|>assistant\n"
    ),
    "llama": (
        "{% for message in messages %}"
        "{% if message.role == 'system' %}"
        "<|begin_of_text|><|start_header_id|>system<|end_header_id|>\n{{ message.content }}<|eot_id|>"
        "{% elif message.role == 'user' %}"
        "<|start_header_id|>user<|end_header_id|>\n{{ message.content }}<|eot_id|>"
        "{% elif message.role == 'assistant' %}"
        "<|start_header_id|>assistant<|end_header_id|>\n{{ message.content }}<|eot_id|>"
        "{% endif %}"
        "{% endfor %}"
        "<|start_header_id|>assistant<|end_header_id|>\n"
    ),
    "mistral": (
        "{% for message in messages %}"
        "{% if message.role == 'user' %}"
        "[INST] {{ message.content }} [/INST]"
        "{% elif message.role == 'assistant' %}"
        " {{ message.content }}</s>"
        "{% endif %}"
        "{% endfor %}"
    ),
    "phi": (
        "{% for message in messages %}"
        "<|im_start|>{{ message.role }}\n{{ message.content }}<|im_end|>\n"
        "{% endfor %}"
        "<|im_start|>assistant\n"
    ),
    "gemma": (
        "{% for message in messages %}"
        "{% if message.role == 'user' %}"
        "<start_of_turn>user\n{{ message.content }}<end_of_turn>\n"
        "{% elif message.role == 'assistant' %}"
        "<start_of_turn>model\n{{ message.content }}<end_of_turn>\n"
        "{% endif %}"
        "{% endfor %}"
        "<start_of_turn>model\n"
    ),
    "default": (
        "{% for message in messages %}"
        "<|im_start|>{{ message.role }}\n{{ message.content }}<|im_end|>\n"
        "{% endfor %}"
        "<|im_start|>assistant\n"
    ),
}

_TRUSTED_TEMPLATE_HASHES: dict[str, str] = {
    hashlib.sha256(template.encode("utf-8")).hexdigest(): family for family, template in _FALLBACK_TEMPLATES.items()
}


class TrustEnum(str, Enum):
    """Template trust decision."""

    TRUSTED = "trusted"
    UNTRUSTED = "untrusted"


@dataclass(frozen=True, slots=True)
class TemplateValidation:
    """Result of validating a model's chat template."""

    is_trusted: bool
    template_hash: str
    matched_family: str  # e.g. "chatml", "llama", "" if unknown
    fallback_used: bool  # True if the model's template was replaced

    def __repr__(self) -> str:
        return (
            f"TemplateValidation(trusted={self.is_trusted}, "
            f"family={self.matched_family!r}, fallback={self.fallback_used})"
        )


def _hash_template(template: str) -> str:
    """Compute SHA-256 hash of a template string.

    Args:
        template: Raw template string.

    Returns:
        Hex-encoded SHA-256 digest.
    """
    return hashlib.sha256(template.encode("utf-8")).hexdigest()


def _resolve_template_trust(raw_validation_result: object) -> TrustEnum:
    try:
        return TrustEnum(raw_validation_result)
    except ValueError:
        logger.warning("Invalid template trust validation result: %r", raw_validation_result)
        return route_enum_error(TrustEnum, raw_validation_result, TrustEnum.UNTRUSTED, "template_trust")


def _detect_family(model_id: str) -> str:
    """Heuristically detect the model family from its name or path.

    Args:
        model_id: Model name, path, or identifier string.

    Returns:
        Family key (e.g. "chatml", "llama") or "default" if unknown.
    """
    lower = model_id.lower()

    if any(tag in lower for tag in ("qwen", "dolphin", "hermes", "openhermes", "chatml", "nous")):
        return "chatml"
    if any(tag in lower for tag in ("llama", "meta-llama", "codellama")):
        return "llama"
    if any(tag in lower for tag in ("mistral", "mixtral", "zephyr")):
        return "mistral"
    if any(tag in lower for tag in ("phi-", "phi2", "phi3", "phi4")):
        return "phi"
    if "gemma" in lower:
        return "gemma"

    return "default"


def validate_template(
    model_id: str,
    embedded_template: str | None,
) -> tuple[str, TemplateValidation]:
    """Validate a model's chat template and return the template to use.

    If the embedded template is recognized (hash matches a trusted template),
    it is returned as-is.  Otherwise, a safe fallback for the model's family
    is returned instead.

    Args:
        model_id: Model name or path, used for family detection.
        embedded_template: The template string from the model's metadata,
            or ``None`` if the model has no embedded template.

    Returns:
        Tuple of (template_to_use, TemplateValidation).
    """
    family = _detect_family(model_id)
    safe_model_id = redact_text(model_id)

    if embedded_template is None:
        # No embedded template — use fallback
        fallback = _FALLBACK_TEMPLATES.get(family, _FALLBACK_TEMPLATES["default"])
        logger.info(
            "Model %s has no embedded chat template — using %s fallback",
            safe_model_id,
            family,
        )
        return fallback, TemplateValidation(
            is_trusted=False,
            template_hash="",
            matched_family=family,
            fallback_used=True,
        )

    template_hash = _hash_template(embedded_template)

    matched_family = _TRUSTED_TEMPLATE_HASHES.get(template_hash)
    raw_validation_result = TrustEnum.TRUSTED.value if matched_family is not None else TrustEnum.UNTRUSTED.value
    trust = _resolve_template_trust(raw_validation_result)
    if trust is TrustEnum.TRUSTED:
        return embedded_template, TemplateValidation(
            is_trusted=True,
            template_hash=template_hash,
            matched_family=matched_family,
            fallback_used=False,
        )

    # Template looks suspicious — quarantine and use fallback
    fallback = _FALLBACK_TEMPLATES.get(family, _FALLBACK_TEMPLATES["default"])
    logger.warning(
        "Model %s has an untrusted chat template (hash=%s) — using %s fallback. "
        "The model's embedded template has been quarantined.",
        safe_model_id,
        template_hash[:16],
        family,
    )
    return fallback, TemplateValidation(
        is_trusted=False,
        template_hash=template_hash,
        matched_family=family,
        fallback_used=True,
    )


def _get_fallback_template(model_id: str) -> str:
    """Return the safe fallback template for a given model.

    Args:
        model_id: Model name or path.

    Returns:
        The fallback chat template string for the detected model family.
    """
    family = _detect_family(model_id)
    return _FALLBACK_TEMPLATES.get(family, _FALLBACK_TEMPLATES["default"])


def get_stats() -> dict[str, Any]:
    """Return template registry statistics.

    Returns:
        Dict with fallback_templates count and supported_families list.
    """
    return {
        "fallback_templates": len(_FALLBACK_TEMPLATES),
        "trusted_template_hashes": len(_TRUSTED_TEMPLATE_HASHES),
        "supported_families": list(_FALLBACK_TEMPLATES.keys()),
    }
