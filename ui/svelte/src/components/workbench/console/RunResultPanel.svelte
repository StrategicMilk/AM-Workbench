<script>
  import JourneyLink from './JourneyLink.svelte';

  let { run = null, trace = null, projectId = 'default' } = $props();

  let firstRevision = $derived(run?.asset_revisions?.[0] ?? []);
  let assetId = $derived(firstRevision[0] ?? '');
  let assetRevision = $derived(firstRevision[1] ?? '');
  let traceId = $derived(trace?.trace_id ?? '');
  let canInspect = $derived(Boolean(run?.run_id && traceId && assetId && assetRevision));
  let statusLabel = $derived(canInspect ? (run?.status ?? 'none') : `${run?.status ?? 'none'} - evidence incomplete`);
</script>

<section class="run-result-panel" aria-label="Run journey actions">
  <div>
    <h3>Run result</h3>
    <span>{statusLabel}</span>
  </div>
  <JourneyLink
    target="playground"
    {projectId}
    disabled={!canInspect}
    label="Experiment"
    ariaLabel="Open this run trace in Workbench Playground"
    params={{
      runId: run?.run_id,
      traceId,
      assetId,
      assetRevision,
    }}
  />
</section>

<style>
  .run-result-panel {
    align-items: center;
    border: 1px solid var(--border-default);
    border-radius: 8px;
    background: var(--surface-elevated);
    display: flex;
    gap: 0.75rem;
    justify-content: space-between;
    padding: 0.85rem 1rem;
  }

  h3,
  span {
    margin: 0;
  }

  h3 {
    color: var(--text-primary);
    font-size: 0.92rem;
  }

  span {
    color: var(--text-muted);
    font-size: 0.78rem;
    overflow-wrap: anywhere;
  }

  @media (max-width: 620px) {
    .run-result-panel {
      align-items: stretch;
      flex-direction: column;
    }
  }
</style>
