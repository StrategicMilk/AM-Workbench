import { expect, test } from '@playwright/test';

const workflowConsole = {
  saved_graph_count: 0,
  active_graph_id: null,
  runtime_settings: {
    safety_mode: 'simulation_only',
    max_parallel_steps: 2,
  },
};

async function installProductMocks(page) {
  await page.route('**/health', (route) => route.fulfill({ json: { status: 'ok' } }));
  await page.route('**/api/**', (route) => {
    const url = new URL(route.request().url());
    if (url.pathname === '/api/projects') {
      return route.fulfill({ json: { projects: [] } });
    }
    if (url.pathname === '/api/workbench/workflow-builder/metadata') {
      return route.fulfill({ json: { step_kinds: ['prompt', 'approval', 'channel_delivery'] } });
    }
    if (url.pathname.includes('/api/workbench/workflow-builder/console/')) {
      return route.fulfill({ json: workflowConsole });
    }
    if (url.pathname.includes('/api/workbench/workflow-builder/graphs/')) {
      return route.fulfill({ json: { state: 'ready', graphs: [] } });
    }
    return route.fulfill({ json: {} });
  });
}

test.describe('RCG-0022 product design coverage', () => {
  test('mobile navigation exposes open-state wiring on the trigger and sidebar', async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 740 });
    await page.addInitScript(() => localStorage.setItem('sidebarCollapsed', 'true'));
    await installProductMocks(page);
    await page.goto('/?view=dashboard');

    const toggle = page.getByRole('button', { name: 'Toggle sidebar' });
    await expect(toggle).toHaveAttribute('aria-expanded', 'false');
    await expect(toggle).toHaveAttribute('aria-controls', 'main-sidebar');

    await toggle.click();

    await expect(toggle).toHaveAttribute('aria-expanded', 'true');
    await expect(page.locator('#main-sidebar.open')).toBeVisible();
  });

  test('workflow builder first render has no browser runtime exception', async ({ page }) => {
    const pageErrors: string[] = [];
    const consoleErrors: string[] = [];
    page.on('pageerror', (error) => pageErrors.push(error.message));
    page.on('console', (message) => {
      if (message.type() === 'error') consoleErrors.push(message.text());
    });
    await installProductMocks(page);

    await page.goto('/projects/demo/workflow-builder');

    await expect(page.getByRole('heading', { name: 'Workflow Builder' })).toBeVisible();
    await expect(page.getByLabel('Workflow runtime console')).toContainText('simulation_only');
    expect(pageErrors).toEqual([]);
    expect(consoleErrors).toEqual([]);
  });

  test('newly reachable Workbench API views are registered in router and navigation', async ({ page }) => {
    await installProductMocks(page);
    await page.goto('/?view=source-tool-cards&project_id=demo');

    await expect(page.getByRole('heading', { name: /Source And Tool Cards/i })).toBeVisible();

    await page.getByLabel('Global search').focus();
    await page.getByRole('combobox', { name: 'Command palette search' }).fill('Repro Capsules');
    await expect(page.getByRole('option', { name: /Go to Repro Capsules/ })).toBeVisible();
  });
});
