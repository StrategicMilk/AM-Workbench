// Smoke tests for the per-project ProjectReceiptStrip.
//
// SHARD-03 task 3.3 (PLAN 2026-04-25-roadmap-03-followups Item 2):
// project cards must show counts by WorkReceiptKind and the spec-required
// "No work recorded yet" empty state. These tests verify both states
// against the real Vite preview build.

import { test, expect } from '@playwright/test';

/**
 * Permissive fallback mocks. Specific tests layer their own
 * ``page.route()`` overrides on the receipts endpoints.
 */
async function installFallbacks(page) {
  await page.route('**/health', async (route) => {
    await route.fulfill({ json: { status: 'ok' } });
  });
  await page.route('**/api/**', async (route) => {
    const url = route.request().url();

    if (url.endsWith('/api/projects')) {
      // The Projects view fetches the project list. Provide one project
      // so a card actually renders (and therefore mounts the strip).
      await route.fulfill({
        json: {
          projects: [
            {
              id: 'proj-fixture',
              project_id: 'proj-fixture',
              name: 'Fixture Project',
              description: 'Used by Playwright smoke tests',
              status: 'pending',
              created_at: '2026-04-25T11:00:00+00:00',
              updated_at: '2026-04-25T11:00:00+00:00',
            },
          ],
        },
      });
      return;
    }

    if (url.includes('/api/projects/proj-fixture/receipts')) {
      // Will be overridden per test.
      await route.fulfill({ json: { project_id: 'proj-fixture', total: 0, offset: 0, limit: 500, receipts: [] } });
      return;
    }

    if (url.endsWith('/api/attention')) {
      await route.fulfill({ json: { count: 0, items: [] } });
      return;
    }

    await route.fulfill({ json: {} });
  });
}

test.describe('ProjectReceiptStrip', () => {
  test('renders the "No work recorded yet" empty state', async ({ page }) => {
    await installFallbacks(page);
    await page.route('**/api/projects/proj-fixture/receipts**', async (route) => {
      await route.fulfill({
        json: { project_id: 'proj-fixture', total: 0, offset: 0, limit: 500, receipts: [] },
      });
    });

    await page.goto('/#workflow');

    // The exact phrase is the SHARD-03 contract — anti-pattern: Fallback as success.
    await expect(page.getByText('No work recorded yet')).toBeVisible();
  });

  test('renders kind chips with counts for a populated project', async ({ page }) => {
    await installFallbacks(page);
    const kinds = ['plan_round', 'worker_task', 'worker_task', 'inspector_pass', 'training_step', 'release_step'];
    await page.route('**/api/projects/proj-fixture/receipts**', async (route) => {
      const receipts = kinds.map((kind, idx) => ({
        receipt_id: `rcpt-${idx}`,
        project_id: 'proj-fixture',
        agent_id: kind === 'release_step' ? 'release-doctor:0.1.0' : 'agent-001',
        agent_type: kind.startsWith('plan')
          ? 'FOREMAN'
          : kind.startsWith('worker')
            ? 'WORKER'
            : kind.startsWith('inspector')
              ? 'INSPECTOR'
              : null,
        kind,
        outcome: { passed: true, score: 0.9, basis: 'tool_evidence', issues: [], suggestions: [], provenance: null },
        awaiting_user: false,
        awaiting_reason: null,
        linked_claim_ids: [],
        inputs_summary: 'fixture',
        outputs_summary: 'fixture',
        started_at_utc: '2026-04-25T11:00:00+00:00',
        finished_at_utc: '2026-04-25T11:01:00+00:00',
      }));
      await route.fulfill({
        json: {
          project_id: 'proj-fixture',
          total: receipts.length,
          offset: 0,
          limit: 500,
          receipts,
        },
      });
    });

    await page.goto('/#workflow');

    // Each kind chip must show its label.
    const strip = page.locator('.receipt-strip').first();
    await expect(strip.getByText('plan', { exact: true })).toBeVisible();
    await expect(strip.getByText('work', { exact: true })).toBeVisible();
    await expect(strip.getByText('inspect', { exact: true })).toBeVisible();
    await expect(strip.getByText('train', { exact: true })).toBeVisible();
    await expect(strip.getByText('release', { exact: true })).toBeVisible();

    // The worker chip must show count "2" (we seeded two worker_task receipts).
    const workerChip = strip.locator('.strip-chip', { hasText: 'work' });
    await expect(workerChip.locator('.chip-count')).toHaveText('2');

    // Empty-state copy must NOT be visible for a populated project.
    await expect(strip.getByText('No work recorded yet')).toHaveCount(0);
  });
});
