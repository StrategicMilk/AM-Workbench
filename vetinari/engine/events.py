"""Typed AM Engine telemetry ingestion and sole engine-to-cost writer.

This module is the only production path allowed to persist ``provider="am_engine"``
cost entries (GAP-L). Request latency is the sum of queue, prefill, and decode
milliseconds. Engine-derived entries are serialized in batches under one
process-level writer lock. Scaffold deployments never synthesize events: they
surface a typed unavailable state and may map the vendor metrics that actually
exist, omitting unavailable VRAM rather than reporting a false zero.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections import deque
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Any, Final, Protocol, TypeAlias, cast

from vetinari.adapters.base_telemetry import get_anomaly_detector, get_sla_tracker
from vetinari.agents.observability import _ObservabilitySpan
from vetinari.analytics.cost_models import CostEntry, require_model_pricing
from vetinari.analytics.cost_storage import (
    CostPersistenceConfig,
    build_cost_persistence_config,
    persist_cost_entries,
)
from vetinari.engine.client_types import (
    EngineEventLagError,
    EngineProtocolError,
    EngineResponseError,
    EngineStreamError,
    NotSupportedByScaffold,
)
from vetinari.engine.event_schema import (
    SCHEMA_VERSION,
    ConsentClass,
    EngineEvent,
    EngineEventType,
    EventSchemaError,
    GaugesPayload,
    IngestState,
    RequestCompletePayload,
    RequestFailedPayload,
    UnsupportedEventSchema,
    parse_event,
    required_metric,
)
from vetinari.engine.event_schema import (
    ModelLifecyclePayload as ModelLifecyclePayload,
)
from vetinari.engine.event_schema import (
    PrefixPayload as PrefixPayload,
)
from vetinari.engine.event_schema import (
    SlotStatePayload as SlotStatePayload,
)
from vetinari.engine.event_transport import (
    EventCheckpoint as _Checkpoint,
)
from vetinari.engine.event_transport import (
    StreamItem as _StreamItem,
)
from vetinari.engine.event_transport import (
    is_log_boundary as _is_log_boundary,
)
from vetinari.engine.event_transport import (
    load_checkpoint as _load_checkpoint,
)
from vetinari.engine.event_transport import (
    load_durable_event_ids as _load_durable_event_ids,
)
from vetinari.engine.event_transport import (
    next_stream_item as _next_stream_item,
)
from vetinari.engine.event_transport import (
    transport_identity as _transport_identity,
)
from vetinari.engine.event_transport import (
    write_checkpoint as _write_checkpoint,
)

logger = logging.getLogger(__name__)

DEFAULT_QUEUE_SIZE: Final = 512
DEFAULT_BATCH_SIZE: Final = 64
DEFAULT_RECONNECT_BASE_SECONDS: Final = 0.05
DEFAULT_RECONNECT_MAX_SECONDS: Final = 1.0
_ENGINE_COST_WRITE_LOCK = threading.RLock()


@dataclass(slots=True)
class IngestCounters:
    """Bounded-ingest counters exposed as gauges."""

    parsed: int = 0
    malformed: int = 0
    schema_refused: int = 0
    dropped: int = 0
    duplicates: int = 0
    reconnects: int = 0
    storage_errors: int = 0

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(parsed={self.parsed!r}, malformed={self.malformed!r}, "
            f"schema_refused={self.schema_refused!r}, dropped={self.dropped!r}, "
            f"duplicates={self.duplicates!r}, reconnects={self.reconnects!r}, "
            f"storage_errors={self.storage_errors!r})"
        )


@dataclass(frozen=True, slots=True)
class EmissionRecord:
    """Governance/provenance record for every downstream fan-out."""

    kind: str
    consent_class: ConsentClass
    request_id: str | None
    trace_id: str | None
    model_id: str | None

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(kind={self.kind!r}, consent_class={self.consent_class.value!r}, "
            f"request_id={self.request_id!r}, trace_id={self.trace_id!r})"
        )


class EventsClient(Protocol):
    """Narrow client surface owned by ``vetinari.engine.client``."""

    def events_stream(
        self,
        *,
        generation: str | None = None,
        after_cursor: int | None = None,
    ) -> AbstractContextManager[Any]:
        """Open the authenticated localhost NDJSON event stream."""


SpanFactory: TypeAlias = Callable[[str, dict[str, Any]], AbstractContextManager[Any]]
PersistBatch: TypeAlias = Callable[[list[CostEntry], CostPersistenceConfig], None]


class EventIngester:
    """Async bounded reader/worker for engine operational-health telemetry."""

    def __init__(
        self,
        *,
        queue_size: int = DEFAULT_QUEUE_SIZE,
        batch_size: int = DEFAULT_BATCH_SIZE,
        persistence: CostPersistenceConfig | None = None,
        sla_tracker: Any | None = None,
        anomaly_detector: Any | None = None,
        span_factory: SpanFactory = _ObservabilitySpan,
        persist_batch: PersistBatch | None = None,
        reconnect_base_seconds: float = DEFAULT_RECONNECT_BASE_SECONDS,
        reconnect_max_seconds: float = DEFAULT_RECONNECT_MAX_SECONDS,
    ) -> None:
        if queue_size <= 0 or batch_size <= 0:
            raise ValueError("queue_size and batch_size must be positive")
        if reconnect_base_seconds <= 0 or reconnect_max_seconds < reconnect_base_seconds:
            raise ValueError("event reconnect delays must be positive and ordered")
        self._queue: asyncio.Queue[_StreamItem] = asyncio.Queue(maxsize=queue_size)
        self._batch_size = batch_size
        self._persistence = persistence or build_cost_persistence_config()
        self._sla = sla_tracker
        self._anomaly = anomaly_detector
        self._span_factory = span_factory
        self._persist_batch = persist_batch or self._persist_entries
        self._reconnect_base_seconds = reconnect_base_seconds
        self._reconnect_max_seconds = reconnect_max_seconds
        self._pending: list[CostEntry] = []
        self._pending_event_ids: list[str] = []
        self._task: asyncio.Task[None] | None = None
        self._failure_close_task: asyncio.Task[None] | None = None
        self._active_stream: Any | None = None
        self._stop = asyncio.Event()
        self._schema_logged = False
        self.state = IngestState.STOPPED
        self.counters = IngestCounters()
        self.emissions: deque[EmissionRecord] = deque(maxlen=queue_size)
        self.retained_events: deque[EngineEvent] = deque(maxlen=queue_size)
        self._seen_fingerprints: deque[str] = deque(maxlen=queue_size)
        self._seen_fingerprint_set: set[str] = set()
        # Accepted identities remain rollback-capable until their ledger row or checkpoint is durable.
        self._uncommitted_fingerprints: dict[str, _Checkpoint | None] = {}
        self._checkpoint_path = self._persistence.entries_path.with_name(
            f"{self._persistence.entries_path.name}.engine-events.checkpoint.json"
        )
        self._checkpoint = _load_checkpoint(self._checkpoint_path)
        self._last_processed_checkpoint = self._checkpoint
        self._durable_event_ids = _load_durable_event_ids(self._persistence)

    @staticmethod
    def map_scaffold_metrics(metrics: Mapping[str, float]) -> GaugesPayload:
        """Map the three llama.cpp scaffold metrics without inventing VRAM."""
        return GaugesPayload(
            slots_busy=required_metric(metrics, "llamacpp:requests_processing"),
            queue_depth=required_metric(metrics, "llamacpp:requests_deferred"),
            kv_occupancy_pct=required_metric(metrics, "llamacpp:kv_cache_usage_ratio") * 100.0,
            vram_used_mb=None,
        )

    def submit_nowait(
        self,
        raw: Mapping[str, Any],
        *,
        generation: str | None = None,
        cursor: int | None = None,
    ) -> bool:
        """Queue an event without blocking the engine reader.

        Returns:
            ``True`` when accepted, or ``False`` when bounded capacity drops it.
        """
        detached = dict(raw)
        if self.state is IngestState.FAILED:
            self.counters.dropped += 1
            return False
        fingerprint = _transport_identity(detached, generation=generation, cursor=cursor)
        if fingerprint in self._durable_event_ids:
            self.counters.duplicates += 1
            self._advance_checkpoint(generation, cursor)
            self._commit_checkpoint()
            return False
        if fingerprint in self._uncommitted_fingerprints or fingerprint in self._seen_fingerprint_set:
            self.counters.duplicates += 1
            return False
        try:
            self._queue.put_nowait(_StreamItem(detached, generation, cursor))
        except asyncio.QueueFull:
            self.counters.dropped += 1
            if _is_log_boundary(self.counters.dropped):
                logger.warning("AM Engine event queue saturated; dropped=%d", self.counters.dropped)
            return False
        if len(self._seen_fingerprints) == self._seen_fingerprints.maxlen:
            expired = self._seen_fingerprints.popleft()
            self._seen_fingerprint_set.discard(expired)
        self._seen_fingerprints.append(fingerprint)
        self._seen_fingerprint_set.add(fingerprint)
        accepted_at = _Checkpoint(generation, cursor) if generation is not None and cursor is not None else None
        self._uncommitted_fingerprints[fingerprint] = accepted_at
        return True

    def gauges(self) -> dict[str, float]:
        """Return observable bounded-ingest counters and queue depth."""
        return {
            "am_engine.ingest_queue_depth": float(self._queue.qsize()),
            "am_engine.ingest_dropped_total": float(self.counters.dropped),
            "am_engine.ingest_duplicates_total": float(self.counters.duplicates),
            "am_engine.ingest_reconnects_total": float(self.counters.reconnects),
            "am_engine.ingest_malformed_total": float(self.counters.malformed),
            "am_engine.ingest_schema_refused_total": float(self.counters.schema_refused),
            "am_engine.ingest_storage_errors_total": float(self.counters.storage_errors),
        }

    async def start(self, client: EventsClient) -> None:
        """Start the supervisor-owned reader/worker task.

        Raises:
            RuntimeError: When the ingester already has a live task.
        """
        if self._task is not None and not self._task.done():
            raise RuntimeError("engine event ingester is already running")
        self._stop.clear()
        self.state = IngestState.RUNNING
        self._task = asyncio.create_task(self._run(client), name="am-engine-events")

    async def stop(self) -> None:
        """Stop the reader, drain accepted work, and durably flush its batch."""
        self._stop.set()
        stream = self._active_stream
        if stream is not None:
            await asyncio.to_thread(stream.close)
        task = self._task
        if task is not None:
            await task
        if self.state is IngestState.RUNNING:
            self.state = IngestState.STOPPED

    async def wait(self) -> None:
        """Wait for the active reader/worker task to finish."""
        task = self._task
        if task is not None:
            await task

    async def drain(self) -> None:
        """Process all currently accepted events and flush pending costs."""
        while not self._queue.empty():
            await self._process_one(await self._queue.get())
            self._queue.task_done()
            if self.state is IngestState.FAILED:
                self._discard_queued_after_failure()
                break
        self._flush()

    async def _run(self, client: EventsClient) -> None:
        worker = asyncio.create_task(self._worker(), name="am-engine-events-worker")
        reconnect_delay = self._reconnect_base_seconds
        try:
            while not self._stop.is_set():
                try:
                    checkpoint = self._checkpoint
                    with client.events_stream(
                        generation=checkpoint.generation if checkpoint is not None else None,
                        after_cursor=checkpoint.cursor if checkpoint is not None else None,
                    ) as stream:
                        self._active_stream = stream
                        reconnect_delay = self._reconnect_base_seconds
                        while not self._stop.is_set():
                            item = await asyncio.to_thread(_next_stream_item, stream)
                            if item is None:
                                if not self._stop.is_set():
                                    raise EngineStreamError("engine NDJSON event stream disconnected")
                                break
                            dropped_before = self.counters.dropped
                            self.submit_nowait(
                                item.raw,
                                generation=item.generation,
                                cursor=item.cursor,
                            )
                            if self.counters.dropped != dropped_before:
                                self.state = IngestState.FAILED
                                self._stop.set()
                                break
                except NotSupportedByScaffold:
                    self.state = IngestState.EVENTS_UNAVAILABLE_SCAFFOLD
                    break
                except EngineEventLagError:
                    if not self._stop.is_set():
                        self.state = IngestState.FAILED
                        logger.error(
                            "AM Engine event ingest cursor fell behind retained history; refusing lossy resume",
                            exc_info=True,
                        )
                    break
                except EngineStreamError:
                    if self._stop.is_set():
                        break
                    self.counters.reconnects += 1
                    if _is_log_boundary(self.counters.reconnects):
                        logger.warning(
                            "AM Engine event stream disconnected; reconnects=%d",
                            self.counters.reconnects,
                        )
                    try:
                        await asyncio.wait_for(self._stop.wait(), timeout=reconnect_delay)
                    except TimeoutError:
                        reconnect_delay = min(reconnect_delay * 2, self._reconnect_max_seconds)
                    continue
                except (EngineProtocolError, EngineResponseError, EventSchemaError):
                    if not self._stop.is_set():
                        self.state = IngestState.FAILED
                        logger.warning("AM Engine event ingest failed", exc_info=True)
                    break
        finally:
            self._active_stream = None
            self._stop.set()
            await worker
            if self._failure_close_task is not None:
                await self._failure_close_task
                self._failure_close_task = None

    async def _worker(self) -> None:
        while not self._stop.is_set() or not self._queue.empty():
            item: _StreamItem | None
            try:
                item = await asyncio.wait_for(self._queue.get(), timeout=0.05)
            except TimeoutError:
                item = None
            if item is None:
                continue
            try:
                await self._process_one(item)
            finally:
                self._queue.task_done()
            if self.state is IngestState.FAILED:
                self._discard_queued_after_failure()
                break
        self._flush()

    def _discard_queued_after_failure(self) -> None:
        discarded = 0
        while True:
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            self._queue.task_done()
            self.counters.dropped += 1
            discarded += 1
        if discarded:
            logger.warning(
                "Discarded queued AM Engine events after ingest failure; discarded=%d",
                discarded,
            )

    async def _process_one(self, item: _StreamItem) -> None:
        try:
            event = parse_event(item.raw)
        except UnsupportedEventSchema:
            self.counters.schema_refused += 1
            if not self._schema_logged:
                logger.warning("Refusing AM Engine event stream with unsupported schema major")
                self._schema_logged = True
            self._rollback_uncommitted()
            self.state = IngestState.FAILED
            self._stop.set()
            return
        except EventSchemaError:
            self.counters.malformed += 1
            logger.warning("Rejecting malformed AM Engine event", exc_info=True)
            self._rollback_uncommitted()
            self.state = IngestState.FAILED
            self._stop.set()
            return
        if event.event is EngineEventType.REQUEST_COMPLETE and event.model_id is None:
            self.counters.malformed += 1
            logger.warning("Rejecting request_complete without model_id")
            self._rollback_uncommitted()
            self.state = IngestState.FAILED
            self._stop.set()
            return
        self.counters.parsed += 1
        self.retained_events.append(event)
        if event.event is EngineEventType.REQUEST_COMPLETE:
            self._handle_complete(event, cast(RequestCompletePayload, event.payload), item)
        elif event.event is EngineEventType.REQUEST_FAILED:
            self._handle_failed(event, cast(RequestFailedPayload, event.payload))
        elif event.event is EngineEventType.GAUGES:
            self._handle_gauges(event, cast(GaugesPayload, event.payload))
        else:
            self._record_emission("typed_event", event)
        if self.state is IngestState.FAILED:
            return
        self._advance_checkpoint(item.generation, item.cursor)
        if not self._pending:
            self._commit_checkpoint()
        if item.generation is None or item.cursor is None:
            self._uncommitted_fingerprints.pop(
                _transport_identity(item.raw, generation=item.generation, cursor=item.cursor),
                None,
            )

    def _handle_complete(
        self,
        event: EngineEvent,
        payload: RequestCompletePayload,
        item: _StreamItem,
    ) -> None:
        if event.model_id is None:
            self.counters.malformed += 1
            logger.warning("Rejecting request_complete without model_id")
            return
        latency_ms = payload.queue_ms + payload.prefill_ms + payload.decode_ms
        pricing = require_model_pricing("am_engine:*")
        entry = CostEntry(
            provider="am_engine",
            model=event.model_id,
            input_tokens=payload.input_tokens,
            output_tokens=payload.output_tokens,
            trace_id=event.trace_id,
            timestamp=event.ts,
            cost_usd=pricing.compute(payload.input_tokens, payload.output_tokens),
            latency_ms=latency_ms,
            task_id=_transport_identity(item.raw, generation=item.generation, cursor=item.cursor),
        )
        with self._span(event):
            self._sla_tracker().record_latency("am_engine.request", latency_ms, success=True)
            self._pending.append(entry)
            self._pending_event_ids.append(cast(str, entry.task_id))
            self._record_emission("cost", event)
            self._record_emission("sla", event)
            if len(self._pending) >= self._batch_size:
                self._flush()

    def _handle_failed(self, event: EngineEvent, payload: RequestFailedPayload) -> None:
        del payload
        with self._span(event):
            tracker = self._sla_tracker()
            tracker.record_latency("am_engine.request", 0.0, success=False)
            tracker.record_request(success=False)
            self._record_emission("sla", event)

    def _handle_gauges(self, event: EngineEvent, payload: GaugesPayload) -> None:
        detector = self._anomaly_detector()
        values = {
            "slots_busy": payload.slots_busy,
            "queue_depth": payload.queue_depth,
            "kv_occupancy_pct": payload.kv_occupancy_pct,
        }
        if payload.vram_used_mb is not None:
            values["vram_used_mb"] = payload.vram_used_mb
        for field_name, value in values.items():
            detector.detect(f"am_engine.{field_name}", value)
            self._record_emission("anomaly", event)

    def _span(self, event: EngineEvent) -> AbstractContextManager[Any]:
        metadata = {
            "request_id": event.request_id or "",
            "trace_id": event.trace_id or "",
            "model_id": event.model_id or "",
            "consent_class": ConsentClass.OPERATIONAL_HEALTH.value,
        }
        return self._span_factory("am_engine.ingest", metadata)

    def _record_emission(self, kind: str, event: EngineEvent) -> None:
        self.emissions.append(
            EmissionRecord(
                kind=kind,
                consent_class=ConsentClass.OPERATIONAL_HEALTH,
                request_id=event.request_id,
                trace_id=event.trace_id,
                model_id=event.model_id,
            )
        )

    def _flush(self) -> None:
        if not self._pending:
            return
        entries = self._pending
        try:
            with _ENGINE_COST_WRITE_LOCK:
                self._persist_batch(entries, self._persistence)
        except Exception:
            self.counters.storage_errors += 1
            self.counters.dropped += len(entries)
            self._pending = []
            self._pending_event_ids = []
            self._rollback_uncommitted()
            self.state = IngestState.FAILED
            self._stop_reader_after_failure()
            logger.exception("Failed to persist AM Engine cost batch")
            return
        self._pending = []
        self._durable_event_ids.update(self._pending_event_ids)
        for fingerprint in self._pending_event_ids:
            self._uncommitted_fingerprints.pop(fingerprint, None)
        self._pending_event_ids = []
        self._commit_checkpoint()

    def _stop_reader_after_failure(self) -> None:
        self._stop.set()
        stream = self._active_stream
        if stream is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.warning("Closing failed AM Engine event stream synchronously; no event loop is active")
            stream.close()
            return
        self._failure_close_task = loop.create_task(
            asyncio.to_thread(stream.close),
            name="am-engine-events-failure-close",
        )

    def _advance_checkpoint(self, generation: str | None, cursor: int | None) -> None:
        if generation is None or cursor is None:
            return
        current = self._last_processed_checkpoint
        if current is not None and current.generation == generation and cursor < current.cursor:
            self.state = IngestState.FAILED
            self._stop.set()
            raise EventSchemaError("engine event cursor moved backwards")
        self._last_processed_checkpoint = _Checkpoint(generation, cursor)

    def _commit_checkpoint(self) -> None:
        checkpoint = self._last_processed_checkpoint
        if checkpoint is None:
            return
        if checkpoint != self._checkpoint:
            _write_checkpoint(self._checkpoint_path, checkpoint)
            self._checkpoint = checkpoint
        for fingerprint, accepted_at in tuple(self._uncommitted_fingerprints.items()):
            if (
                accepted_at is not None
                and accepted_at.generation == checkpoint.generation
                and accepted_at.cursor <= checkpoint.cursor
            ):
                self._uncommitted_fingerprints.pop(fingerprint, None)

    def _rollback_uncommitted(self) -> None:
        """Restore accepted identities and cursor state to the last durable checkpoint."""
        rolled_back = set(self._uncommitted_fingerprints)
        if rolled_back:
            maxlen = self._seen_fingerprints.maxlen
            self._seen_fingerprints = deque(
                (fingerprint for fingerprint in self._seen_fingerprints if fingerprint not in rolled_back),
                maxlen=maxlen,
            )
            self._seen_fingerprint_set.difference_update(rolled_back)
            self._uncommitted_fingerprints.clear()
        self._last_processed_checkpoint = self._checkpoint

    @staticmethod
    def _persist_entries(entries: list[CostEntry], config: CostPersistenceConfig) -> None:
        persist_cost_entries(entries, config, consent_class=ConsentClass.OPERATIONAL_HEALTH.value)

    def _sla_tracker(self) -> Any:
        if self._sla is None:
            self._sla = get_sla_tracker()
        return self._sla

    def _anomaly_detector(self) -> Any:
        if self._anomaly is None:
            self._anomaly = get_anomaly_detector()
        return self._anomaly


__all__ = [
    "SCHEMA_VERSION",
    "ConsentClass",
    "EngineEvent",
    "EngineEventType",
    "EventIngester",
    "EventSchemaError",
    "GaugesPayload",
    "IngestCounters",
    "IngestState",
    "UnsupportedEventSchema",
    "parse_event",
]
