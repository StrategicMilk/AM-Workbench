function deepFreeze(value) {
  if (!value || typeof value !== 'object' || Object.isFrozen(value)) {
    return value;
  }

  for (const child of Object.values(value)) {
    deepFreeze(child);
  }

  return Object.freeze(value);
}

export function cloneApiFixture(value) {
  if (typeof structuredClone === 'function') {
    return structuredClone(value);
  }
  return JSON.parse(JSON.stringify(value));
}

export const apiEmptyEnvelope = deepFreeze({
  results: [],
});

export const apiErrorEnvelope = deepFreeze({
  error: 'request failed',
  detail: 'The request could not be completed.',
});

export const resourceCockpitSnapshot = deepFreeze({
  machine_profile: {},
  runtime_appliance: {},
  active_leases: [
    {
      lease_id: 'l1',
      status: { label: 'active', color: 'green' },
      action: { id: 'release', label: 'Release' },
    },
  ],
  queued_jobs: [
    {
      job_id: 'j1',
      workload: 'batch-a',
      priority: 1,
    },
  ],
  safe_actions: [],
  policy_proposals: [],
  degradation_reasons: [],
  overall_status: 'healthy',
});

export const workbenchStatusSnapshot = deepFreeze({
  results: [
    {
      domain: 'config',
      state: 'configured',
      label: 'Config',
      checks: [],
    },
  ],
});

export const domainReviewQueuesPayload = deepFreeze({
  queues: [
    { label: 'Ready', count: 5 },
    { label: 'Gold', count: 2 },
  ],
  work_items: [
    {
      id: 'w1',
      title: 'Citation risk',
      risk: 'high',
    },
  ],
});

export const ragEnvelope = deepFreeze({
  results: [
    {
      id: 'r1',
      score: 0.9,
      text: 'sample',
    },
  ],
});

export const preferenceCardsPayload = deepFreeze({
  preferences: [
    {
      id: 'p1',
      label: 'Response style',
      value: 'concise',
    },
  ],
});

export const effectiveConfigPayload = deepFreeze({
  status: 'ok',
  snapshots: [
    {
      snapshot_id: 'snap-1',
      run_id: 'run-1',
      run_kind: 'scheduled',
      captured_at_utc: '2026-01-01T00:00:00Z',
      status: 'ok',
      entries: [
        {
          category: 'runtime',
          key: 'explorer_backend',
          requested_value: '/api/workbench/effective-config/snapshot',
          effective_value: '/api/workbench/effective-config/snapshot',
          source_layer: 'kernel',
          backend_accepted: true,
          confidence: 1,
          stale: false,
          conflicts: [],
        },
      ],
    },
  ],
  diff: [],
});

export const annotationQueuePayload = deepFreeze({
  queue: [
    {
      id: 'a1',
      text: 'sample annotation',
    },
  ],
  commits: [
    {
      id: 'c1',
      approved: false,
    },
  ],
});

export const extensionsList = deepFreeze([
  {
    id: 'ext-1',
    name: 'ExampleExt',
    installed: false,
  },
]);

export const promotionRecipes = deepFreeze([
  {
    id: 'r1',
    name: 'Promote to gold',
    ready: false,
  },
]);
