import { nativeKernelPathFromUrl } from './native_kernel_routes.js';
import { requireObject } from './api_contract.js';

const JSON_METHODS = new Set(['GET', 'POST', 'PUT', 'PATCH', 'DELETE']);
const MAX_BRIDGE_BODY_BYTES = 256 * 1024;

function tauriInvoke() {
  return globalThis.__TAURI__?.core?.invoke ?? globalThis.__TAURI_INTERNALS__?.invoke ?? null;
}

function apiPath(input) {
  const raw = typeof input === 'string' ? input : input?.url;
  if (!raw) return null;
  return nativeKernelPathFromUrl(raw);
}

function nativeKernelRejected(path, payload) {
  return path.startsWith('/api/v1/training/')
    || path.startsWith('/api/training/')
    ? payload?.status === 'rejected'
    : false;
}

function requestMethod(init, input) {
  return String(init?.method ?? input?.method ?? 'GET').toUpperCase();
}

function requestBody(init) {
  if (init?.body == null || typeof init.body === 'string') {
    if (init?.body == null) return null;
    if (init.body.length > MAX_BRIDGE_BODY_BYTES) return undefined;
    try {
      return JSON.parse(init.body);
    } catch {
      return undefined;
    }
  }
  return undefined;
}

function jsonResponse(value) {
  return new Response(JSON.stringify(value), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  });
}

function jsonErrorResponse(value, status = 502) {
  return new Response(JSON.stringify(value), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

export function installTauriFetchBridge() {
  const invoke = tauriInvoke();
  if (!invoke || globalThis.__vetinariTauriFetchBridgeInstalled) return;

  const originalFetch = globalThis.fetch.bind(globalThis);
  globalThis.fetch = async (input, init = {}) => {
    const method = requestMethod(init, input);
    const path = apiPath(input);
    const body = requestBody(init);
    if (!path || !JSON_METHODS.has(method) || body === undefined) {
      return originalFetch(input, init);
    }
    try {
      const payload = requireObject(await invoke('vetinari_kernel_request', {
        payload: { method, path, body },
      }), 'native kernel response');
      if (nativeKernelRejected(path, payload)) {
        return jsonErrorResponse(payload, 409);
      }
      return jsonResponse(payload);
    } catch (err) {
      return jsonErrorResponse({
        error: 'native_kernel_route_unavailable',
        detail: err instanceof Error ? err.message : String(err),
        path,
        method,
      });
    }
  };
  globalThis.__vetinariTauriFetchBridgeInstalled = true;
}
