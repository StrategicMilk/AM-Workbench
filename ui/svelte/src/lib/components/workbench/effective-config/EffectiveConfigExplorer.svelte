<script>
  import { workbenchKernelRequest } from '$lib/api.js';
  import { validateEffectiveConfigEntries } from '../../../configContract.js';

  let { projectId = 'default' } = $props();

  let loading = $state(true);
  let error = $state('');
  let payload = $state(null);

  $effect(() => {
    let cancelled = false;
    loading = true;
    error = '';
    getWorkbenchEffectiveConfigSnapshot(projectId)
      .then((data) => {
        if (!cancelled) payload = data;
      })
      .catch((err) => {
        if (!cancelled) {
          error = err?.message ?? 'effective config unavailable';
          payload = fallbackPayload(projectId);
        }
      })
      .finally(() => {
        if (!cancelled) loading = false;
      });
    return () => {
      cancelled = true;
    };
  });

  let latestSnapshot = $derived(payload?.snapshots?.[payload.snapshots.length - 1]);
  let entries = $derived(latestSnapshot?.entries ?? []);
  let diffs = $derived(payload?.diff ?? []);
  let validationResults = $derived(validateEffectiveConfigEntries(entries, payload?.schema ?? {}));
  let validationByKey = $derived(
    validationResults.reduce((acc, result) => {
      acc[result.key] = [...(acc[result.key] ?? []), result];
      return acc;
    }, {})
  );

  async function getWorkbenchEffectiveConfigSnapshot(id) {
    return workbenchKernelRequest(`/api/workbench/effective-config/snapshot?project_id=${encodeURIComponent(id)}`);
  }

  function fallbackPayload(id) {
    return {
      project_id: id,
      status: 'degraded',
      snapshots: [
        {
          snapshot_id: `client-fallback:${id}`,
          run_id: 'client-fallback',
          run_kind: 'ui-fallback',
          captured_at_utc: new Date().toISOString(),
          status: 'degraded',
          blockers: ['backend-effective-config-route-unavailable'],
          entries: [
            {
              category: 'runtime',
              key: 'explorer_backend',
              requested_value: '/api/workbench/effective-config/snapshot',
              effective_value: 'unavailable',
              source_layer: 'svelte-client',
              backend_accepted: false,
              provenance_ref: `project:${id}`,
              confidence: 0,
              safety_ref: 'fail-closed-client-fallback',
              budget_ref: 'not-measured',
              authority_ref: 'workbench-shell',
              persisted_ref: 'not-persisted',
              conflicts: [],
              fallback_reason: 'backend route unavailable',
              stale: true,
            },
          ],
        },
      ],
      diff: [],
    };
  }

  function confidenceLabel(value) {
    const numeric = Number(value);
    if (!Number.isFinite(numeric) || numeric < 0 || numeric > 1) {
      return 'confidence unavailable';
    }
    return `${Math.round(numeric * 100)}% confidence`;
  }
</script>

<section class="effective-config" aria-labelledby="effective-config-title">
  <header class="effective-config-header">
    <div>
      <p class="eyebrow">Workbench</p>
      <h1 id="effective-config-title">Effective Config</h1>
    </div>
    <span class:degraded={payload?.status !== 'ok'}>{loading ? 'loading' : payload?.status}</span>
  </header>

  {#if error}
    <div class="banner" role="alert" aria-live="assertive">{error}</div>
  {/if}

  <div class="summary-grid" aria-label="Snapshot summary">
    <div>
      <span>Run</span>
      <strong>{latestSnapshot?.run_id ?? 'none'}</strong>
    </div>
    <div>
      <span>Kind</span>
      <strong>{latestSnapshot?.run_kind ?? 'none'}</strong>
    </div>
    <div>
      <span>Entries</span>
      <strong>{entries.length}</strong>
    </div>
    <div>
      <span>Diffs</span>
      <strong>{diffs.length}</strong>
    </div>
  </div>

  <div class="table-wrap">
    <table aria-label="Effective configuration entries" aria-busy={loading}>
      <caption class="sr-only">Requested and effective configuration values for the active Workbench project</caption>
      <thead>
        <tr>
          <th>Category</th>
          <th>Setting</th>
          <th>Requested</th>
          <th>Effective</th>
          <th>Layer</th>
          <th>Signals</th>
        </tr>
      </thead>
      <tbody>
        {#each entries as entry}
          <tr class:stale={entry.stale || !entry.backend_accepted}>
            <td>{entry.category}</td>
            <td>
              <span>{entry.key}</span>
              {#each validationByKey[entry.key] ?? [] as result}
                <span class="validation-badge">{result.badge}</span>
              {/each}
            </td>
            <td>{String(entry.requested_value)}</td>
            <td>{String(entry.effective_value)}</td>
            <td>{entry.source_layer}</td>
            <td>
              <span>{entry.backend_accepted ? 'accepted' : 'rejected'}</span>
              <span>{confidenceLabel(entry.confidence)}</span>
              {#if entry.fallback_reason}<span>{entry.fallback_reason}</span>{/if}
            </td>
          </tr>
        {/each}
      </tbody>
    </table>
  </div>

  <div class="diff-list" aria-label="Effective config diff">
    {#each diffs as diff}
      <div class="diff-row">
        <strong>{diff.category}.{diff.key}</strong>
        <span>{String(diff.before_effective_value)} -> {String(diff.after_effective_value)}</span>
      </div>
    {:else}
      <div class="diff-row muted">No effective value changes in the current comparison.</div>
    {/each}
  </div>

  {#if validationResults.length}
    <div class="validation-list" aria-label="Effective config validation results">
      {#each validationResults as result}
        <div><strong>{result.badge}</strong> {result.key}: {result.message}</div>
      {/each}
    </div>
  {/if}
</section>

<style>
  .effective-config {
    display: flex;
    flex-direction: column;
    gap: 18px;
    padding: 24px;
    color: var(--text-primary);
  }

  .effective-config-header {
    display: flex;
    align-items: flex-end;
    justify-content: space-between;
    gap: 16px;
  }

  .eyebrow {
    margin: 0 0 4px;
    color: var(--text-muted);
    font-size: 12px;
    text-transform: uppercase;
  }

  h1 {
    margin: 0;
    font-size: 28px;
    font-weight: 700;
  }

  .effective-config-header span,
  .summary-grid span,
  .table-wrap td span {
    color: var(--text-muted);
    font-size: 12px;
  }

  .validation-badge {
    display: inline-flex;
    margin-left: 6px;
    border: 1px solid var(--warning, #f59e0b);
    border-radius: 6px;
    padding: 2px 4px;
    color: var(--warning, #f59e0b);
  }

  .effective-config-header span {
    border: 1px solid var(--border-default);
    border-radius: 999px;
    padding: 6px 10px;
  }

  .effective-config-header span.degraded {
    color: var(--warning, #f59e0b);
    border-color: var(--warning, #f59e0b);
  }

  .banner {
    border-left: 3px solid var(--warning, #f59e0b);
    background: var(--surface-elevated);
    padding: 10px 12px;
    color: var(--text-secondary);
  }

  .sr-only {
    position: absolute;
    width: 1px;
    height: 1px;
    padding: 0;
    margin: -1px;
    overflow: hidden;
    clip: rect(0, 0, 0, 0);
    white-space: nowrap;
  }

  .summary-grid {
    display: grid;
    grid-template-columns: repeat(4, minmax(0, 1fr));
    gap: 12px;
  }

  .summary-grid div {
    border: 1px solid var(--border-default);
    border-radius: 8px;
    padding: 12px;
    min-width: 0;
  }

  .summary-grid strong {
    display: block;
    overflow-wrap: anywhere;
    margin-top: 6px;
    font-size: 14px;
  }

  .table-wrap {
    overflow-x: auto;
    border: 1px solid var(--border-default);
    border-radius: 8px;
  }

  table {
    width: 100%;
    border-collapse: collapse;
    min-width: 860px;
  }

  th,
  td {
    padding: 10px 12px;
    border-bottom: 1px solid var(--border-default);
    text-align: left;
    vertical-align: top;
    font-size: 13px;
  }

  th {
    color: var(--text-muted);
    font-weight: 600;
  }

  tr.stale td {
    background: rgba(245, 158, 11, 0.08);
  }

  .diff-list {
    display: grid;
    gap: 8px;
  }

  .diff-row {
    display: flex;
    justify-content: space-between;
    gap: 16px;
    border-bottom: 1px solid var(--border-default);
    padding: 8px 0;
    font-size: 13px;
  }

  .diff-row span {
    overflow-wrap: anywhere;
    text-align: right;
  }

  .muted {
    color: var(--text-muted);
  }

  @media (max-width: 760px) {
    .effective-config {
      padding: 16px;
    }

    .summary-grid {
      grid-template-columns: repeat(2, minmax(0, 1fr));
    }

    .diff-row {
      flex-direction: column;
    }

    .diff-row span {
      text-align: left;
    }
  }
</style>
