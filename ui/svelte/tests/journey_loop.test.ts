import { expect, test } from '@playwright/test';

const appUrl = 'http://127.0.0.1:5174';

const runRows = [
  {
    run_id: 'run-1',
    kind: 'experiment',
    status: 'passed',
    started_at_utc: '2026-05-12T13:00:00Z',
    finished_at_utc: '2026-05-12T13:02:00Z',
    actor_agent_type: 'worker',
    asset_revisions: [['asset-1', 'v1']],
    lease_id: 'lease-1',
    shard_kind: 'ui',
    metrics: [{ name: 'quality', value: 0.91, unit: '' }],
    project_id: 'demo',
  },
];

const traceRows = [
  {
    trace_id: 'trace-1',
    run_id: 'run-1',
    root_span_id: 'span-1',
    captured_at_utc: '2026-05-12T13:02:00Z',
    spans: [
      {
        span_id: 'span-1',
        parent_span_id: '',
        tool_name: 'eval',
        started_at_utc: '2026-05-12T13:00:00Z',
        finished_at_utc: '2026-05-12T13:02:00Z',
        inputs_hash: 'in',
        outputs_hash: 'out',
        error: '',
        duration_ms: 1200,
      },
    ],
  },
];

const experiments = [
  {
    experiment_id: 'exp-a',
    source_trace_id: 'trace-1',
    source_run_id: 'run-1',
    asset_id: 'asset-1',
    asset_revision: 'v1',
    prompt_text: 'draft',
    agent_edits: [],
    tool_overrides: [],
    model_overrides: [],
    created_at_utc: '2026-05-12T13:05:00Z',
    score: 0.62,
    notes: 'baseline',
  },
  {
    experiment_id: 'exp-b',
    source_trace_id: 'trace-1',
    source_run_id: 'run-1',
    asset_id: 'asset-1',
    asset_revision: 'v1',
    prompt_text: 'winner',
    agent_edits: [],
    tool_overrides: [],
    model_overrides: [],
    created_at_utc: '2026-05-12T13:06:00Z',
    score: 0.94,
    notes: 'winner',
  },
];

const promotions = {
  items: [
    {
      proposal_id: 'proposal-1',
      kind: 'method',
      status: 'pending',
      affected_assets: ['asset-1'],
      gate_passed: true,
      gate_blockers: [],
      gate_evidence: {
        eval_count_matched: 2,
        eval_count_failing: 0,
        taint_count: 0,
        plan_feedback_match_count: 0,
        source_run_id: 'run-1',
        source_trace_id: 'trace-1',
        asset_id: 'asset-1',
      },
      opened_at_utc: '2026-05-12T13:07:00Z',
    },
  ],
};

async function installJourneyLoopMocks(page) {
  await page.route('**/health', (route) => route.fulfill({ json: { status: 'ok' } }));
  await page.route('**/api/**', (route) => {
    const url = new URL(route.request().url());
    if (url.pathname === '/api/projects') {
      return route.fulfill({ json: { projects: [] } });
    }
    if (url.pathname === '/api/workbench/console/runs') {
      return route.fulfill({ json: runRows });
    }
    if (url.pathname === '/api/workbench/console/runs/run-1/traces') {
      return route.fulfill({ json: traceRows });
    }
    if (url.pathname === '/api/workbench/console/assets') {
      return route.fulfill({
        json: [
          {
            asset_id: 'asset-1',
            kind: 'prompt',
            name: 'Asset One',
            revision: 'v1',
            created_at_utc: '2026-05-12T13:00:00Z',
            taints: [],
            provenance: {},
          },
        ],
      });
    }
    if (url.pathname === '/api/workbench/playground/experiments') {
      return route.fulfill({ json: { experiments, count: experiments.length } });
    }
    if (url.pathname === '/api/v1/projects/demo/workbench/promotions') {
      return route.fulfill({ json: promotions });
    }
    return route.fulfill({ json: {} });
  });
}

test.describe('Workbench journey loop', () => {
  test('console opens a selected evidence trace in playground with typed prepopulation', async ({ page }) => {
    await installJourneyLoopMocks(page);

    await page.goto(`${appUrl}/projects/demo/workbench-console`);

    await expect(page.getByRole('heading', { name: 'Workbench Console' })).toBeVisible();
    await page.getByRole('link', { name: 'Open this run trace in Workbench Playground' }).click();

    await expect(page.getByRole('heading', { name: 'Workbench Playground' })).toBeVisible();
    await expect(page.getByLabel('Run ID')).toHaveValue('run-1');
    await expect(page.getByLabel('Trace ID')).toHaveValue('trace-1');
    await expect(page.getByLabel('Asset ID')).toHaveValue('asset-1');
    await expect(page.getByLabel('Asset Revision')).toHaveValue('v1');
  });

  test('playground hands a winning experiment to promotion inbox and preserves the trace target', async ({ page }) => {
    await installJourneyLoopMocks(page);

    await page.goto(`${appUrl}/projects/demo/workbench-playground?run_id=run-1&trace_id=trace-1&asset_id=asset-1&asset_revision=v1`);

    await expect(page.getByLabel('Experiment handoff')).toContainText('exp-b');
    await page.getByRole('link', { name: 'Open winning experiment in Promotion Inbox' }).click();

    await expect(page.getByRole('heading', { name: 'Promotion Inbox' })).toBeVisible();
    await expect(page.getByLabel('Promotion evidence trace target')).toContainText('exp-b');
    await expect(page.getByRole('link', { name: 'Open promotion evidence trace in Workbench Console' })).toHaveCount(1);

    await page.getByRole('link', { name: 'Open promotion evidence trace in Workbench Console' }).first().click();
    await expect(page.getByRole('heading', { name: 'Workbench Console' })).toBeVisible();
    await expect(page).toHaveURL(/evidence_trace_id=trace-1/);
  });
});
