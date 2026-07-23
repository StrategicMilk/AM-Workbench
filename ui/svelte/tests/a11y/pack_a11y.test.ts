import { expect, test } from '@playwright/test';
import AxeBuilder from '@axe-core/playwright';
import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../../..');

const capabilitySummary = {
  capabilities: [
    {
      kind: 'local-runtime',
      display_name: 'Local Runtime',
      risk_level: 'moderate',
      target_environment: 'local',
      install_state: 'not_installed',
      health_state: 'unknown',
    },
  ],
};

const capabilityMetadata = {
  kind: 'local-runtime',
  display_name: 'Local Runtime',
  risk_level: 'moderate',
  target_environment: 'local',
  disk_impact_mb: 128,
  network_impact_mb: 16,
  requires_native_binary: false,
  requires_wsl: false,
  requires_credentials: [],
  extra_packages: ['vetinari-runtime'],
  degraded_fallback: 'Manual runtime setup remains available.',
  uninstall_note: 'Remove the runtime package to uninstall.',
};

async function installCapabilityMocks(page) {
  await page.route('**/health', async (route) => {
    await route.fulfill({ json: { status: 'ok' } });
  });
  await page.route('**/api/**', async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname === '/api/projects') {
      await route.fulfill({ json: { projects: [] } });
      return;
    }
    if (url.pathname === '/api/v1/capabilities') {
      await route.fulfill({ json: capabilitySummary });
      return;
    }
    if (url.pathname === '/api/v1/capabilities/local-runtime/probe') {
      await route.fulfill({ json: { reachable: true } });
      return;
    }
    if (url.pathname === '/api/v1/capabilities/local-runtime') {
      await route.fulfill({ json: capabilityMetadata });
      return;
    }
    await route.fulfill({ json: {} });
  });
}

test.describe('PACK-A11Y component remediation probes', () => {
  test('install approval modal traps focus, closes on Escape, and has named controls', async ({ page }) => {
    await installCapabilityMocks(page);
    await page.goto('/?view=capabilities');

    await page.getByRole('button', { name: 'Install Local Runtime' }).click();
    const dialog = page.getByRole('dialog', { name: 'Approve capability install' });
    await expect(dialog).toBeVisible();
    await expect(dialog.getByRole('button', { name: 'Close install approval' })).toBeVisible();

    for (let i = 0; i < 8; i++) {
      await page.keyboard.press('Tab');
      const focusedInsideDialog = await page.evaluate(
        () => document.activeElement?.closest('[role="dialog"]') !== null
      );
      expect(focusedInsideDialog).toBe(true);
    }

    const axeResults = await new AxeBuilder({ page })
      .include('[role="dialog"]')
      .withTags(['wcag2a', 'wcag2aa'])
      .analyze();
    expect(axeResults.violations).toHaveLength(0);

    await page.keyboard.press('Escape');
    await expect(dialog).toHaveCount(0);
  });

  test('sidebar connection dot exposes a non-color status label', async ({ page }) => {
    await installCapabilityMocks(page);
    await page.goto('/?view=capabilities');

    await expect(page.locator('.sidebar-footer .status-dot')).toHaveAttribute(
      'aria-label',
      /Stream active|No stream/
    );
  });

  test('annotation and sensitive workflow controls use explicit label associations', async () => {
    const annotation = await fs.readFile(
      path.join(repoRoot, 'ui/svelte/src/components/workbench/AnnotationCard.svelte'),
      'utf8'
    );
    const sensitive = await fs.readFile(
      path.join(repoRoot, 'ui/svelte/src/lib/components/workbench/life_admin/SensitiveWorkflowPanel.svelte'),
      'utf8'
    );

    expect(annotation).toContain('for={controlId(field.name)}');
    expect(annotation).toContain('id={controlId(field.name)}');
    expect(annotation).toContain('aria-required={field.required');
    expect(annotation).toContain('role="alert"');
    expect(sensitive).toContain('for="sensitive-workflow-jurisdiction"');
    expect(sensitive).toContain('id="sensitive-workflow-jurisdiction"');
    expect(sensitive).toContain('aria-describedby={jurisdictionHelpId}');
    expect(sensitive).toContain('id="sensitive-workflow-tax-year"');
    expect(sensitive).toContain('id="sensitive-workflow-authority-ref"');
  });

  test('sensitive workflow jurisdiction help is wired only when required', async ({ page }) => {
    await installCapabilityMocks(page);
    await page.goto('/?view=professional-life');

    const domain = page.getByLabel('Sensitive domain');
    const jurisdiction = page.getByLabel('Jurisdiction');
    const help = page.locator('#sensitive-workflow-jurisdiction-help');

    await expect(domain).toHaveValue('tax');
    await expect(help).toBeVisible();
    await expect(jurisdiction).toHaveAttribute('aria-describedby', 'sensitive-workflow-jurisdiction-help');
    await expect(help).toHaveText('Jurisdiction is required for this domain.');

    await domain.selectOption('general_professional');

    await expect(help).toHaveCount(0);
    await expect(jurisdiction).not.toHaveAttribute('aria-describedby', /.+/);
  });
});
