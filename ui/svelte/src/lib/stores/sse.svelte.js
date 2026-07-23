/**
 * SSE (Server-Sent Events) connection store with auto-reconnect.
 *
 * Manages a resilient EventSource connection per project stream.
 * Uses exponential backoff on disconnect, cleans up on unsubscribe.
 */

import { appState } from './app.svelte.js';
import { nativeProjectStreamPath } from '../native_kernel_routes.js';

/** Minimum backoff between reconnect attempts (ms). */
const MIN_BACKOFF = 1000;
/** Maximum backoff between reconnect attempts (ms). */
const MAX_BACKOFF = 30000;
/** Backoff multiplier on each consecutive failure. */
const BACKOFF_FACTOR = 2;

let _eventSource = $state(null);
let _connected = $state(false);
let _reconnectTimer = null;
let _backoff = MIN_BACKOFF;

/** Active event handlers keyed by event type. */
let _handlers = {};

function tauriInvokeAvailable() {
  return Boolean(globalThis.__TAURI__?.core?.invoke ?? globalThis.__TAURI_INTERNALS__?.invoke);
}

function projectStreamUrl(projectId) {
  return nativeProjectStreamPath(projectId);
}

/**
 * Subscribe to a project's SSE stream.
 *
 * @param {string} projectId - The project to stream events for.
 * @param {Record<string, (data: any) => void>} handlers - Map of event type to handler function.
 */
export function subscribe(projectId, handlers) {
  unsubscribe(); // Clean up any existing connection
  _handlers = handlers;
  _connect(projectId);
}

/** Disconnect and clean up the current SSE connection. */
export function unsubscribe() {
  _clearReconnect();
  if (_eventSource) {
    _eventSource.close();
    _eventSource = null;
  }
  _connected = false;
  // Keep appState in sync with connection reality so the shell pill is accurate
  // after an explicit teardown (e.g. navigating away from a project stream).
  appState.serverConnected = false;
  _backoff = MIN_BACKOFF;
  _handlers = {};
}

/** Whether the SSE connection is currently open. */
export function isConnected() {
  return _connected;
}

function _connect(projectId) {
  _clearReconnect();

  const url = projectStreamUrl(projectId);
  if (!url) {
    _connected = false;
    appState.serverConnected = false;
    return;
  }
  const es = new EventSource(url);

  es.onopen = () => {
    // Guard: if this EventSource has been superseded, ignore its events
    if (es !== _eventSource) return;
    _connected = true;
    _backoff = MIN_BACKOFF;
    appState.serverConnected = true;
  };

  es.onerror = () => {
    // Guard: if this EventSource has been superseded, ignore its events
    if (es !== _eventSource) return;
    _connected = false;
    // Mark the app as disconnected on ANY error, including dead streams where
    // readyState === 2 (CLOSED). The shell must not show "Connected" while
    // reconnection is in flight.
    appState.serverConnected = false;
    es.close();
    _eventSource = null;
    _scheduleReconnect(projectId);
  };

  // Wire up custom event handlers
  es.onmessage = (event) => {
    // Guard: if this EventSource has been superseded, ignore its events
    if (es !== _eventSource) return;
    _dispatch('message', event);
  };

  // Named event types from the backend
  for (const type of Object.keys(_handlers)) {
    if (type === 'message') continue; // Already handled above
    es.addEventListener(type, (event) => {
      // Guard: if this EventSource has been superseded, ignore its events
      if (es !== _eventSource) return;
      _dispatch(type, event);
    });
  }

  _eventSource = es;
}

function _dispatch(type, event) {
  const handler = _handlers[type];
  if (!handler) return;
  try {
    const data = JSON.parse(event.data);
    handler(data);
  } catch {
    // Non-JSON payload — pass raw data
    handler(event.data);
  }
}

function _scheduleReconnect(projectId) {
  _clearReconnect();
  _reconnectTimer = setTimeout(() => {
    _connect(projectId);
  }, _backoff);
  _backoff = Math.min(_backoff * BACKOFF_FACTOR, MAX_BACKOFF);
}

function _clearReconnect() {
  if (_reconnectTimer) {
    clearTimeout(_reconnectTimer);
    _reconnectTimer = null;
  }
}
