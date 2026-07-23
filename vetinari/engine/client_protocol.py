"""Wire-payload normalization and parsing for the AM Engine client."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, cast

from vetinari.engine.client_types import (
    CURRENT_SCHEMA_MAJOR,
    EngineProtocolError,
    EngineSchemaVersionError,
    PrefixRef,
)


def batch_payload(items: Sequence[str], *, model_id: str | None, add_special: bool | None) -> dict[str, Any]:
    """Build a schema-versioned tokenize or count request payload.

    Returns:
        Detached request payload ready for the engine wire protocol.
    """
    payload: dict[str, Any] = {"schema_version": CURRENT_SCHEMA_MAJOR, "items": list(items)}
    if model_id is not None:
        payload["model"] = model_id
    if add_special is not None:
        payload["add_special"] = add_special
    return payload


def normalize_openai_request(request: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize the legacy Python model key to the OpenAI wire name.

    Args:
        request: Caller-supplied request mapping.

    Returns:
        Detached request payload with schema and model fields normalized.

    Raises:
        ValueError: If both model field spellings are supplied.
    """
    payload = dict(request)
    if "model_id" in payload:
        if "model" in payload:
            raise ValueError("chat request cannot contain both model and model_id")
        payload["model"] = payload.pop("model_id")
    payload.setdefault("schema_version", CURRENT_SCHEMA_MAJOR)
    return payload


def require_request_schema(payload: Mapping[str, Any]) -> None:
    """Require the payload to use the engine client's supported schema major.

    Raises:
        EngineSchemaVersionError: If the schema version is malformed or unsupported.
    """
    raw = payload.get("schema_version")
    try:
        major = int(str(raw).split(".", maxsplit=1)[0])
    except (TypeError, ValueError) as exc:
        raise EngineSchemaVersionError("engine request schema_version is malformed", schema_version=raw) from exc
    if major != CURRENT_SCHEMA_MAJOR:
        raise EngineSchemaVersionError(
            "engine request schema major is unsupported",
            expected=CURRENT_SCHEMA_MAJOR,
            received=raw,
        )


def parse_token_results(value: Any, *, expected: int) -> tuple[tuple[int, ...], ...]:
    """Parse ordered token arrays while enforcing response cardinality.

    Returns:
        Immutable token-ID arrays in request order.

    Raises:
        EngineProtocolError: If the response shape, cardinality, or token types are invalid.
    """
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence) or len(value) != expected:
        raise EngineProtocolError("engine tokenize response cardinality does not match request")
    parsed: list[tuple[int, ...]] = []
    for result in value:
        if isinstance(result, (str, bytes)) or not isinstance(result, Sequence):
            raise EngineProtocolError("engine tokenize result must be an array of token ids")
        if any(isinstance(token, bool) or not isinstance(token, int) for token in result):
            raise EngineProtocolError("engine tokenize result contains a non-integer token id")
        parsed.append(tuple(cast(Sequence[int], result)))
    return tuple(parsed)


def parse_token_counts(value: Any, *, expected: int) -> tuple[int, ...]:
    """Parse ordered token counts while enforcing shape and cardinality.

    Returns:
        Immutable non-negative counts in request order.

    Raises:
        EngineProtocolError: If the response shape, cardinality, or count values are invalid.
    """
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence) or len(value) != expected:
        raise EngineProtocolError("engine count response cardinality does not match request")
    if any(isinstance(count, bool) or not isinstance(count, int) or count < 0 for count in value):
        raise EngineProtocolError("engine count response contains an invalid token count")
    return tuple(cast(Sequence[int], value))


def coerce_prefix_ref(value: PrefixRef | Mapping[str, Any]) -> PrefixRef:
    """Coerce the public mapping form of a prefix reference to its typed form.

    Returns:
        Validated typed prefix reference.

    Raises:
        ValueError: If the mapping omits a string prefix name or content hash.
    """
    if isinstance(value, PrefixRef):
        return value
    prefix_name = value.get("prefix_name", value.get("name"))
    content_hash = value.get("content_hash")
    if not isinstance(prefix_name, str) or not isinstance(content_hash, str):
        raise ValueError("prefix refs require prefix_name and content_hash strings")
    return PrefixRef(prefix_name=prefix_name, content_hash=content_hash)


__all__ = [
    "batch_payload",
    "coerce_prefix_ref",
    "normalize_openai_request",
    "parse_token_counts",
    "parse_token_results",
    "require_request_schema",
]
