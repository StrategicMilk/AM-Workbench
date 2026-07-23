"""Frozen schema types and validation for AM Engine telemetry events.

The transport ingester deliberately depends on this module's typed boundary;
downstream consumers never inspect the raw event mapping after validation.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from math import isclose, isfinite
from typing import Any, Final, TypeAlias

SCHEMA_VERSION: Final = 1
_COMMON_REQUIRED = frozenset({"ts", "schema_version", "event"})
_COMMON_OPTIONAL = frozenset({"request_id", "trace_id", "model_id"})


class ConsentClass(str, Enum):
    """Typed producer-side data-governance classes."""

    OPERATIONAL_HEALTH = "operational-health"


class EngineEventType(str, Enum):
    """Frozen IS3.3 event names."""

    REQUEST_COMPLETE = "request_complete"
    REQUEST_FAILED = "request_failed"
    MODEL_LOADED = "model_loaded"
    MODEL_UNLOADED = "model_unloaded"
    SLOT_STATE = "slot_state"
    PREFIX_REGISTERED = "prefix_registered"
    PREFIX_HIT = "prefix_hit"
    GAUGES = "gauges"


class IngestState(str, Enum):
    """Lifecycle states exposed to the supervisor-owned caller."""

    STOPPED = "stopped"
    RUNNING = "running"
    EVENTS_UNAVAILABLE_SCAFFOLD = "events_unavailable_scaffold"
    FAILED = "failed"


class EventSchemaError(ValueError):
    """Raised when an event violates the frozen v1 boundary schema."""


class UnsupportedEventSchema(EventSchemaError):
    """Raised when an event stream declares an unknown schema major."""


@dataclass(frozen=True, slots=True)
class RequestCompletePayload:
    """Completed-request telemetry payload."""

    queue_ms: float
    prefill_ms: float
    decode_ms: float
    input_tokens: int
    output_tokens: int
    tok_per_s: float
    prefix_hit_tokens: int
    speculation_proposed_tokens: int
    speculation_accepted_tokens: int
    priority_class: str
    eval_slot: int
    spec_accept_rate: float | None = None

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(input_tokens={self.input_tokens!r}, "
            f"output_tokens={self.output_tokens!r}, eval_slot={self.eval_slot!r})"
        )


@dataclass(frozen=True, slots=True)
class RequestFailedPayload:
    """Failed-request telemetry payload."""

    code: str
    priority_class: str


@dataclass(frozen=True, slots=True)
class ModelLifecyclePayload:
    """Model load/unload telemetry payload."""

    vram_mb: float


@dataclass(frozen=True, slots=True)
class SlotStatePayload:
    """Inference-slot state payload."""

    slot_id: int
    state: str


@dataclass(frozen=True, slots=True)
class PrefixPayload:
    """Prefix registration/hit payload."""

    name: str
    tokens: int


@dataclass(frozen=True, slots=True)
class GaugesPayload:
    """Engine-health gauges; VRAM is absent when the scaffold cannot report it."""

    slots_busy: float
    queue_depth: float
    kv_occupancy_pct: float
    vram_used_mb: float | None = None

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(slots_busy={self.slots_busy!r}, queue_depth={self.queue_depth!r}, "
            f"kv_occupancy_pct={self.kv_occupancy_pct!r}, vram_used_mb={self.vram_used_mb!r})"
        )


EventPayload: TypeAlias = (
    RequestCompletePayload
    | RequestFailedPayload
    | ModelLifecyclePayload
    | SlotStatePayload
    | PrefixPayload
    | GaugesPayload
)


@dataclass(frozen=True, slots=True)
class EngineEvent:
    """Validated event envelope with a typed payload."""

    ts: float
    schema_version: int
    event: EngineEventType
    payload: EventPayload
    request_id: str | None = None
    trace_id: str | None = None
    model_id: str | None = None

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(event={self.event.value!r}, request_id={self.request_id!r}, "
            f"trace_id={self.trace_id!r}, model_id={self.model_id!r})"
        )


def parse_event(raw: Mapping[str, Any]) -> EngineEvent:
    """Validate and convert one exact IS3.3 v1 event object.

    Returns:
        Typed event envelope with no downstream raw-dictionary dependency.

    Raises:
        UnsupportedEventSchema: When the schema major is not supported.
        EventSchemaError: When fields are missing, extra, or ill-typed.
    """
    payload_required, payload_optional = _payload_keys_for(raw.get("event"))
    _require_exact_keys(raw, _COMMON_REQUIRED | payload_required, _COMMON_OPTIONAL | payload_optional)
    version = _required_int(raw, "schema_version")
    if version != SCHEMA_VERSION:
        raise UnsupportedEventSchema(f"unsupported engine event schema major {version!r}")
    try:
        event_type = EngineEventType(_required_str(raw, "event"))
    except ValueError as exc:
        raise EventSchemaError(f"unknown engine event type {raw.get('event')!r}") from exc
    return EngineEvent(
        ts=_required_number(raw, "ts"),
        schema_version=version,
        event=event_type,
        payload=_parse_payload(event_type, raw),
        request_id=_optional_str(raw, "request_id"),
        trace_id=_optional_str(raw, "trace_id"),
        model_id=_optional_str(raw, "model_id"),
    )


def required_metric(metrics: Mapping[str, float], key: str) -> float:
    """Read one required numeric scaffold metric.

    Args:
        metrics: Metrics exported by the scaffold.
        key: Required metric name.

    Returns:
        Numeric value for the requested metric.

    Raises:
        EventSchemaError: When the metric is absent or non-numeric.
    """
    try:
        return float(metrics[key])
    except (KeyError, TypeError, ValueError) as exc:
        raise EventSchemaError(f"required scaffold metric {key!r} is unavailable") from exc


def _payload_keys_for(raw_event: object) -> tuple[frozenset[str], frozenset[str]]:
    try:
        event = EngineEventType(str(raw_event))
    except ValueError as exc:
        raise EventSchemaError(f"unknown engine event type {raw_event!r}") from exc
    if event is EngineEventType.REQUEST_COMPLETE:
        return (
            frozenset({
                "queue_ms",
                "prefill_ms",
                "decode_ms",
                "input_tokens",
                "output_tokens",
                "tok_per_s",
                "prefix_hit_tokens",
                "speculation_proposed_tokens",
                "speculation_accepted_tokens",
                "priority_class",
                "eval_slot",
            }),
            frozenset({"spec_accept_rate"}),
        )
    if event is EngineEventType.REQUEST_FAILED:
        return frozenset({"code", "priority_class"}), frozenset()
    if event in {EngineEventType.MODEL_LOADED, EngineEventType.MODEL_UNLOADED}:
        return frozenset({"vram_mb"}), frozenset()
    if event is EngineEventType.SLOT_STATE:
        return frozenset({"slot_id", "state"}), frozenset()
    if event in {EngineEventType.PREFIX_REGISTERED, EngineEventType.PREFIX_HIT}:
        return frozenset({"name", "tokens"}), frozenset()
    return frozenset({"slots_busy", "queue_depth", "kv_occupancy_pct"}), frozenset({"vram_used_mb"})


def _parse_payload(event: EngineEventType, raw: Mapping[str, Any]) -> EventPayload:
    if event is EngineEventType.REQUEST_COMPLETE:
        proposed, accepted, acceptance_rate = _parse_speculation_metrics(raw)
        return RequestCompletePayload(
            queue_ms=_required_number(raw, "queue_ms"),
            prefill_ms=_required_number(raw, "prefill_ms"),
            decode_ms=_required_number(raw, "decode_ms"),
            input_tokens=_required_int(raw, "input_tokens"),
            output_tokens=_required_int(raw, "output_tokens"),
            tok_per_s=_required_number(raw, "tok_per_s"),
            prefix_hit_tokens=_required_int(raw, "prefix_hit_tokens"),
            speculation_proposed_tokens=proposed,
            speculation_accepted_tokens=accepted,
            spec_accept_rate=acceptance_rate,
            priority_class=_required_str(raw, "priority_class"),
            eval_slot=_required_int(raw, "eval_slot"),
        )
    if event is EngineEventType.REQUEST_FAILED:
        return RequestFailedPayload(
            code=_required_str(raw, "code"), priority_class=_required_str(raw, "priority_class")
        )
    if event in {EngineEventType.MODEL_LOADED, EngineEventType.MODEL_UNLOADED}:
        return ModelLifecyclePayload(vram_mb=_required_number(raw, "vram_mb"))
    if event is EngineEventType.SLOT_STATE:
        return SlotStatePayload(slot_id=_required_int(raw, "slot_id"), state=_required_str(raw, "state"))
    if event in {EngineEventType.PREFIX_REGISTERED, EngineEventType.PREFIX_HIT}:
        return PrefixPayload(name=_required_str(raw, "name"), tokens=_required_int(raw, "tokens"))
    return GaugesPayload(
        slots_busy=_required_number(raw, "slots_busy"),
        queue_depth=_required_number(raw, "queue_depth"),
        kv_occupancy_pct=_required_number(raw, "kv_occupancy_pct"),
        vram_used_mb=_optional_number(raw, "vram_used_mb"),
    )


def _parse_speculation_metrics(raw: Mapping[str, Any]) -> tuple[int, int, float | None]:
    proposed = _required_int(raw, "speculation_proposed_tokens")
    accepted = _required_int(raw, "speculation_accepted_tokens")
    rate = _optional_number(raw, "spec_accept_rate")
    if proposed < 0 or accepted < 0:
        raise EventSchemaError("speculation token counts must be non-negative")
    if accepted > proposed:
        raise EventSchemaError("accepted speculation tokens must not exceed proposed tokens")
    if rate is not None and (not isfinite(rate) or not 0.0 <= rate <= 1.0):
        raise EventSchemaError("speculation acceptance rate must be finite and between zero and one")
    if proposed == 0:
        if rate is not None:
            raise EventSchemaError("speculation acceptance rate requires committed proposals")
    elif rate is None or not isclose(rate, accepted / proposed, rel_tol=0.0, abs_tol=2.220446049250313e-16):
        raise EventSchemaError("speculation acceptance rate must match exact token counts")
    return proposed, accepted, rate


def _require_exact_keys(raw: Mapping[str, Any], required: frozenset[str], allowed_extra: frozenset[str]) -> None:
    missing = required - raw.keys()
    if missing:
        raise EventSchemaError(f"missing required event fields: {sorted(missing)!r}")
    extra = raw.keys() - required - allowed_extra
    if extra:
        raise EventSchemaError(f"unknown event fields: {sorted(extra)!r}")


def _required_str(raw: Mapping[str, Any], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value:
        raise EventSchemaError(f"{key} must be a non-empty string")
    return value


def _optional_str(raw: Mapping[str, Any], key: str) -> str | None:
    value = raw.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise EventSchemaError(f"{key} must be a non-empty string when present")
    return value


def _required_int(raw: Mapping[str, Any], key: str) -> int:
    value = raw.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise EventSchemaError(f"{key} must be an integer")
    return value


def _required_number(raw: Mapping[str, Any], key: str) -> float:
    value = raw.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise EventSchemaError(f"{key} must be numeric")
    return float(value)


def _optional_number(raw: Mapping[str, Any], key: str) -> float | None:
    value = raw.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise EventSchemaError(f"{key} must be numeric when present")
    return float(value)


__all__ = [
    "SCHEMA_VERSION",
    "ConsentClass",
    "EngineEvent",
    "EngineEventType",
    "EventSchemaError",
    "GaugesPayload",
    "IngestState",
    "ModelLifecyclePayload",
    "PrefixPayload",
    "RequestCompletePayload",
    "RequestFailedPayload",
    "SlotStatePayload",
    "UnsupportedEventSchema",
    "parse_event",
    "required_metric",
]
