import { describe, expect, it } from 'vitest';

import { createFetchMock, getCalls, jsonResponse } from './api-mock.js';
import { getAppState, resetAppState } from './render.js';
import { cloneApiFixture, resourceCockpitSnapshot } from '../fixtures/api-shapes.js';

describe('shared Svelte test fixtures', () => {
  it('serves mocked JSON routes and records fetch calls', async () => {
    createFetchMock({
      '/api/workbench/status': jsonResponse({ status: 'ok' }),
    });

    const mockedFetch = globalThis['fetch'];
    const response = await mockedFetch('/api/workbench/status');

    await expect(response.json()).resolves.toEqual({ status: 'ok' });
    expect(getCalls()).toMatchObject([
      {
        method: 'GET',
        url: '/api/workbench/status',
      },
    ]);
  });

  it('resets shared app state for component render helpers', () => {
    resetAppState({ currentView: 'resource-cockpit', sidebarCollapsed: true });

    expect(getAppState()).toMatchObject({
      currentView: 'resource-cockpit',
      currentProjectId: 'test-project',
      sidebarCollapsed: true,
    });
  });

  it('clones frozen API fixtures before scenario-specific mutation', () => {
    const clone = cloneApiFixture(resourceCockpitSnapshot);

    clone.active_leases[0].status.label = 'released';

    expect(clone.active_leases[0].status.label).toBe('released');
    expect(resourceCockpitSnapshot.active_leases[0].status.label).toBe('active');
    expect(Object.isFrozen(resourceCockpitSnapshot.active_leases[0].status)).toBe(true);
  });
});
