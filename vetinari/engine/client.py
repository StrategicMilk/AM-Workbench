"""Typed HTTP and SSE transport for the owned AM Engine process."""

from __future__ import annotations

import json
import logging
import secrets
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import stamina

from vetinari.adapters.base import ProviderConfig
from vetinari.engine.client_admin import _EngineAdminMixin
from vetinari.engine.client_protocol import (
    batch_payload,
    coerce_prefix_ref,
    normalize_openai_request,
    parse_token_counts,
    parse_token_results,
    require_request_schema,
)
from vetinari.engine.client_streams import EngineEventsStream, EngineStream
from vetinari.engine.client_types import (
    CURRENT_SCHEMA_MAJOR,
    ChatRequest,
    ChatResponse,
    ChatStreamEvent,
    CountResponse,
    EmbeddingsRequest,
    EmbeddingsResponse,
    EngineAmbiguousRequestError,
    EngineBootstrapError,
    EngineErrorCode,
    EngineEventLagError,
    EngineProtocolError,
    EngineResponseError,
    EngineSchemaVersionError,
    EngineStreamError,
    NotSupportedByScaffold,
    PrefixCacheMissError,
    PrefixRef,
    ProbeResponse,
    TokenizeResponse,
)
from vetinari.exceptions import (
    EngineUnavailableError,
    InferenceError,
    ModelUnavailableError,
    VetinariTimeoutError,
)
from vetinari.observability.otel_genai import _get_otel_trace
from vetinari.types import ModelProvider

logger = logging.getLogger(__name__)

ENGINE_PRINCIPAL_ID = "local-supervisor"

_RETRYABLE_ENGINE_ERROR_CODES = frozenset({
    EngineErrorCode.BACKEND_UNAVAILABLE,
    EngineErrorCode.QUEUE_FULL,
    EngineErrorCode.DRAINING,
    EngineErrorCode.OOM,
    EngineErrorCode.ALLOCATION_FAILED,
    EngineErrorCode.QUOTA_EXHAUSTED,
    EngineErrorCode.EVAL_RECEIPT_UNAVAILABLE,
    EngineErrorCode.INTERNAL,
})


@dataclass(frozen=True, slots=True)
class _CallerIdentity:
    """Caller-authored correlation that remains stable through one request."""

    request_id: str
    trace_id: str
    headers: Mapping[str, str]


class EngineClient(_EngineAdminMixin):
    """The sole shared typed transport to the AM Engine HTTP surface."""

    def __init__(
        self,
        base_url: str,
        auth_token: str,
        config: ProviderConfig,
        *,
        transport: httpx.BaseTransport | None = None,
        stream_transport: httpx.BaseTransport | None = None,
        scaffold_mode: bool = False,
        endpoint_generation: int = 0,
    ) -> None:
        if not auth_token:
            raise ValueError("auth_token must not be empty")
        self._config = config
        self._scaffold_mode = scaffold_mode
        self._endpoint_generation = endpoint_generation
        headers = {"Authorization": f"Bearer {auth_token}", "Accept": "application/json"}
        timeout = httpx.Timeout(float(config.timeout_seconds))
        normalized_url = base_url.rstrip("/") + "/"
        self.__sync_client = httpx.Client(
            base_url=normalized_url,
            headers=headers,
            timeout=timeout,
            transport=transport,
            trust_env=False,
        )
        self.__stream_client = httpx.Client(
            base_url=normalized_url,
            headers=headers,
            timeout=timeout,
            transport=stream_transport or transport,
            trust_env=False,
        )
        self._prefix_counts: dict[tuple[str, str], int] = {}
        self._prefix_lock = threading.RLock()
        self._closed = False

    def __enter__(self) -> EngineClient:
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()

    @property
    def transport_identity(self) -> tuple[int, int]:
        """Return opaque identities for proving lifetime transport reuse."""
        return id(self.__sync_client), id(self.__stream_client)

    @property
    def endpoint_generation(self) -> int:
        """Return the supervisor generation that owns these transports.

        Returns:
            Monotonic endpoint generation published by the supervisor.
        """
        return self._endpoint_generation

    def close(self) -> None:
        """Close both lifetime-scoped transports."""
        if self._closed:
            return
        self._closed = True
        self.__sync_client.close()
        self.__stream_client.close()

    def chat(self, request: ChatRequest | Mapping[str, Any]) -> ChatResponse:
        """Run a synchronous chat completion. Returns: The typed completion response."""
        payload = request.to_wire() if isinstance(request, ChatRequest) else normalize_openai_request(request)
        payload["stream"] = False
        caller_identity = _new_caller_identity()
        response = self._request_json(
            "POST",
            "/v1/chat/completions",
            payload=payload,
            caller_identity=caller_identity,
        )
        _require_caller_identity_echo(response, caller_identity)
        trusted_response = dict(response)
        trusted_response["request_id"] = caller_identity.request_id
        trusted_response["trace_id"] = caller_identity.trace_id
        return ChatResponse.from_wire(trusted_response)

    def chat_stream(self, request: ChatRequest | Mapping[str, Any]) -> EngineStream:
        """Create a streaming request. Returns: A close-aborting context-managed iterator."""
        payload = request.to_wire() if isinstance(request, ChatRequest) else normalize_openai_request(request)
        payload["stream"] = True
        return EngineStream(self, payload, request_id=_new_request_id())

    def events_stream(
        self,
        *,
        generation: str | None = None,
        after_cursor: int | None = None,
    ) -> EngineEventsStream:
        """Create an authenticated resumable engine-event stream.

        Args:
            generation: Process-local event generation from a prior stream.
            after_cursor: Last durably processed sequential cursor.

        Returns:
            Context-managed NDJSON stream with transport cursor metadata.
        """
        return EngineEventsStream(self, generation=generation, after_cursor=after_cursor)

    def embeddings(self, request: EmbeddingsRequest | Mapping[str, Any]) -> EmbeddingsResponse:
        """Create ordered embeddings through the strict OpenAI data envelope.

        Args:
            request: Typed or mapping-based embedding request.

        Returns:
            Ordered typed vectors with request-matching cardinality.

        Raises:
            ValueError: If the request input is not an array.
            EngineProtocolError: If response shape or cardinality is malformed.
        """
        payload = request.to_wire() if isinstance(request, EmbeddingsRequest) else normalize_openai_request(request)
        items = payload.get("input")
        if isinstance(items, (str, bytes)) or not isinstance(items, Sequence):
            raise ValueError("engine embeddings input must be an array")
        response = EmbeddingsResponse.from_wire(self._request_json("POST", "/v1/embeddings", payload=payload))
        if len(response.vectors) != len(items):
            raise EngineProtocolError("engine embeddings response cardinality does not match request")
        return response

    def tokenize_batch(
        self,
        items: Sequence[str],
        *,
        model_id: str | None = None,
        add_special: bool | None = None,
    ) -> TokenizeResponse:
        """Tokenize a batch. Returns: Ordered token ids. Raises: EngineProtocolError on shape drift."""
        clean_items = tuple(str(item) for item in items)
        if self._scaffold_mode:
            results: list[tuple[int, ...]] = []
            for item in clean_items:
                body: dict[str, Any] = {"content": item}
                if add_special is not None:
                    body["add_special"] = add_special
                response = self._request_json("POST", "/tokenize", payload=body)
                tokens = response.get("tokens")
                if not isinstance(tokens, Sequence):
                    raise EngineProtocolError("scaffold tokenize response is missing tokens")
                results.append(tuple(int(token) for token in tokens))
            return TokenizeResponse(results=tuple(results))
        body = batch_payload(clean_items, model_id=model_id, add_special=add_special)
        response = self._request_json("POST", "/v1/tokenize", payload=body)
        raw_results = response.get("results")
        return TokenizeResponse(results=parse_token_results(raw_results, expected=len(clean_items)))

    def count_tokens(
        self,
        items: Sequence[str] = (),
        *,
        model_id: str | None = None,
        add_special: bool | None = None,
        prefix_refs: Sequence[PrefixRef | Mapping[str, Any]] = (),
    ) -> CountResponse:
        """Count tokens. Returns: Cached then dynamic counts. Raises: PrefixCacheMissError for cache drift."""
        cached: list[int] = []
        with self._prefix_lock:
            for raw_ref in prefix_refs:
                ref = coerce_prefix_ref(raw_ref)
                key = (ref.prefix_name, ref.content_hash)
                if key not in self._prefix_counts:
                    raise PrefixCacheMissError(
                        "registered prefix has no exact cached token count",
                        prefix_name=ref.prefix_name,
                        content_hash=ref.content_hash,
                    )
                cached.append(self._prefix_counts[key])
        clean_items = tuple(str(item) for item in items)
        if not clean_items:
            return CountResponse(counts=tuple(cached))
        if self._scaffold_mode:
            tokens = self.tokenize_batch(clean_items, model_id=model_id, add_special=add_special)
            dynamic = tuple(len(result) for result in tokens.results)
        else:
            body = batch_payload(clean_items, model_id=model_id, add_special=add_special)
            response = self._request_json("POST", "/v1/count", payload=body)
            raw_counts = response.get("counts")
            dynamic = parse_token_counts(raw_counts, expected=len(clean_items))
        return CountResponse(counts=tuple(cached) + dynamic)

    def cancel(self, request_id: str) -> Mapping[str, Any]:
        """Cancel a request. Returns: The engine acknowledgement. Raises: NotSupportedByScaffold in scaffold mode."""
        if self._scaffold_mode:
            raise NotSupportedByScaffold("the compatibility scaffold has no per-request cancel route")
        return self._request_json("POST", "/v1/cancel", payload={"request_id": request_id})

    def version(self) -> ProbeResponse:
        """Return the typed version probe. Returns: Schema-checked version data."""
        payload = self._request_json("GET", "/version", idempotent=True)
        return ProbeResponse(schema_version=payload.get("schema_version"), payload=payload)

    def readyz(self) -> ProbeResponse:
        """Return the typed readiness probe. Returns: Schema-checked readiness data."""
        payload = self._request_json("GET", "/readyz", idempotent=True)
        return ProbeResponse(schema_version=payload.get("schema_version"), payload=payload)

    def _admin(
        self,
        method: str,
        path: str,
        payload: Mapping[str, Any] | None = None,
        *,
        params: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        if self._scaffold_mode:
            raise NotSupportedByScaffold(
                "the compatibility scaffold does not implement this admin operation", path=path
            )
        wire_params = dict(params or {})
        if method == "GET":
            wire_params.setdefault("schema_version", CURRENT_SCHEMA_MAJOR)
        return self._request_json(
            method,
            path,
            payload=payload,
            params=wire_params or None,
            idempotent=method == "GET",
        )

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        payload: Mapping[str, Any] | None = None,
        params: Mapping[str, Any] | None = None,
        idempotent: bool = False,
        caller_identity: _CallerIdentity | None = None,
    ) -> Mapping[str, Any]:
        normalized_method = method.upper()
        identity = caller_identity or _new_caller_identity()
        request_id = identity.request_id
        headers = dict(identity.headers)
        safe_method = normalized_method in {"GET", "HEAD", "OPTIONS"}
        transport_retry_safe = idempotent and safe_method

        def should_retry(exc: Exception) -> bool:
            if isinstance(exc, httpx.TransportError):
                return transport_retry_safe
            return bool(getattr(exc, "retryable", False))

        try:
            for attempt in stamina.retry_context(
                on=should_retry,
                attempts=max(1, int(self._config.max_retries) + 1),
                timeout=float(self._config.timeout_seconds),
                wait_initial=0.05,
                wait_max=0.5,
                wait_jitter=0.0,
            ):
                with attempt:
                    response = self.__sync_client.request(
                        normalized_method,
                        path,
                        json=self._wire_payload(payload),
                        params=params,
                        headers=headers,
                    )
                    decoded = self._decode_response(response)
                    self._validate_payload(decoded)
                    self._raise_envelope(decoded)
                    if response.is_error:
                        raise EngineProtocolError(
                            "engine returned an unstructured HTTP error",
                            status_code=response.status_code,
                        )
                    return decoded
        except httpx.TimeoutException as exc:
            if not safe_method:
                raise EngineAmbiguousRequestError(
                    "AM Engine state-changing request timed out; it was not replayed automatically",
                    request_id=request_id,
                    path=path,
                ) from exc
            raise VetinariTimeoutError("AM Engine request timed out", path=path) from exc
        except httpx.TransportError as exc:
            if not safe_method:
                raise EngineAmbiguousRequestError(
                    "AM Engine state-changing request lost its response; it was not replayed automatically",
                    request_id=request_id,
                    path=path,
                ) from exc
            raise EngineUnavailableError("AM Engine transport failed", path=path) from exc
        raise EngineProtocolError("engine request retry loop completed without a response", path=path)

    def _open_stream(self, payload: Mapping[str, Any], *, request_id: str) -> tuple[Any, httpx.Response]:
        context = self.__stream_client.stream(
            "POST",
            "/v1/chat/completions",
            json=self._wire_payload(payload),
            headers={"Accept": "text/event-stream", "x-request-id": request_id, **_trace_headers()},
        )
        try:
            response = context.__enter__()
            if response.is_error:
                decoded = self._decode_response(response)
                self._validate_payload(decoded)
                self._raise_envelope(decoded)
                raise EngineProtocolError(
                    "engine stream returned an unstructured HTTP error", status_code=response.status_code
                )
            return context, response
        except Exception:
            context.__exit__(None, None, None)
            raise

    def _open_events_stream(
        self,
        *,
        generation: str | None,
        after_cursor: int | None,
    ) -> tuple[Any, httpx.Response]:
        if (generation is None) != (after_cursor is None):
            raise ValueError("event resume requires both generation and after_cursor")
        params = None
        if generation is not None and after_cursor is not None:
            params = {"generation": generation, "after_cursor": after_cursor}
        context = self.__stream_client.stream(
            "GET",
            "/events",
            params=params,
            headers={"Accept": "application/x-ndjson", **_trace_headers()},
        )
        try:
            response = context.__enter__()
            if response.is_error:
                if self._scaffold_mode and response.status_code in {404, 405, 501}:
                    raise NotSupportedByScaffold(
                        "the engine does not expose the public event stream",
                        path="/events",
                        status_code=response.status_code,
                    )
                decoded = self._decode_response(response)
                self._validate_payload(decoded)
                self._raise_envelope(decoded)
                raise EngineProtocolError(
                    "engine event stream returned an unstructured HTTP error",
                    status_code=response.status_code,
                )
            return context, response
        except Exception:
            context.__exit__(None, None, None)
            raise

    def _decode_response(self, response: httpx.Response) -> Mapping[str, Any]:
        try:
            decoded = response.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise EngineProtocolError(
                "engine response is not a JSON object",
                status_code=response.status_code,
            ) from exc
        if not isinstance(decoded, Mapping):
            raise EngineProtocolError("engine response must be a JSON object", status_code=response.status_code)
        return decoded

    def _raise_envelope(self, payload: Mapping[str, Any]) -> None:
        envelope = payload.get("error")
        if envelope is None:
            return
        if not isinstance(envelope, Mapping):
            raise EngineProtocolError("engine error envelope must be an object")
        raw_code = envelope.get("code")
        try:
            code = EngineErrorCode(str(raw_code))
        except ValueError as exc:
            raise EngineProtocolError("engine returned an unknown error code", code=raw_code) from exc
        message = str(envelope.get("message") or code.value)
        raw_retryable = envelope.get("retryable")
        if not isinstance(raw_retryable, bool):
            raise EngineProtocolError("engine error retryable field must be boolean", code=code.value)
        retryable = code in _RETRYABLE_ENGINE_ERROR_CODES
        if raw_retryable is not retryable:
            raise EngineProtocolError(
                "engine error retryability disagrees with the owned error vocabulary",
                code=code.value,
                observed=raw_retryable,
                expected=retryable,
            )
        raw_details = envelope.get("details")
        details = dict(raw_details) if isinstance(raw_details, Mapping) else {}
        if code in {EngineErrorCode.MODEL_NOT_LOADED, EngineErrorCode.MODEL_CORRUPT}:
            error: InferenceError = ModelUnavailableError(
                message,
                code=code.value,
                retryable=retryable,
                details=details,
            )
            error.code = code  # type: ignore[attr-defined]
            error.retryable = retryable  # type: ignore[attr-defined]
            error.details = details  # type: ignore[attr-defined]
            raise error
        raise EngineResponseError(message, code=code, retryable=retryable, details=details)

    def _validate_payload(self, payload: Mapping[str, Any]) -> None:
        if "schema_version" not in payload:
            if self._scaffold_mode:
                return
            raise EngineSchemaVersionError(
                "first-party engine response is missing schema_version",
                expected=CURRENT_SCHEMA_MAJOR,
            )
        raw = payload["schema_version"]
        try:
            major = int(str(raw).split(".", maxsplit=1)[0])
        except (TypeError, ValueError) as exc:
            raise EngineSchemaVersionError("engine schema_version is malformed", schema_version=raw) from exc
        if major != CURRENT_SCHEMA_MAJOR:
            raise EngineSchemaVersionError(
                "engine schema major is unsupported",
                expected=CURRENT_SCHEMA_MAJOR,
                received=raw,
            )

    def _wire_payload(self, payload: Mapping[str, Any] | None) -> dict[str, Any] | None:
        if payload is None:
            return None
        wire = dict(payload)
        if not self._scaffold_mode:
            wire.setdefault("schema_version", CURRENT_SCHEMA_MAJOR)
            require_request_schema(wire)
        return wire


_engine_client: EngineClient | None = None
_engine_client_lock = threading.Lock()


def get_engine_client() -> EngineClient:
    """Get the singleton. Returns: The shared client. Raises: EngineUnavailableError when provisioning fails."""
    global _engine_client
    with _engine_client_lock:
        from vetinari.engine import get_supervisor

        supervisor = get_supervisor()
        endpoint = supervisor.ensure_running()
        if _engine_client is not None and _engine_client.endpoint_generation == endpoint.generation:
            supervisor.attach_events_client(_engine_client)
            return _engine_client
        stale = _engine_client
        _engine_client = None
        if stale is not None:
            supervisor.detach_events_client(stale)
            stale.close()
        try:
            token = endpoint.token_path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise EngineUnavailableError(
                "AM Engine authentication token is unavailable",
                path=str(endpoint.token_path),
            ) from exc
        if not token:
            raise EngineUnavailableError("AM Engine authentication token is empty", path=str(endpoint.token_path))
        config = ProviderConfig(
            provider_type=ModelProvider.AM_ENGINE,
            name="am-engine",
            endpoint=endpoint.url,
            max_retries=3,
            timeout_seconds=supervisor.config.request_timeout_seconds,
        )
        from vetinari.engine.supervisor import EngineRuntimeMode

        client = EngineClient(
            endpoint.url,
            token,
            config,
            scaffold_mode=supervisor.config.runtime_mode is EngineRuntimeMode.SCAFFOLD,
            endpoint_generation=endpoint.generation,
        )
        try:
            supervisor.attach_events_client(client)
        except Exception:
            client.close()
            raise
        _engine_client = client
        return client


def receipt_trust_context() -> tuple[Path, str, str]:
    """Return independently pinned receipt-verification inputs for the live owned engine.

    Returns:
        Anchor path, exact anchor digest, and P155 authority SPKI digest.

    Raises:
        EngineUnavailableError: If the owned engine has not completed its trusted identity handshake.
    """
    from vetinari.engine import get_supervisor

    supervisor = get_supervisor()
    supervisor.ensure_running()
    return supervisor.receipt_trust_context()


def receipt_engine_instance_id() -> str:
    """Return the independently verified active engine process identity."""
    from vetinari.engine import get_supervisor

    supervisor = get_supervisor()
    supervisor.ensure_running()
    return supervisor.receipt_engine_instance_id()


def invalidate_engine_client(*, endpoint_generation: int | None = None) -> None:
    """Invalidate the shared transport after an endpoint generation change.

    Args:
        endpoint_generation: Optional generation to invalidate. A newer client is preserved.
    """
    global _engine_client
    with _engine_client_lock:
        client = _engine_client
        if client is None:
            return
        if endpoint_generation is not None and client.endpoint_generation != endpoint_generation:
            return
        _engine_client = None
        client.close()


def _trace_headers() -> dict[str, str]:
    otel_trace = _get_otel_trace()
    if otel_trace is None:
        return {}
    try:
        context = otel_trace.get_current_span().get_span_context()
        if not context.is_valid:
            return {}
        return {
            "traceparent": f"00-{context.trace_id:032x}-{context.span_id:016x}-{'01' if context.trace_flags.sampled else '00'}"
        }
    except (AttributeError, RuntimeError):
        logger.warning("OpenTelemetry trace context unavailable", exc_info=True)
        return {}


def _new_caller_identity() -> _CallerIdentity:
    request_id = _new_request_id()
    trace_headers = _trace_headers()
    traceparent = trace_headers.get("traceparent")
    trace_id = traceparent.split("-", maxsplit=3)[1] if traceparent is not None else secrets.token_hex(16)
    return _CallerIdentity(
        request_id=request_id,
        trace_id=trace_id,
        headers={"x-request-id": request_id, "x-trace-id": trace_id, **trace_headers},
    )


def _require_caller_identity_echo(payload: Mapping[str, Any], identity: _CallerIdentity) -> None:
    for field, expected in (("request_id", identity.request_id), ("trace_id", identity.trace_id)):
        observed = payload.get(field)
        if observed != expected:
            raise EngineProtocolError(
                f"engine chat response {field} does not match the caller-authored identity",
                expected=expected,
                observed=observed,
            )


def _new_request_id() -> str:
    """Return an opaque caller-owned request identity for retries and cancellation."""
    return f"vetinari-{secrets.token_hex(16)}"


__all__ = [
    "CURRENT_SCHEMA_MAJOR",
    "ENGINE_PRINCIPAL_ID",
    "ChatRequest",
    "ChatResponse",
    "ChatStreamEvent",
    "CountResponse",
    "EmbeddingsRequest",
    "EmbeddingsResponse",
    "EngineAmbiguousRequestError",
    "EngineBootstrapError",
    "EngineClient",
    "EngineErrorCode",
    "EngineEventLagError",
    "EngineEventsStream",
    "EngineProtocolError",
    "EngineResponseError",
    "EngineSchemaVersionError",
    "EngineStream",
    "EngineStreamError",
    "NotSupportedByScaffold",
    "PrefixCacheMissError",
    "PrefixRef",
    "ProbeResponse",
    "TokenizeResponse",
    "get_engine_client",
    "receipt_engine_instance_id",
    "receipt_trust_context",
]
