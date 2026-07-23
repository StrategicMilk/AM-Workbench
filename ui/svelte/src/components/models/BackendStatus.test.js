import { mount, unmount } from '../../../node_modules/svelte/src/index-client.js';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { getEngineHealth, getEngineMetrics, getEngineVersion } from '$lib/api.js';
import BackendStatus from './BackendStatus.svelte';

vi.mock('$lib/api.js', () => ({
  getEngineHealth: vi.fn(),
  getEngineMetrics: vi.fn(),
  getEngineVersion: vi.fn(),
}));

let mounted;
let container;

async function render(props = {}) {
  container = document.createElement('div');
  document.body.appendChild(container);
  mounted = mount(BackendStatus, { target: container, props });
  await vi.waitFor(() => expect(container.querySelector('[data-state]')).not.toBeNull());
  return container;
}

afterEach(async () => {
  if (mounted) await unmount(mounted);
  container?.remove();
  mounted = null;
  container = null;
  vi.clearAllMocks();
});

describe('BackendStatus', () => {
  it('renders running metrics, version, and legacy backend rows', async () => {
    getEngineHealth.mockResolvedValue({ status: 'ok' });
    getEngineMetrics.mockResolvedValue({ metrics: { queue_depth: 2, slots_busy: 1, kv_occupancy_pct: 42, tok_s: 18 } });
    getEngineVersion.mockResolvedValue({ engine_version: '1.2.3' });

    const view = await render({ backends: [{ provider_type: 'am_engine', status: 'ready', cache_durability: 'durable' }] });
    await vi.waitFor(() => expect(view.querySelector('[data-state="RUNNING"]')).not.toBeNull());
    expect(view.textContent).toContain('18 tok/s');
    expect(view.textContent).toContain('42%');
    expect(view.textContent).toContain('v1.2.3');
    expect(view.textContent).toContain('am_engine');
    expect(view.textContent).toContain('durable');
  });

  for (const state of ['STOPPED', 'MISSING', 'VERSION_MISMATCH', 'DEGRADED']) {
    it(`renders ${state} without fabricated metrics`, async () => {
      const error = Object.assign(new Error('unavailable'), { body: { engine_state: state, message: `${state} detail` } });
      getEngineHealth.mockRejectedValue(error);
      getEngineMetrics.mockRejectedValue(error);
      getEngineVersion.mockRejectedValue(error);

      const view = await render();
      await vi.waitFor(() => expect(view.textContent).toContain(`${state} detail`));
      expect(view.querySelector(`[data-state="${state}"]`)).not.toBeNull();
      expect(view.querySelector('.metrics')).toBeNull();
      expect(view.textContent).not.toContain('0 tok/s');
    });
  }
});
