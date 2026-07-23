"""OpenAI-compatible server-side route handlers.

Exposes a small subset of the OpenAI HTTP API (``/v1/chat/completions`` and
``/v1/completions``) so external tools (IDE plugins, OpenAI-shaped clients)
can drive a Vetinari-managed adapter pool without learning Vetinari's native
API.  Handlers translate between OpenAI's request/response envelopes and the
internal :py:class:`~vetinari.adapters.base.InferenceRequest` /
:py:class:`~vetinari.adapters.base.InferenceResponse` types via a manager
factory the caller supplies.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import Callable, Mapping
from typing import Any, cast

from litestar import Request, post
from litestar.connection import ASGIConnection
from litestar.response import Response

from vetinari.adapters.base import InferenceRequest, InferenceResponse
from vetinari.adapters.openai_server_adapter_inference import build_fim_prompt
from vetinari.constants import INFERENCE_STATUS_OK
from vetinari.security.fail_closed import assert_closed_schema, sanitize_untrusted_text
from vetinari.security.request_guards import RequestConnection, local_user_guard

logger = logging.getLogger(__name__)

ManagerFactory = Callable[[], Any]


def _local_user_guard(connection: ASGIConnection, handler: Any) -> None:
    """Litestar-typed guard delegating to the framework-neutral ``local_user_guard``.

    ``local_user_guard`` is typed over the structural ``RequestConnection`` protocol
    (``scope``/``headers``/``client``), which a Litestar ``ASGIConnection`` satisfies at
    runtime. The cast bridges the static invariance gap between ``ASGIConnection.scope``
    (``Scope``) and the protocol's ``scope: dict`` without loosening the security
    predicate or the protocol itself.
    """
    local_user_guard(cast("RequestConnection", connection), handler)


def _extract_chat_prompt(messages: list[Mapping[str, Any]]) -> tuple[str, str | None]:
    """Flatten an OpenAI ``messages`` array into prompt + optional system prompt.

    Args:
        messages: List of ``{"role": str, "content": str}`` entries.

    Returns:
        ``(user_prompt, system_prompt_or_None)`` where the user prompt is the
        last user message and the system prompt is the first system message.
    """
    system_prompt: str | None = None
    user_chunks: list[str] = []
    for entry in messages:
        if not isinstance(entry, Mapping):
            continue
        assert_closed_schema(entry, allowed_keys={"role", "content", "name"}, required_keys={"role", "content"})
        role = entry.get("role")
        content = entry.get("content")
        if not isinstance(content, str):
            continue
        content = sanitize_untrusted_text(content, max_length=20_000)
        if role == "system" and system_prompt is None:
            system_prompt = content
        elif role in ("user", "assistant"):
            user_chunks.append(content)
    return ("\n".join(user_chunks), system_prompt)


def _coerce_non_negative_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        integer = int(value)
    except (TypeError, ValueError):
        logger.warning(
            "Ignoring non-numeric token metadata value of type %s",
            type(value).__name__,
        )
        return None
    return integer if integer >= 0 else None


def _estimated_prompt_tokens(*parts: str | None) -> int:
    text = "\n".join(part for part in parts if part)
    return len(text.split()) if text else 0


def _usage_payload(response: InferenceResponse, *, prompt_tokens: int) -> dict[str, int]:
    metadata = response.metadata or {}
    reported_prompt_tokens = _coerce_non_negative_int(metadata.get("prompt_tokens"))
    reported_completion_tokens = _coerce_non_negative_int(metadata.get("completion_tokens"))
    completion_tokens = reported_completion_tokens if reported_completion_tokens is not None else response.tokens_used
    prompt_token_count = reported_prompt_tokens if reported_prompt_tokens is not None else prompt_tokens
    return {
        "prompt_tokens": prompt_token_count,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_token_count + completion_tokens,
    }


def _response_to_chat_payload(
    model_id: str,
    response: InferenceResponse,
    *,
    prompt: str = "",
    system_prompt: str | None = None,
) -> dict[str, Any]:
    usage = _usage_payload(response, prompt_tokens=_estimated_prompt_tokens(system_prompt, prompt))
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model_id,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": response.output},
                "finish_reason": "stop" if response.status == INFERENCE_STATUS_OK else "error",
            }
        ],
        "usage": usage,
    }


def _response_to_text_payload(model_id: str, response: InferenceResponse, *, prompt: str = "") -> dict[str, Any]:
    usage = _usage_payload(response, prompt_tokens=_estimated_prompt_tokens(prompt))
    return {
        "id": f"cmpl-{uuid.uuid4().hex[:24]}",
        "object": "text_completion",
        "created": int(time.time()),
        "model": model_id,
        "choices": [
            {
                "index": 0,
                "text": response.output,
                "finish_reason": "stop" if response.status == INFERENCE_STATUS_OK else "error",
            }
        ],
        "usage": usage,
    }


def _sse_chunks_from_response(model_id: str, response: InferenceResponse) -> bytes:
    """Render a non-streaming inference response as one SSE event plus ``[DONE]``."""
    completion_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    delta_payload = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model_id,
        "choices": [
            {
                "index": 0,
                "delta": {"role": "assistant", "content": response.output},
                "finish_reason": None,
            }
        ],
    }
    done_payload = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model_id,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    }
    return (
        f"data: {json.dumps(delta_payload, separators=(',', ':'))}\n\n".encode()
        + f"data: {json.dumps(done_payload, separators=(',', ':'))}\n\n".encode()
        + b"data: [DONE]\n\n"
    )


def _chat_completion_payload(data: dict[str, Any], manager_factory: ManagerFactory) -> Any:
    assert_closed_schema(
        data,
        allowed_keys={
            "model",
            "messages",
            "max_tokens",
            "temperature",
            "stream",
            "response_format",
            "stop",
            "top_p",
            "frequency_penalty",
            "presence_penalty",
            "n",
            "user",
            "logit_bias",
        },
        required_keys={"messages"},
    )
    model_id = sanitize_untrusted_text(str(data.get("model") or "default"), max_length=200)
    max_tokens = int(data.get("max_tokens") or 2048)
    temperature = float(data.get("temperature") or 0.7)
    stream = bool(data.get("stream") or False)
    prompt, system_prompt = _extract_chat_prompt(data.get("messages") or [])
    infer_request = InferenceRequest(
        model_id=model_id,
        prompt=prompt,
        system_prompt=system_prompt,
        max_tokens=max_tokens,
        temperature=temperature,
    )
    response = manager_factory().infer(infer_request)
    if stream:
        return Response(
            content=_sse_chunks_from_response(model_id, response),
            media_type="text/event-stream",
            status_code=200,
            headers={"Cache-Control": "no-cache"},
        )
    return _response_to_chat_payload(model_id, response, prompt=prompt, system_prompt=system_prompt)


def _completion_payload(data: dict[str, Any], manager_factory: ManagerFactory) -> dict[str, Any]:
    assert_closed_schema(
        data,
        allowed_keys={"model", "prompt", "suffix", "max_tokens", "temperature", "stream"},
    )
    model_id = sanitize_untrusted_text(str(data.get("model") or "default"), max_length=200)
    max_tokens = int(data.get("max_tokens") or 2048)
    temperature = float(data.get("temperature") or 0.7)
    prompt = sanitize_untrusted_text(str(data.get("prompt") or ""), max_length=20_000)
    suffix = data.get("suffix")
    if isinstance(suffix, str) and suffix:
        suffix = sanitize_untrusted_text(suffix, max_length=20_000)
        prompt = build_fim_prompt(prompt, suffix)
    infer_request = InferenceRequest(
        model_id=model_id,
        prompt=prompt,
        max_tokens=max_tokens,
        temperature=temperature,
    )
    return _response_to_text_payload(model_id, manager_factory().infer(infer_request), prompt=prompt)


def create_openai_compat_handlers(manager_factory: ManagerFactory) -> list[Any]:
    """Build the OpenAI-compatible chat/completions Litestar route handlers.

    Args:
        manager_factory: Zero-arg callable that returns an inference manager
            with ``infer(request, provider_name=None) -> InferenceResponse``.
            The factory is invoked per request so test fakes can be swapped
            without reconstructing the app.

    Returns:
        Litestar route-handler list suitable for ``Litestar(route_handlers=...)``.
    """

    @post(
        "/v1/chat/completions",
        sync_to_thread=False,
        status_code=200,
        media_type="application/json",
        guards=[_local_user_guard],
    )
    def chat_completions(request: Request[Any, Any, Any], data: dict[str, Any]) -> Any:
        """Translate a chat completion request and return either JSON or SSE."""
        del request
        return _chat_completion_payload(data, manager_factory)

    @post("/v1/completions", sync_to_thread=False, status_code=200, guards=[_local_user_guard])
    def completions(request: Request[Any, Any, Any], data: dict[str, Any]) -> Any:
        """Translate a plain completion request, supporting OpenAI suffix-as-FIM."""
        del request
        return _completion_payload(data, manager_factory)

    return [chat_completions, completions]


__all__ = ["ManagerFactory", "build_fim_prompt", "create_openai_compat_handlers"]
