import { expect, test } from '@playwright/test';

const overview = {
  active_tasks: 0,
  models_loaded: 0,
  session_cost: 0,
  avg_latency_ms: 0,
  success_rate: 0,
  memory_entries: 0,
  latency_history: [],
  token_history: [],
  hardware: {},
  recent_events: [],
};

async function installJourneyMocks(page, failDashboard = false) {
  await page.route('**/health', (route) =>
    failDashboard ? route.fulfill({ status: 503, body: 'health unavailable' }) : route.fulfill({ json: { status: 'ok' } })
  );
  await page.route('**/api/**', (route) => {
    const url = new URL(route.request().url());
    if (url.pathname === '/api/projects') {
      return route.fulfill({ json: { projects: [] } });
    }
    if (url.pathname === '/api/v1/analytics/overview') {
      return failDashboard
        ? route.fulfill({ status: 503, body: 'analytics unavailable' })
        : route.fulfill({ json: overview });
    }
    if (url.pathname === '/api/v1/models') {
      return route.fulfill({ json: { models: [] } });
    }
    if (url.pathname === '/api/v1/workbench/migration/plan') {
      return route.fulfill({ json: { plan: { proposal_id: 'empty', conflicts: [], findings: [] } } });
    }
    return route.fulfill({ json: {} });
  });
}

test.describe('RCG-0023 real user journey and recovery', () => {
  test('documented project dashboard URL mounts the live SPA route', async ({ page }) => {
    await installJourneyMocks(page);

    await page.goto('/projects/demo/dashboard');

    await expect(page.getByRole('heading', { name: 'Dashboard' })).toBeVisible();
    expect(page.url()).toContain('/projects/demo/dashboard');
    expect(new URL(page.url()).hash).toBe('');
  });

  test('first dashboard outage shows actionable recovery instead of a blank view', async ({ page }) => {
    await installJourneyMocks(page, true);

    await page.goto('/?view=dashboard');

    await expect(page.locator('.dashboard-error[role="alert"]')).toContainText('All dashboard endpoints unreachable');
    await expect(page.getByRole('button', { name: 'Retry' })).toBeVisible();
  });

  test('migration first-run path presents backup guidance before destructive apply', async ({ page }) => {
    await installJourneyMocks(page);

    await page.goto('/?view=workbench-migration&project_id=demo');

    await expect(page.getByRole('heading', { name: 'Migration Wizard' }).first()).toBeVisible();
    await expect(page.getByLabel(/Backup completed and reviewed/)).toBeVisible();
    await expect(page.getByRole('button', { name: /Apply/ })).toBeDisabled();
  });
});
