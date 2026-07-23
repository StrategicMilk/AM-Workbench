"""Wire value types and typed failures for the AM Engine client."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from math import isfinite
from typing import Any

from vetinari.exceptions import EngineUnavailableError, InferenceError, VetinariError
from vetinari.types import AgentType

CURRENT_SCHEMA_MAJOR = 1
_EVAL_CONTEXT_SCHEMA_VERSION = 1
_LOWER_HEX_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_CORRELATION_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")


class EngineErrorCode(str, Enum):
    """The complete IS3.4 engine error vocabulary."""

    MODEL_NOT_LOADED = "model_not_loaded"
    MODEL_CORRUPT = "model_corrupt"
    BACKEND_UNAVAILABLE = "backend_unavailable"
    CONTEXT_OVERFLOW = "context_overflow"
    QUEUE_FULL = "queue_full"
    DRAINING = "draining"
    OOM = "oom"
    ALLOCATION_FAILED = "allocation_failed"
    QUOTA_EXHAUSTED = "quota_exhausted"
    GRAMMAR_INVALID = "grammar_invalid"
    TEMPLATE_UNTRUSTED = "template_untrusted"
    VERSION_MISMATCH = "version_mismatch"
    UNAUTHORIZED = "unauthorized"
    SESSION_UNKNOWN = "session_unknown"
    UNSUPPORTED_PARAM = "unsupported_param"
    EVAL_TIMEOUT = "eval_timeout"
    CANCELLED = "cancelled"
    EVAL_RECEIPT_UNAVAILABLE = "eval_receipt_unavailable"
    EVAL_ATTEMPT_CONFLICT = "eval_attempt_conflict"
    EVAL_RECEIPT_ERROR = "eval_receipt_error"
    INTERNAL = "internal"


class EngineResponseError(InferenceError):
    """A typed remote failure preserving the engine envelope losslessly."""

    def __init__(
        self,
        message: str,
        *,
        code: EngineErrorCode,
        retryable: bool,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        self.code = code
        self.retryable = retryable
        self.details = dict(details or {})
        super().__init__(message, code=code.value, retryable=retryable, details=self.details)


class EngineProtocolError(InferenceError):
    """The engine returned a malformed or incompatible wire payload."""


class EngineSchemaVersionError(EngineProtocolError):
    """The engine payload uses an unsupported schema major version."""


class EngineStreamError(InferenceError):
    """An engine SSE response disconnected or could not be decoded."""


class EngineEventLagError(EngineStreamError):
    """The durable event checkpoint fell behind the engine replay window."""


class EngineAmbiguousRequestError(EngineUnavailableError):
    """A state-changing request lost its response and was not replayed."""

    def __init__(self, message: str, *, request_id: str, path: str) -> None:
        self.request_id = request_id
        self.path = path
        super().__init__(message, request_id=request_id, path=path)


class EngineBootstrapError(EngineUnavailableError):
    """The configured model failed during an explicit engine bootstrap."""

    def __init__(self, message: str, *, model_id: str, generation: int) -> None:
        self.model_id = model_id
        self.generation = generation
        super().__init__(message, model_id=model_id, generation=generation)


class PrefixCacheMissError(InferenceError):
    """A registered prefix was requested without an exact cached count."""


class NotSupportedByScaffold(VetinariError):
    """The vendored compatibility scaffold does not implement an operation."""


@dataclass(frozen=True, slots=True)
class PrefixRef:
    """An exact reference into the static-prefix token-count cache."""

    prefix_name: str
    content_hash: str


@dataclass(frozen=True, slots=True)
class EvalContext:
    """Stable evaluation correlation supplied to the Rust engine.

    Args:
        run_id: Evaluation run identifier.
        suite_id: Evaluation suite identifier.
        suite_revision_sha256: Digest of the exact ordered suite revision.
        case_id: Stable case identifier within the suite.
        ordinal: Zero-based case ordinal within the suite.
        case_spec_sha256: Digest of the exact case specification.
        schema_version: Evaluation context wire schema version.
    """

    run_id: str
    suite_id: str
    suite_revision_sha256: str
    case_id: str
    ordinal: int
    case_spec_sha256: str
    schema_version: int = _EVAL_CONTEXT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != _EVAL_CONTEXT_SCHEMA_VERSION:
            raise ValueError("eval context schema_version must be 1")
        for name, value in (
            ("run_id", self.run_id),
            ("suite_id", self.suite_id),
            ("case_id", self.case_id),
        ):
            if not _CORRELATION_ID_RE.fullmatch(value):
                raise ValueError(f"eval context {name} must use the bounded ASCII identifier grammar")
        for name, value in (
            ("suite_revision_sha256", self.suite_revision_sha256),
            ("case_spec_sha256", self.case_spec_sha256),
        ):
            if not _LOWER_HEX_SHA256_RE.fullmatch(value):
                raise ValueError(f"eval context {name} must be a 64-character lowercase SHA-256 digest")
        if isinstance(self.ordinal, bool) or not 0 <= self.ordinal <= 0xFFFF_FFFF:
            raise ValueError("eval context ordinal must be an unsigned 32-bit integer")

    def to_wire(self) -> dict[str, int | str]:
        """Return the strict engine evaluation-context representation.

        Returns:
            A detached wire dictionary containing every schema-v1 field.
        """
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "suite_id": self.suite_id,
            "suite_revision_sha256": self.suite_revision_sha256,
            "case_id": self.case_id,
            "ordinal": self.ordinal,
            "case_spec_sha256": self.case_spec_sha256,
        }

    def __repr__(self) -> str:
        return (
            f"EvalContext(run_id={self.run_id!r}, suite_id={self.suite_id!r}, "
            f"case_id={self.case_id!r}, ordinal={self.ordinal!r})"
        )


@dataclass(frozen=True, slots=True)
class ChatRequest:
    """Schema-versioned request for chat completion."""

    messages: tuple[Mapping[str, Any], ...]
    model_id: str | None = None
    max_tokens: int | None = None
    temperature: float | None = None
    top_k: int | None = None
    top_p: float | None = None
    min_p: float | None = None
    typical_p: float | None = None
    repeat_penalty: float | None = None
    presence_penalty: float | None = None
    frequency_penalty: float | None = None
    logit_bias: Mapping[str, float] | None = None
    seed: int | None = None
    dry_multiplier: float | None = None
    dry_base: float | None = None
    dry_allowed_length: int | None = None
    xtc_probability: float | None = None
    xtc_threshold: float | None = None
    top_n_sigma: float | None = None
    grammar: str | None = None
    priority_class: str | None = None
    role: AgentType | str | None = None
    eval_slot: int | None = None
    eval_context: EvalContext | None = None
    session_id: str | None = None
    prefix_refs: tuple[PrefixRef | Mapping[str, Any], ...] = ()
    schema_version: int = CURRENT_SCHEMA_MAJOR
    extra: Mapping[str, Any] = field(default_factory=dict)

    def to_wire(self) -> dict[str, Any]:
        """Return the versioned OpenAI-compatible engine representation.

        Returns:
            A detached wire dictionary with engine extensions preserved.

        Raises:
            ValueError: If extension data attempts to replace a typed field.
        """
        is_eval = self.priority_class == "eval"
        if is_eval != (self.eval_context is not None):
            raise ValueError("eval priority and eval_context must be supplied together")
        if self.eval_context is not None and (self.seed is None or self.eval_slot is None):
            raise ValueError("eval_context requires an explicit seed and eval_slot")
        payload: dict[str, Any] = {
            "schema_version": self.schema_version,
            "messages": [_chat_message_to_wire(message) for message in self.messages],
        }
        payload.update({
            key: value
            for key, value in (
                ("model", self.model_id),
                ("max_tokens", self.max_tokens),
                ("temperature", self.temperature),
                ("top_k", self.top_k),
                ("top_p", self.top_p),
                ("min_p", self.min_p),
                ("typical_p", self.typical_p),
                ("repeat_penalty", self.repeat_penalty),
                ("presence_penalty", self.presence_penalty),
                ("frequency_penalty", self.frequency_penalty),
                ("logit_bias", dict(self.logit_bias) if self.logit_bias is not None else None),
                ("seed", self.seed),
                ("dry_multiplier", self.dry_multiplier),
                ("dry_base", self.dry_base),
                ("dry_allowed_length", self.dry_allowed_length),
                ("xtc_probability", self.xtc_probability),
                ("xtc_threshold", self.xtc_threshold),
                ("top_n_sigma", self.top_n_sigma),
                ("grammar", self.grammar),
                ("priority_class", self.priority_class),
                ("role", _workload_role_to_wire(self.role)),
                ("eval_slot", self.eval_slot),
                ("eval_context", self.eval_context.to_wire() if self.eval_context is not None else None),
                ("session_id", self.session_id),
            )
            if value is not None
        })
        if self.prefix_refs:
            payload["prefix_refs"] = [_prefix_ref_to_wire(ref) for ref in self.prefix_refs]
        extra = dict(self.extra)
        if "stop_sequences" in extra:
            if "stop" in extra:
                raise ValueError("chat request cannot contain both stop and stop_sequences")
            extra["stop"] = extra.pop("stop_sequences")
        collisions = payload.keys() & extra.keys()
        if collisions:
            raise ValueError(f"chat request extra fields replace typed fields: {sorted(collisions)!r}")
        payload.update(extra)
        return payload

    def __repr__(self) -> str:
        return f"ChatRequest(model_id={self.model_id!r}, messages={len(self.messages)}, schema_version={self.schema_version!r})"


@dataclass(frozen=True, slots=True)
class ChatResponse:
    """Typed chat response retaining the complete engine payload."""

    content: str
    request_id: str | None
    input_tokens: int | None
    output_tokens: int | None
    confidence: float | None
    raw: Mapping[str, Any]
    engine_receipt: Mapping[str, Any] | None = None
    trace_id: str | None = None

    @classmethod
    def from_wire(cls, payload: Mapping[str, Any]) -> ChatResponse:
        """Parse chat content. Returns: A typed response. Raises: EngineProtocolError for missing content."""
        content = payload.get("content")
        if not isinstance(content, str):
            choices = payload.get("choices")
            if isinstance(choices, Sequence) and choices and isinstance(choices[0], Mapping):
                message = choices[0].get("message")
                if isinstance(message, Mapping) and isinstance(message.get("content"), str):
                    content = message["content"]
        if not isinstance(content, str):
            raise EngineProtocolError("engine chat response is missing text content")
        raw_usage = payload.get("usage")
        usage: Mapping[str, Any] = raw_usage if isinstance(raw_usage, Mapping) else {}
        raw_receipt = payload.get("engine_receipt")
        if raw_receipt is not None and not isinstance(raw_receipt, Mapping):
            raise EngineProtocolError("engine chat response engine_receipt must be an object")
        return cls(
            content=content,
            request_id=_optional_str(payload.get("request_id") or payload.get("id")),
            input_tokens=_optional_int(payload.get("input_tokens") or usage.get("prompt_tokens")),
            output_tokens=_optional_int(payload.get("output_tokens") or usage.get("completion_tokens")),
            confidence=_optional_float(payload.get("confidence")),
            raw=dict(payload),
            engine_receipt=dict(raw_receipt) if isinstance(raw_receipt, Mapping) else None,
            trace_id=_optional_str(payload.get("trace_id")),
        )

    def __repr__(self) -> str:
        return (
            f"ChatResponse(request_id={self.request_id!r}, trace_id={self.trace_id!r}, "
            f"input_tokens={self.input_tokens!r}, output_tokens={self.output_tokens!r})"
        )


@dataclass(frozen=True, slots=True)
class ChatStreamEvent:
    """One schema-checked SSE event from a streaming chat response."""

    payload: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class EmbeddingsRequest:
    """Request for one or more embedding vectors."""

    items: tuple[str, ...]
    model_id: str | None = None
    schema_version: int = CURRENT_SCHEMA_MAJOR

    def to_wire(self) -> dict[str, Any]:
        """Return the OpenAI-compatible request. Returns: A detached wire dictionary."""
        payload: dict[str, Any] = {"schema_version": self.schema_version, "input": list(self.items)}
        if self.model_id is not None:
            payload["model"] = self.model_id
        return payload


@dataclass(frozen=True, slots=True)
class EmbeddingsResponse:
    """Typed ordered embedding vectors."""

    vectors: tuple[tuple[float, ...], ...]
    raw: Mapping[str, Any]

    @classmethod
    def from_wire(cls, payload: Mapping[str, Any]) -> EmbeddingsResponse:
        """Parse the strict OpenAI-compatible embedding data array.

        Args:
            payload: Schema-validated response object.

        Returns:
            Ordered finite embedding vectors.

        Raises:
            EngineProtocolError: If data objects, indices, or vectors are malformed.
        """
        if "vectors" in payload:
            raise EngineProtocolError("engine embeddings response exposed legacy vectors outside data")
        if payload.get("object") != "list":
            raise EngineProtocolError("engine embeddings response object must be list")
        data = payload.get("data")
        if isinstance(data, (str, bytes)) or not isinstance(data, Sequence):
            raise EngineProtocolError("engine embeddings response is missing data")
        vectors: list[tuple[float, ...]] = []
        for expected_index, item in enumerate(data):
            if not isinstance(item, Mapping) or item.get("object") != "embedding":
                raise EngineProtocolError("engine embeddings data item has an invalid object type")
            if item.get("index") != expected_index:
                raise EngineProtocolError("engine embeddings data indices are not sequential")
            vector = item.get("embedding")
            if isinstance(vector, (str, bytes)) or not isinstance(vector, Sequence):
                raise EngineProtocolError("engine embedding vector must be an array")
            if not vector:
                raise EngineProtocolError("engine embedding vector must not be empty")
            if any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in vector):
                raise EngineProtocolError("engine embedding vector contains a non-numeric value")
            parsed = tuple(float(value) for value in vector)
            if any(not isfinite(value) for value in parsed):
                raise EngineProtocolError("engine embedding vector contains a non-finite value")
            vectors.append(parsed)
        return cls(vectors=tuple(vectors), raw=dict(payload))


@dataclass(frozen=True, slots=True)
class TokenizeResponse:
    """Ordered token ids for each submitted item."""

    results: tuple[tuple[int, ...], ...]


@dataclass(frozen=True, slots=True)
class CountResponse:
    """Ordered exact token counts for submitted items and prefix refs."""

    counts: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class ProbeResponse:
    """Typed version/readiness probe payload."""

    schema_version: int | str | None
    payload: Mapping[str, Any]


def _optional_str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _optional_int(value: Any) -> int | None:
    return int(value) if not isinstance(value, bool) and isinstance(value, (int, float)) else None


def _optional_float(value: Any) -> float | None:
    return float(value) if not isinstance(value, bool) and isinstance(value, (int, float)) else None


def _prefix_ref_to_wire(value: PrefixRef | Mapping[str, Any]) -> dict[str, str]:
    if isinstance(value, PrefixRef):
        return {"name": value.prefix_name, "content_hash": value.content_hash}
    name = value.get("name", value.get("prefix_name"))
    content_hash = value.get("content_hash")
    if not isinstance(name, str) or not name or not isinstance(content_hash, str) or not content_hash:
        raise ValueError("prefix refs require non-empty name and content_hash strings")
    return {"name": name, "content_hash": content_hash}


def _chat_message_to_wire(value: Mapping[str, Any]) -> dict[str, str]:
    if set(value) != {"role", "content"}:
        raise ValueError("chat messages require exactly role and content")
    role = value.get("role")
    content = value.get("content")
    if role not in {"system", "user", "assistant", "tool"}:
        raise ValueError("chat message role is unsupported")
    if not isinstance(content, str) or not content.strip():
        raise ValueError("chat message content must be a non-empty string")
    return {"role": str(role), "content": content}


def _workload_role_to_wire(value: AgentType | str | None) -> str | None:
    if value is None:
        return None
    raw = value.value.lower() if isinstance(value, AgentType) else value.lower()
    return raw if raw in {"foreman", "worker", "inspector"} else "unknown"
