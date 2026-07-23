import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  pullModelFromHub,
  searchModelHub,
  workbenchKernelRequest,
} from '../../src/lib/api.js';

function installNativeInvoke(response = {}) {
  const invoke = vi.fn().mockResolvedValue(response);
  globalThis.__TAURI__ = { core: { invoke } };
  globalThis.fetch = vi.fn().mockRejectedValue(new Error('fetch bypassed native bridge'));
  return invoke;
}

afterEach(() => {
  delete globalThis.__TAURI__;
  delete globalThis.__TAURI_INTERNALS__;
  vi.restoreAllMocks();
});

describe('native API domain wrappers', () => {
  it('routes model hub search through the native Tauri kernel bridge when available', async () => {
    const invoke = installNativeInvoke({ results: [{ repo_id: 'llama/example' }] });

    const body = await searchModelHub('llama');

    expect(body.results).toHaveLength(1);
    expect(globalThis.fetch).not.toHaveBeenCalled();
    expect(invoke).toHaveBeenCalledWith('vetinari_kernel_request', {
      payload: {
        method: 'GET',
        path: '/api/models/hub/search?q=llama',
        body: null,
      },
    });
  });

  it('routes model hub pulls through the native Tauri kernel bridge when available', async () => {
    const invoke = installNativeInvoke({ status: 'queued', download_id: 'dl-1' });
    const spec = { repo_id: 'org/model', filename: 'model.gguf' };

    const body = await pullModelFromHub(spec);

    expect(body.download_id).toBe('dl-1');
    expect(globalThis.fetch).not.toHaveBeenCalled();
    expect(invoke).toHaveBeenCalledWith('vetinari_kernel_request', {
      payload: {
        method: 'POST',
        path: '/api/models/hub/pull',
        body: spec,
      },
    });
  });

  it('routes workbench playground actions through the native Tauri kernel bridge', async () => {
    const invoke = installNativeInvoke({ experiment_id: 'exp-1' });

    const body = await workbenchKernelRequest('/api/workbench/playground/run', {
      method: 'POST',
      body: JSON.stringify({ run_id: 'run-1', trace_id: 'trace-1' }),
    });

    expect(body.experiment_id).toBe('exp-1');
    expect(globalThis.fetch).not.toHaveBeenCalled();
    expect(invoke).toHaveBeenCalledWith('vetinari_kernel_request', {
      payload: {
        method: 'POST',
        path: '/api/workbench/playground/run',
        body: { run_id: 'run-1', trace_id: 'trace-1' },
      },
    });
  });
});
