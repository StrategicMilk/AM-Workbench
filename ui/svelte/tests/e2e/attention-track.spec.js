// Smoke tests for the Dashboard's AttentionTrack band.
//
// SHARD-03 task 3.4 (PLAN 2026-04-25-roadmap-03-followups Item 2):
// "UI test renders a project with 0 receipts and asserts the empty-state
// placeholder." These tests do that for the Attention Track specifically
// — the empty state ("No attention required") and a populated state with
// real ``awaiting_reason`` strings.
//
// All API calls are mocked via ``page.route()`` so no live backend is
// required.

import { test, expect } from '@playwright/test';

/**
 * Install permissive 200-OK fallback mocks for every /api/ endpoint the
 * Dashboard touches, so the page renders without error. Specific tests
 * layer their own ``page.route()`` overrides on top.
 */
async function installFallbacks(page) {
  await page.route('**/health', async (route) => {
    await route.fulfill({ json: { status: 'ok' } });
  });
  await page.route('**/api/**', async (route) => {
    const url = route.request().url();

    // Default: empty body wrapped in the most common shape.
    if (url.endsWith('/api/attention')) {
      // Will be overridden per test, but provide a safe default.
      await route.fulfill({ json: { count: 0, items: [] } });
      return;
    }
    if (url.includes('/api/projects/')) {
      await route.fulfill({ json: { project_id: 'unknown', total: 0, offset: 0, limit: 100, receipts: [] } });
      return;
    }
    if (url.endsWith('/api/projects')) {
      await route.fulfill({ json: { projects: [] } });
      return;
    }

    // Fallback for any unrelated dashboard endpoint (KPIs, hardware,
    // activity feed) — empty-but-shaped JSON keeps the page alive.
    await route.fulfill({ json: {} });
  });
}

test.describe('AttentionTrack', () => {
  test('renders the "No attention required" empty state', async ({ page }) => {
    await installFallbacks(page);
    await page.route('**/api/attention', async (route) => {
      await route.fulfill({ json: { count: 0, items: [] } });
    });

    await page.goto('/#dashboard');

    // The empty-state copy comes from the component, not the API; if the
    // component synthesised a fake "in progress" status we would see that
    // string instead — anti-pattern: Fallback as success.
    await expect(page.getByText('No attention required')).toBeVisible();
    // The header is still rendered.
    await expect(page.getByRole('heading', { name: /Attention required/i })).toBeVisible();
    await expect(page.getByText(/^updated /)).toHaveAttribute('aria-hidden', 'true');
    await expect(page.getByText('No attention required')).toHaveAttribute('role', 'status');
    // The list itself must be empty.
    await expect(page.locator('.attention-list')).toHaveCount(0);
  });

  test('renders awaiting items with structured awaiting_reason', async ({ page }) => {
    await installFallbacks(page);
    await page.route('**/api/attention', async (route) => {
      await route.fulfill({
        json: {
          count: 2,
          items: [
            {
              receipt_id: 'rcpt-001',
              project_id: 'proj-A',
              kind: 'inspector_pass',
              awaiting_user: true,
              awaiting_reason: 'inspector surfaced unsupported claims: 2',
              finished_at_utc: '2026-04-25T12:00:00+00:00',
            },
            {
              receipt_id: 'rcpt-002',
              project_id: 'proj-B',
              kind: 'plan_round',
              awaiting_user: true,
              awaiting_reason: 'plan reviewer refused -- TS-14 non-goals matched',
              finished_at_utc: '2026-04-25T12:01:00+00:00',
            },
          ],
        },
      });
    });

    await page.goto('/#dashboard');

    // Both reasons must be displayed verbatim (no client-side synthesis).
    await expect(page.getByText('inspector surfaced unsupported claims: 2')).toBeVisible();
    await expect(page.getByText('plan reviewer refused -- TS-14 non-goals matched')).toBeVisible();
    // Project ids surface so the user knows which project is blocked.
    await expect(page.getByText('proj-A', { exact: true })).toBeVisible();
    await expect(page.getByText('proj-B', { exact: true })).toBeVisible();
    // Empty-state copy must NOT be visible when there are awaiting items.
    await expect(page.getByText('No attention required')).toHaveCount(0);
  });
});
