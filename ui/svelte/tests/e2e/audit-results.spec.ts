import { expect, test } from '@playwright/test';

const runSummary = {
  run_id: '20260524T010000Z-full-spectrum',
  status: 'partial',
  phase: 'converged',
  current_round: 3,
  started_at: '2026-05-24T00:00:00Z',
  completed_at: '2026-05-24T01:00:00Z',
  archived: false,
  pinned: true,
  finding_count: 2,
  open_findings: 1,
  severity_counts: { critical: 1, medium: 1 },
  lane_counts: { 'user-supportability': 1, accessibility: 1 },
  closure_status_counts: { 'still-open': 1, resolved: 1 },
  artifact_refs: ['outputs/audit/demo/finding-registry.json'],
  top_findings: [{ id: 'FSA-9001', severity: 'critical', lane: 'user-supportability', title: 'Visible branch' }],
};

test('audit results journey reaches run details and refilters findings through the API', async ({ page }) => {
  const detailQueries: string[] = [];

  await page.route('**/health', (route) => route.fulfill({ json: { status: 'ok' } }));
  await page.route('**/api/**', (route) => {
    const url = new URL(route.request().url());
    if (url.pathname === '/api/audit/full-spectrum/results') {
      return route.fulfill({
        json: {
          status: 'ok',
          runs: [runSummary],
          summary: { total_runs: 1, visible_runs: 1, skipped_runs: 0, open_findings: 1, total_findings: 2 },
        },
      });
    }
    if (url.pathname === '/api/audit/full-spectrum/results/20260524T010000Z-full-spectrum') {
      detailQueries.push(url.searchParams.toString());
      return route.fulfill({
        json: {
          status: 'ok',
          run: {
            ...runSummary,
            finding_filter: {
              status: url.searchParams.get('finding_status') ?? 'open',
              severity: url.searchParams.get('severity') ?? 'all',
              lane: url.searchParams.get('lane') ?? 'all',
              query: url.searchParams.get('query') ?? '',
            },
            finding_result_count: 1,
            finding_limit: 50,
            findings: [
              {
                id: 'FSA-9001',
                severity: 'critical',
                lane: 'user-supportability',
                status: 'open',
                closure_status: 'still-open',
                title: 'Visible branch',
              },
            ],
            lane_artifacts: [
              {
                lane: 'user-supportability',
                path: 'outputs/audit/demo/user-supportability/LANE-EVIDENCE.json',
              },
            ],
          },
        },
      });
    }
    return route.fulfill({ json: {} });
  });

  await page.goto('/?view=audit-results');

  await expect(page.getByRole('heading', { name: 'Audit Results' })).toBeVisible();
  await expect(page.getByRole('button', { name: /20260524T010000Z-full-spectrum/ })).toBeVisible();
  await expect(page.getByRole('table', { name: 'Filtered full-spectrum findings' })).toContainText('FSA-9001');

  await page.getByLabel('Finding severity filter').selectOption('critical');
  await page.getByLabel('Finding lane filter').selectOption('user-supportability');
  await page.getByLabel('Finding text search').fill('visible');

  await expect.poll(() => detailQueries.some((query) => query.includes('severity=critical'))).toBeTruthy();
  await expect.poll(() => detailQueries.some((query) => query.includes('lane=user-supportability'))).toBeTruthy();
  await expect.poll(() => detailQueries.some((query) => query.includes('query=visible'))).toBeTruthy();
});
