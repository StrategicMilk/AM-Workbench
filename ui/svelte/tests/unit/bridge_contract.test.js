import { afterEach, describe, expect, it, vi } from 'vitest';

import { installTauriFetchBridge } from '../../src/lib/tauri_fetch_bridge.js';
import { nativeKernelPathFromUrl } from '../../src/lib/native_kernel_routes.js';

afterEach(() => {
  delete globalThis.__TAURI__;
  delete globalThis.__TAURI_INTERNALS__;
  delete globalThis.__vetinariTauriFetchBridgeInstalled;
  vi.restoreAllMocks();
});

describe('native Tauri fetch bridge contract', () => {
  it('classifies model hub and workbench playground routes as native-kernel owned', () => {
    expect(nativeKernelPathFromUrl('/api/models/hub/search?q=llama')).toBe('/api/models/hub/search?q=llama');
    expect(nativeKernelPathFromUrl('/api/models/hub/pull')).toBe('/api/models/hub/pull');
    expect(nativeKernelPathFromUrl('/api/workbench/playground/run')).toBe('/api/workbench/playground/run');
    expect(nativeKernelPathFromUrl('https://example.invalid/api/workbench/playground/run')).toBeNull();
  });

  it('intercepts same-origin JSON requests and invokes the native kernel instead of browser fetch', async () => {
    const originalFetch = vi.fn().mockResolvedValue(new Response('{}', { status: 200 }));
    const invoke = vi.fn().mockResolvedValue({ ok: true, source: 'tauri' });
    globalThis.fetch = originalFetch;
    globalThis.__TAURI__ = { core: { invoke } };

    installTauriFetchBridge();
    const response = await globalThis.fetch('/api/workbench/playground/run', {
      method: 'POST',
      body: JSON.stringify({ run_id: 'run-1' }),
    });

    expect(originalFetch).not.toHaveBeenCalled();
    expect(invoke).toHaveBeenCalledWith('vetinari_kernel_request', {
      payload: {
        method: 'POST',
        path: '/api/workbench/playground/run',
        body: { run_id: 'run-1' },
      },
    });
    await expect(response.json()).resolves.toEqual({ ok: true, source: 'tauri' });
  });

  it('falls back to browser fetch for non-native or non-JSON requests', async () => {
    const originalFetch = vi.fn().mockResolvedValue(new Response('ok', { status: 200 }));
    const invoke = vi.fn().mockResolvedValue({ ok: true });
    globalThis.fetch = originalFetch;
    globalThis.__TAURI__ = { core: { invoke } };

    installTauriFetchBridge();
    await globalThis.fetch('/assets/logo.png');
    await globalThis.fetch('/api/workbench/playground/run', {
      method: 'POST',
      body: new FormData(),
    });

    expect(originalFetch).toHaveBeenCalledTimes(2);
    expect(invoke).not.toHaveBeenCalled();
  });
});
