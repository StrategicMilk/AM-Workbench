"""Pure helpers for the llama.cpp provider adapter."""

from __future__ import annotations

import hashlib
import io
import json
import logging
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any

from vetinari.adapters.base import InferenceRequest
from vetinari.adapters.grammar_library import get_grammar_for_task_type, validate_grammar
from vetinari.adapters.llama_cpp_model_info import _JSON_RESPONSE_FORMAT

if TYPE_CHECKING:
    from vetinari.models.chat_templates import TemplateValidation

logger = logging.getLogger(__name__)


SYSTEM_PROMPT_BOUNDARY = "<<<CONTEXT_BOUNDARY>>>"
ADAPTER_CACHE_VERSION = "llama_cpp_adapter:v2"
_ARTIFACT_HASH_CACHE_MAX = 128

_ARTIFACT_HASH_CACHE: dict[tuple[str, int, int], str] = {}
_ARTIFACT_HASH_LOCK = threading.Lock()


def _artifact_sha256(path: Path) -> str:
    """Return a memoized SHA-256 digest for a resolved GGUF artifact."""
    resolved = path.resolve()
    stat = resolved.stat()
    cache_key = (str(resolved), stat.st_size, stat.st_mtime_ns)
    with _ARTIFACT_HASH_LOCK:
        cached = _ARTIFACT_HASH_CACHE.get(cache_key)
        if cached is not None:
            return cached

    digest = hashlib.sha256()
    with io.FileIO(resolved, "r") as artifact:
        for chunk in iter(lambda: artifact.read(1024 * 1024), b""):
            digest.update(chunk)
    value = digest.hexdigest()

    with _ARTIFACT_HASH_LOCK:
        if len(_ARTIFACT_HASH_CACHE) >= _ARTIFACT_HASH_CACHE_MAX:
            _ARTIFACT_HASH_CACHE.pop(next(iter(_ARTIFACT_HASH_CACHE)))
        _ARTIFACT_HASH_CACHE[cache_key] = value
    return value


def _text_ref(text: str) -> str:
    """Return a non-reversible reference for prompt/cache identity text."""
    if not text:
        return ""
    return f"sha256:{hashlib.sha256(text.encode('utf-8')).hexdigest()}:len:{len(text)}"


def _semantic_cache_identity(
    request: InferenceRequest,
    *,
    model_path: Path,
    resolved_model_id: str,
    resolution_outcome: str,
) -> tuple[str, str]:
    """Build semantic-cache isolation keys from output-affecting identity."""
    artifact_hash = _artifact_sha256(model_path)
    grammar = request.grammar
    if grammar is None and request.task_type:
        grammar = get_grammar_for_task_type(request.task_type)
        if grammar and not validate_grammar(grammar):
            grammar = None

    sampler_identity = {
        "adapter_version": ADAPTER_CACHE_VERSION,
        "artifact_ref": f"sha256:{artifact_hash}",
        "artifact_sha256": artifact_hash,
        "fallback_outcome": resolution_outcome,
        "grammar": grammar,
        "logit_bias": request.logit_bias,
        "max_tokens": request.max_tokens,
        "metadata_response_format": request.metadata.get("response_format"),
        "mirostat_eta": request.mirostat_eta,
        "mirostat_mode": request.mirostat_mode,
        "mirostat_tau": request.mirostat_tau,
        "min_p": request.min_p,
        "presence_penalty": request.presence_penalty,
        "frequency_penalty": request.frequency_penalty,
        "repeat_penalty": request.repeat_penalty,
        "requested_model_id": request.model_id,
        "resolved_model_id": resolved_model_id,
        "response_format": request.response_format,
        "seed": request.seed,
        "stop_sequences": request.stop_sequences,
        "system_prompt_ref": _text_ref(request.system_prompt or ""),
        "task_type": request.task_type,
        "temperature": request.temperature,
        "template": request.metadata.get("chat_template") or request.metadata.get("template"),
        "tfs_z": request.tfs_z,
        "top_k": request.top_k,
        "top_p": request.top_p,
        "typical_p": request.typical_p,
    }
    cache_context = json.dumps(sampler_identity, sort_keys=True, separators=(",", ":"), default=str)
    cache_model_id = f"{resolved_model_id}:{artifact_hash}"
    return cache_model_id, cache_context


def _build_completion_kwargs(
    request: InferenceRequest,
    messages: list[dict[str, str]],
    *,
    stream: bool = False,
) -> dict[str, Any]:
    """Build kwargs dict for llama_cpp create_chat_completion."""
    _min_p = request.min_p if request.min_p > 0 else 0.05
    kwargs: dict[str, Any] = {
        "messages": messages,
        "max_tokens": request.max_tokens,
        "temperature": request.temperature,
        "top_p": request.top_p,
        "top_k": request.top_k,
        "min_p": _min_p,
        "stop": request.stop_sequences or None,
        "repeat_penalty": request.repeat_penalty,
        "frequency_penalty": request.frequency_penalty,
        "presence_penalty": request.presence_penalty,
    }
    if not stream:
        kwargs["logprobs"] = True
    if stream:
        kwargs["stream"] = True
    if request.mirostat_mode > 0:
        kwargs["mirostat_mode"] = request.mirostat_mode
        kwargs["mirostat_tau"] = request.mirostat_tau
        kwargs["mirostat_eta"] = request.mirostat_eta
        kwargs.pop("top_p", None)
        kwargs.pop("top_k", None)
        kwargs.pop("min_p", None)
    if request.seed >= 0:
        kwargs["seed"] = request.seed

    _request_grammar = request.grammar
    if _request_grammar is None and request.task_type:
        _request_grammar = get_grammar_for_task_type(request.task_type)
        if _request_grammar:
            logger.debug(
                "Auto-selected grammar for task_type='%s'",
                request.task_type,
            )
    if _request_grammar:
        if validate_grammar(_request_grammar):
            kwargs["grammar"] = _request_grammar
        else:
            logger.warning(
                "Invalid grammar for task_type='%s' - skipping grammar enforcement",
                request.task_type,
            )
    if request.logit_bias:
        kwargs["logit_bias"] = request.logit_bias
    if request.typical_p > 0:
        kwargs["typical_p"] = request.typical_p
    if request.tfs_z > 0:
        kwargs["tfs_z"] = request.tfs_z
    _wants_json = (
        request.response_format == "json"
        or request.metadata.get("response_format") == "json"
        or (not stream and "respond only with valid json" in request.prompt[-200:].lower())
    )
    if _wants_json:
        kwargs["response_format"] = _JSON_RESPONSE_FORMAT
    return kwargs


def _extract_embedded_chat_template(llm: Any) -> str | None:
    """Extract a llama.cpp embedded chat template from known metadata fields."""
    for attr_name in ("metadata", "model_metadata", "_metadata"):
        metadata = getattr(llm, attr_name, None)
        if callable(metadata):
            try:
                metadata = metadata()
            except Exception as exc:
                logger.warning("Could not read llama.cpp metadata attribute %s: %s", attr_name, exc)
                continue
        if not isinstance(metadata, dict):
            continue
        for key in (
            "tokenizer.chat_template",
            "tokenizer.ggml.chat_template",
            "chat_template",
            "llama.chat_template",
        ):
            value = metadata.get(key)
            if isinstance(value, str) and value.strip():
                return value
    return None


def _validate_loaded_chat_template(model_id: str, llm: Any) -> TemplateValidation | None:
    """Validate an embedded GGUF chat template when one is available."""
    embedded_template = _extract_embedded_chat_template(llm)
    if embedded_template is None:
        return None
    from vetinari.models.chat_templates import validate_template

    _template, validation = validate_template(model_id, embedded_template)
    return validation


def _chat_template_validation_metadata(validation: TemplateValidation) -> dict[str, Any]:
    """Return serializable metadata for a chat-template validation result."""
    return {
        "is_trusted": validation.is_trusted,
        "template_hash": validation.template_hash,
        "matched_family": validation.matched_family,
        "fallback_used": validation.fallback_used,
    }
