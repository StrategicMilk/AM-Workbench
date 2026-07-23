const MIN_BACKOFF = 1000;
const MAX_BACKOFF = 30000;
const BACKOFF_FACTOR = 2;
const STREAM_URL = '/api/v1/engine/agent-stream';
const CANCEL_URL = '/api/v1/engine/agent-stream/cancel';

let eventSource = $state(null);
let connected = $state(false);
let error = $state(null);
let reconnectTimer = null;
let backoff = MIN_BACKOFF;
let handlers = {};
let subscribed = false;

export function subscribe(nextHandlers = {}) {
  unsubscribe();
  subscribed = true;
  handlers = nextHandlers;
  connect();
}

export function unsubscribe() {
  subscribed = false;
  clearReconnect();
  eventSource?.close();
  eventSource = null;
  connected = false;
  error = null;
  handlers = {};
  backoff = MIN_BACKOFF;
}

export async function cancelGeneration() {
  const response = await fetch(CANCEL_URL, {
    method: 'POST',
    headers: { 'X-Requested-With': 'XMLHttpRequest' },
  });
  if (!response.ok) throw new Error(`Engine cancellation failed (${response.status})`);
  eventSource?.close();
  eventSource = null;
  connected = false;
  subscribed = false;
  clearReconnect();
  return response.json();
}

export function streamState() {
  return { connected, error };
}

function connect() {
  clearReconnect();
  const source = new EventSource(STREAM_URL);
  eventSource = source;
  source.onopen = () => {
    if (source !== eventSource) return;
    connected = true;
    error = null;
    backoff = MIN_BACKOFF;
    handlers.open?.();
  };
  source.onmessage = (event) => {
    if (source !== eventSource) return;
    let payload = event.data;
    try { payload = JSON.parse(event.data); } catch { /* raw token */ }
    handlers.message?.(payload);
  };
  source.onerror = () => {
    if (source !== eventSource) return;
    connected = false;
    error = 'Engine stream disconnected';
    handlers.error?.(error);
    source.close();
    eventSource = null;
    if (subscribed) scheduleReconnect();
  };
}

function scheduleReconnect() {
  clearReconnect();
  reconnectTimer = setTimeout(connect, backoff);
  backoff = Math.min(backoff * BACKOFF_FACTOR, MAX_BACKOFF);
}

function clearReconnect() {
  if (reconnectTimer) clearTimeout(reconnectTimer);
  reconnectTimer = null;
}
