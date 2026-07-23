import { expect, test } from '@playwright/test';

async function installCommonMocks(page) {
  await page.route('**/health', (route) => route.fulfill({ json: { status: 'ok' } }));
  await page.route('**/api/projects', (route) => route.fulfill({ json: { projects: [] } }));
}

test.describe('RCG-0003 guardrail and recovery surfaces', () => {
  test('FSA-0282 positive: Workbench Status displays the authorization guard instead of fabricated passing readiness', async ({ page }) => {
    await installCommonMocks(page);
    await page.route('**/api/workbench/status/snapshot**', (route) =>
      route.fulfill({ status: 401, body: 'admin token required' })
    );

    await page.goto('/?view=workbench-status&project_id=demo');

    await expect(page.getByText('admin access required')).toBeVisible();
    await expect(page.getByText('configured, degraded, broken, busy, stale, approval_required')).toBeVisible();
  });

  test('FSA-0282 negative: Workbench Status request without an admin token stays unauthorized', async ({ page }) => {
    let sawRequestWithoutAdminToken = false;
    await installCommonMocks(page);
    await page.route('**/api/workbench/status/snapshot**', (route) => {
      sawRequestWithoutAdminToken = !route.request().headers()['x-admin-token'];
      return route.fulfill({ status: 401, body: 'admin token required' });
    });

    await page.goto('/?view=workbench-status&project_id=demo');

    expect(sawRequestWithoutAdminToken).toBe(true);
    await expect(page.getByText('admin access required')).toBeVisible();
  });

  test('FSA-0347 positive: Command Safety reports an auth failure when the trusted tool-pin decision guard rejects the request', async ({ page }) => {
    await installCommonMocks(page);
    await page.route('**/api/workbench/command-safety/profiles', (route) =>
      route.fulfill({ json: { profiles: [{ profile_id: 'readonly-local' }] } })
    );
    await page.route('**/api/workbench/command-safety/decide', (route) =>
      route.fulfill({ status: 401, body: 'not authorized to trust caller-minted tool pins' })
    );

    await page.goto('/?view=command-safety&project_id=demo');
    await page.getByRole('button', { name: 'Evaluate' }).click();

    await expect(page.locator('.command-safety-panel .error')).toContainText('401');
  });

  test('FSA-0347 negative: Command Safety request without an admin token is rejected visibly', async ({ page }) => {
    let sawRequestWithoutAdminToken = false;
    await installCommonMocks(page);
    await page.route('**/api/workbench/command-safety/profiles', (route) =>
      route.fulfill({ json: { profiles: [{ profile_id: 'readonly-local' }] } })
    );
    await page.route('**/api/workbench/command-safety/decide', (route) => {
      sawRequestWithoutAdminToken = !route.request().headers()['x-admin-token'];
      return route.fulfill({ status: 401, body: 'not authorized to trust caller-minted tool pins' });
    });

    await page.goto('/?view=command-safety&project_id=demo');
    await page.getByRole('button', { name: 'Evaluate' }).click();

    await expect(page.locator('.command-safety-panel .error')).toContainText('401');
    expect(sawRequestWithoutAdminToken).toBe(true);
  });

  test('Migration Wizard blocks apply until explicit backup confirmation is checked', async ({ page }) => {
    let applyBody = null;
    await installCommonMocks(page);
    await page.route('**/api/v1/workbench/migration/plan', (route) =>
      route.fulfill({
        json: {
          plan: {
            proposal_id: 'proposal-demo',
            evidence_refs: ['evidence:migration-plan-demo'],
            provenance_ref: 'provenance:migration-plan-demo',
            state: 'verified',
            conflicts: [],
            findings: [
              {
                item_id: 'history',
                label: 'Conversation history',
                path: 'old/history.jsonl',
                risk: 'low',
                default_selected: true,
                redacted_preview: 'history preview',
              },
            ],
          },
        },
      })
    );
    await page.route('**/api/v1/workbench/migration/apply', async (route) => {
      applyBody = route.request().postDataJSON();
      return route.fulfill({ json: { result: { status: 'applied', report_path: 'reports/migration.json' } } });
    });

    await page.goto('/?view=workbench-migration&project_id=demo');

    const applyButton = page.getByRole('button', { name: /Apply/ });
    await expect(applyButton).toBeDisabled();
    expect(applyBody).toBeNull();

    await page.getByLabel(/Backup completed and reviewed/).check();
    await expect(applyButton).toBeEnabled();
    await applyButton.click();

    await expect(page.locator('.apply-result')).toContainText('applied');
    expect(applyBody).toMatchObject({ proposal_id: 'proposal-demo', backup_confirmed: true });
  });

  test('FSA-0353 positive: Local Runtime smoke-test failure stays visible and does not report a passing receipt', async ({ page }) => {
    await installCommonMocks(page);
    await page.route('**/api/v1/workbench/onboarding/health', (route) =>
      route.fulfill({
        json: {
          readiness: {
            probes: [{ runtime_kind: 'lmstudio', reachable: false, base_url: 'http://127.0.0.1:1234', discovered_models: [], latency_ms: 0 }],
            blockers: [],
            hardware_fit_by_model: {},
            scheduler_lanes_ready: { default: false },
          },
        },
      })
    );
    await page.route('**/api/v1/workbench/onboarding/smoke-test', (route) =>
      route.fulfill({ status: 500, body: 'smoke test failed; receipt was not emitted' })
    );

    await page.goto('/?view=local-runtime');
    await page.getByRole('button', { name: /Run smoke test/ }).first().click();

    await expect(page.locator('.runtime-alert')).toContainText('smoke test failed');
    await expect(page.locator('.runtime-alert')).not.toContainText('passed');
  });

  test('FSA-0353 negative: Local Runtime smoke-test request without an admin token is rejected visibly', async ({ page }) => {
    let sawRequestWithoutAdminToken = false;
    await installCommonMocks(page);
    await page.route('**/api/v1/workbench/onboarding/health', (route) =>
      route.fulfill({
        json: {
          readiness: {
            probes: [{ runtime_kind: 'lmstudio', reachable: true, base_url: 'http://127.0.0.1:1234', discovered_models: ['demo'], latency_ms: 12 }],
            blockers: [],
            hardware_fit_by_model: {},
            scheduler_lanes_ready: { default: true },
          },
        },
      })
    );
    await page.route('**/api/v1/workbench/onboarding/smoke-test', (route) => {
      sawRequestWithoutAdminToken = !route.request().headers()['x-admin-token'];
      return route.fulfill({ status: 401, body: 'not authorized to emit local runtime smoke-test receipts' });
    });

    await page.goto('/?view=local-runtime');
    await page.getByRole('button', { name: /Run smoke test/ }).first().click();

    expect(sawRequestWithoutAdminToken).toBe(true);
    await expect(page.locator('.runtime-alert')).toContainText('not authorized');
  });
});
