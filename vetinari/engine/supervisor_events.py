"""Event-ingest and idle-policy operations for the engine supervisor."""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import TYPE_CHECKING

from vetinari.exceptions import EngineUnavailableError

if TYPE_CHECKING:
    from vetinari.engine.events import EventsClient

logger = logging.getLogger(__name__)


def _supervisor_surface():  # type: ignore[no-untyped-def]
    from vetinari.engine import supervisor

    return supervisor


class EngineEventsMixin:
    """Own event-stream attachment and model idle-policy accounting."""

    def attach_events_client(self, client: EventsClient) -> None:
        """Start the supervisor-owned asynchronous engine-event consumer.

        Raises:
            EngineUnavailableError: If ingestion cannot start or replace a failed consumer.
        """
        from vetinari.engine.events import IngestState

        with self._events_transition_lock:
            with self._lock:
                thread = self._events_thread
                ingest_state = self._event_ingester.state
                if (
                    self._events_client is client
                    and thread is not None
                    and thread.is_alive()
                    and ingest_state is IngestState.RUNNING
                ):
                    return
                if (
                    self._event_ingester_started
                    and ingest_state is IngestState.EVENTS_UNAVAILABLE_SCAFFOLD
                    and (thread is None or not thread.is_alive())
                ):
                    return
                replace_ingester = self._event_ingester_started
                stop_existing = thread is not None
            if stop_existing:
                self._stop_event_ingest()
                if thread is not None and thread.is_alive():
                    raise EngineUnavailableError(
                        "AM Engine event-ingest runtime did not stop before replacement",
                    )
            if replace_ingester:
                replacement_ingester = self._event_ingester_factory()
                with self._lock:
                    self._event_ingester = replacement_ingester
                    self._event_ingester_started = False
            with self._lock:
                self._events_ready.clear()
                self._events_finished.clear()
                self._events_client = client
                thread = threading.Thread(
                    target=self._run_event_ingest,
                    args=(client,),
                    name="am-engine-event-ingest",
                    daemon=True,
                )
                self._events_thread = thread
                self._event_ingester_started = True
                thread.start()
            deadline = self._monotonic() + self.config.request_timeout_seconds
            while not self._events_ready.wait(timeout=0.01):
                if self._events_finished.is_set():
                    raise EngineUnavailableError("AM Engine event-ingest runtime failed during startup")
                if self._monotonic() >= deadline:
                    self._stop_event_ingest()
                    raise EngineUnavailableError("AM Engine event-ingest runtime did not start")
            with self._lock:
                if self._events_thread is not thread or self._events_client is not client or not thread.is_alive():
                    raise EngineUnavailableError("AM Engine event-ingest runtime failed during startup")

    def detach_events_client(self, client: EventsClient) -> None:
        """Detach event ingestion only for the currently attached client generation."""
        with self._events_transition_lock:
            with self._lock:
                if self._events_client is not client:
                    return
            self._stop_event_ingest()

    def _run_event_ingest(self, client: EventsClient) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        with self._lock:
            self._events_loop = loop
        try:
            loop.run_until_complete(self._serve_event_ingest(client))
        except Exception:
            logger.exception("AM Engine event-ingest runtime failed")
        finally:
            current_thread = threading.current_thread()
            with self._lock:
                if self._events_thread is current_thread:
                    self._events_loop = None
                    self._events_thread = None
                    self._events_client = None
            self._events_finished.set()
            loop.close()

    async def _serve_event_ingest(self, client: EventsClient) -> None:
        await self._event_ingester.start(client)
        self._events_ready.set()
        await self._event_ingester.wait()

    def _stop_event_ingest(self) -> None:
        with self._events_transition_lock:
            with self._lock:
                loop = self._events_loop
                thread = self._events_thread
            if loop is not None and loop.is_running():
                future = asyncio.run_coroutine_threadsafe(self._event_ingester.stop(), loop)
                try:
                    future.result(timeout=self.config.drain_timeout_seconds)
                except TimeoutError:
                    logger.warning("AM Engine event ingest did not stop before the drain deadline")
                except Exception:
                    logger.warning("AM Engine event-ingest shutdown failed", exc_info=True)
            if thread is not None and thread is not threading.current_thread():
                thread.join(timeout=self.config.drain_timeout_seconds)
            with self._lock:
                if thread is self._events_thread and (thread is None or not thread.is_alive()):
                    self._events_thread = None
                    self._events_client = None

    def note_activity(self) -> None:
        """Record successful data-plane activity for idle-policy accounting."""
        with self._lock:
            self._last_activity = self._monotonic()

    def set_factory_pin(self, pinned: bool) -> None:
        """Pin or unpin loaded models while a factory run is active."""
        with self._lock:
            self._factory_pin_count = max(0, self._factory_pin_count + (1 if pinned else -1))

    def enforce_idle_policy(self) -> bool:
        """Unload an idle scaffold model without terminating the daemon.

        Returns:
            ``True`` only when an unload request was issued.
        """
        surface = _supervisor_surface()
        with self._lock:
            if self._factory_pin_count:
                return False
            keep_alive_seconds = surface.parse_keep_alive(self.config.keep_alive)
            if keep_alive_seconds is None:
                return False
            if self._monotonic() - self._last_activity < keep_alive_seconds:
                return False
            endpoint = self._endpoint
            runtime_mode = self.config.runtime_mode
        if runtime_mode is surface.EngineRuntimeMode.OWNED:
            return False
        if endpoint is None or not self.capabilities.model_unload:
            return False
        token = self._read_token(endpoint.token_path)
        self._request_json(
            endpoint.url,
            "/models/unload",
            token,
            self.config.request_timeout_seconds,
            "POST",
            None,
        )
        with self._lock:
            self._last_activity = self._monotonic()
        return True

    def reload_config(self) -> None:
        """Expose a gated seam for future binaries that implement live reload.

        Raises:
            EngineUnavailableError: If the pinned binary lacks live reload.
        """
        if not self.capabilities.config_reload:
            raise EngineUnavailableError("the pinned AM Engine does not support config reload")
