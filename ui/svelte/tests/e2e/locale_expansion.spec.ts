import { expect, test } from '@playwright/test';
import { readFileSync } from 'node:fs';

const localeFixture = JSON.parse(
  readFileSync(new URL('./fixtures/long-label-locale.json', import.meta.url), 'utf-8')
);

async function installLocaleMocks(page) {
  await page.route('**/health', (route) => route.fulfill({ json: { status: 'ok' } }));
  await page.route('**/api/**', (route) => {
    const url = new URL(route.request().url());
    if (url.pathname === '/api/projects') {
      return route.fulfill({ json: { projects: [] } });
    }
    if (url.pathname === '/api/workbench/domain-kits') {
      return route.fulfill({ json: { kits: localeFixture.domain_kits } });
    }
    return route.fulfill({ json: {} });
  });
}

test.describe('Locale expansion and long-label resilience', () => {
  test('domain kit long localized labels wrap inside keyboard selection controls', async ({ page }) => {
    await installLocaleMocks(page);
    await page.setViewportSize({ width: 1024, height: 760 });
    await page.goto('/?view=domain-kits');

    const kitButton = page.getByRole('button', {
      name: /Arbeitsbereichsuebergreifende Kontextwiederherstellungsfreigabe/,
    });
    await expect(kitButton).toBeVisible();
    await kitButton.focus();
    await expect(kitButton).toBeFocused();

    const overflow = await kitButton.evaluate((element) => element.scrollWidth > element.clientWidth + 1);
    expect(overflow).toBe(false);
  });
});
