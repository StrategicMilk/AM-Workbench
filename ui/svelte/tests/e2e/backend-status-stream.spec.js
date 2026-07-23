import AxeBuilder from '@axe-core/playwright';
import { expect, test } from '@playwright/test';

test('models cockpit renders live engine status and remains accessible', async ({ page }) => {
  await page.addInitScript(() => {
    const invoke = async (_command, { payload }) => {
      if (payload.path === '/api/v1/engine/health') return { status: 'ok' };
      if (payload.path === '/api/v1/engine/metrics') {
        return { metrics: { queue_depth: 1, slots_busy: 1, kv_occupancy_pct: 25, tok_s: 12 } };
      }
      if (payload.path === '/api/v1/engine/version') return { engine_version: '1.0.0' };
      return { models: [], recommendations: [], candidates: [] };
    };
    Object.defineProperty(globalThis, '__TAURI__', {
      value: { core: { invoke } },
      configurable: false,
    });
  });
  await page.route('**/api/**', (route) => route.fulfill({ json: { models: [], recommendations: [], candidates: [] } }));
  await page.route('**/health', (route) => route.fulfill({ json: { status: 'ok' } }));
  await page.route('**/api/v1/engine/health', (route) => route.fulfill({ json: { status: 'ok' } }));
  await page.route('**/api/v1/engine/metrics', (route) => route.fulfill({
    json: { metrics: { queue_depth: 1, slots_busy: 1, kv_occupancy_pct: 25, tok_s: 12 } },
  }));
  await page.route('**/api/v1/engine/version', (route) => route.fulfill({ json: { engine_version: '1.0.0' } }));
  await page.route('**/api/v1/engine/agent-stream', (route) => route.fulfill({
    status: 200,
    contentType: 'text/event-stream',
    body: 'data: {"delta":{"content":"ready"}}\n\n',
  }));
  await page.goto('/#models');

  await expect(page.getByRole('heading', { name: 'AM Engine' })).toBeVisible();
  await expect(page.locator('[data-state]')).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Live generation' })).toBeVisible();
  const results = await new AxeBuilder({ page })
    .include('.backend-status')
    .include('.stream-panel')
    .analyze();
  expect(results.violations).toEqual([]);
});
