import { expect, test } from '@playwright/test';

const analyticsOverview = {
  active_tasks: 2,
  models_loaded: 1,
  session_cost: 0.42,
  avg_latency_ms: 140,
  success_rate: 0.98,
  memory_entries: 6,
  latency_history: [
    { label: '09:00', value: 120 },
    { label: '09:05', value: 160 },
  ],
  token_history: [
    { label: '09:00', value: 1200 },
    { label: '09:05', value: 1800 },
  ],
  hardware: {},
  recent_events: [],
};

const domainKits = {
  kits: [
    {
      kit_id: 'repo-maintenance',
      title: 'Repository Maintenance',
      domain: 'repo_maintenance',
      supported_workflows: ['triage', 'repair'],
      supported_claim_kinds: ['implementation'],
      unsupported_claims: ['legal_advice'],
      capability_pack_ids: ['pack-core'],
      source_kinds: ['git'],
      tool_kinds: ['shell'],
      benchmark_provider_ids: ['pytest'],
      eval_fixtures: ['repo-smoke'],
      sample_notebook_refs: ['notebook:repo'],
      refusal_boundaries: ['No destructive operations without approval.'],
      required_caveat_acknowledgements: ['review-risk'],
      rate_limit_policy: { requests_per_minute: 10, burst: 2 },
    },
  ],
};

async function installBaseMocks(page) {
  await page.route('**/health', (route) => route.fulfill({ json: { status: 'ok' } }));
  await page.route('**/api/**', (route) => {
    const url = new URL(route.request().url());
    if (url.pathname === '/api/v1/analytics/overview') {
      return route.fulfill({ json: analyticsOverview });
    }
    if (url.pathname === '/api/projects') {
      return route.fulfill({ json: { projects: [] } });
    }
    if (url.pathname === '/api/workbench/domain-kits') {
      return route.fulfill({ json: domainKits });
    }
    if (url.pathname.includes('/api/workbench/domain-kits/') && url.pathname.endsWith('/evaluate')) {
      return route.fulfill({ json: { verdict: { status: 'supported', supported: true, reasons: ['kit supports workflow'] } } });
    }
    return route.fulfill({ json: {} });
  });
}

test.describe('RCG-0001 keyboard and semantic accessibility', () => {
  test('command palette implements dialog, combobox, listbox, activedescendant, and Escape focus return', async ({ page }) => {
    await installBaseMocks(page);
    await page.goto('/?view=dashboard');

    const trigger = page.getByLabel('Global search');
    await trigger.focus();

    const dialog = page.locator('.command-palette[role="dialog"]');
    await expect(dialog).toBeVisible();
    await expect(dialog).toHaveAttribute('aria-modal', 'true');

    const combobox = dialog.getByRole('combobox', { name: 'Command palette search' });
    await expect(combobox).toHaveAttribute('aria-expanded', 'true');
    await expect(combobox).toHaveAttribute('aria-haspopup', 'listbox');
    await expect(combobox).toHaveAttribute('aria-controls', 'command-palette-results');
    await expect(dialog.locator('#command-palette-results[role="listbox"]')).toBeVisible();

    await page.keyboard.press('ArrowDown');
    const activeId = await combobox.getAttribute('aria-activedescendant');
    expect(activeId).toBeTruthy();
    await expect(page.locator(`[id="${activeId}"]`)).toHaveAttribute('aria-selected', 'true');

    await page.keyboard.press('Escape');
    await expect(dialog).toHaveCount(0);
    await expect(trigger).toBeFocused();
  });

  test('dashboard charts expose accessible chart summaries and data tables', async ({ page }) => {
    await installBaseMocks(page);
    await page.goto('/?view=dashboard');

    const latencyChart = page.locator('.chart-container[role="img"]').filter({ hasText: '' }).first();
    await expect(latencyChart).toHaveAttribute('aria-label', /Latency Trend: Latency \(ms\) 120, 160/);
    const tableId = await latencyChart.getAttribute('aria-describedby');
    expect(tableId).toBeTruthy();
    await expect(page.locator(`#${tableId}`)).toContainText('Latency (ms)');
  });

  test('domain kit selection is reachable through a keyboard-operable button', async ({ page }) => {
    await installBaseMocks(page);
    await page.goto('/?view=domain-kits');

    const kitButton = page.getByRole('button', { name: /Repository Maintenance/ });
    await expect(kitButton).toBeVisible();
    await kitButton.focus();
    await page.keyboard.press('Enter');

    await expect(page.getByLabel('Support evaluation')).toContainText('Repository Maintenance');
    await expect(page.getByRole('button', { name: 'Evaluate' })).toBeEnabled();
  });
});
