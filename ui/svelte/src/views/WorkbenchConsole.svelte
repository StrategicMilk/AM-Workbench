<script>
  import { showToast } from '$lib/stores/toast.svelte.js';
  import RunTable from '$components/workbench/RunTable.svelte';
  import RunDetailPane from '$components/workbench/RunDetailPane.svelte';
  import TraceWaterfall from '$components/workbench/TraceWaterfall.svelte';
  import AssetCard from '$components/workbench/AssetCard.svelte';
  import ConsoleFilterBar from '$components/workbench/ConsoleFilterBar.svelte';
  import ConsoleStatusPane from '$components/workbench/ConsoleStatusPane.svelte';
  import ProvenanceBadge from '$components/workbench/ProvenanceBadge.svelte';
  import HardwareFitBadge from '$components/workbench/HardwareFitBadge.svelte';
  import PolicyBadge from '$components/workbench/PolicyBadge.svelte';
  import ArtifactDrawer from '$components/workbench/ArtifactDrawer.svelte';
  import { workbenchKernelRequest } from '$lib/api.js';
  import RunResultPanel from '$components/workbench/console/RunResultPanel.svelte';
  import { readWorkbenchJourneyState } from '$lib/workbench/journey_router.ts';
  import { browserLocationParam, isBrowser } from '$lib/utils/browser.js';

  let { projectId = browserLocationParam('project_id', 'default') } = $props();

  const initialJourneyState = readWorkbenchJourneyState(isBrowser() ? window.location.search : '');
  let runs = $state([]);
  let traces = $state([]);
  let assets = $state([]);
  let selectedRunId = $state(initialJourneyState.runId ?? null);
  let requestedTraceId = $state(initialJourneyState.evidenceTraceId ?? initialJourneyState.traceId ?? null);
  let loading = $state(true);
  let error = $state(null);
  let drawerRecord = $state(null);
  let filterState = $state({ kind: null, status: null, leaseId: null });

  let selectedRun = $derived(runs.find((run) => run.run_id === selectedRunId) ?? null);
  let selectedTrace = $derived(
    traces.find((trace) => trace.trace_id === requestedTraceId) ?? traces[0] ?? null
  );

  function workbenchUrl(path, params = {}) {
    const query = new URLSearchParams();
    for (const [key, value] of Object.entries(params)) {
      if (value !== null && value !== undefined && value !== '') query.set(key, value);
    }
    return query.toString() ? `${path}?${query.toString()}` : path;
  }

  $effect(() => {
    let cancelled = false;
    loading = true;
    error = null;
    Promise.all([
      workbenchKernelRequest(workbenchUrl('/api/workbench/console/runs', {
        project_id: projectId,
        kind: filterState.kind,
        status: filterState.status,
        lease_id: filterState.leaseId,
      })),
      workbenchKernelRequest(workbenchUrl('/api/workbench/console/assets', { project_id: projectId })),
    ]).then(([runRows, assetRows]) => {
      if (cancelled) return;
      runs = runRows;
      assets = assetRows;
      if (!selectedRunId && runRows.length > 0) selectedRunId = runRows[0].run_id;
      loading = false;
    }).catch((err) => {
      if (cancelled) return;
      error = err.message ?? String(err);
      showToast(`Workbench console load failed: ${error}`, 'error');
      loading = false;
    });
    return () => { cancelled = true; };
  });

  $effect(() => {
    if (!selectedRunId) {
      traces = [];
      return;
    }
    let cancelled = false;
    workbenchKernelRequest(workbenchUrl(`/api/workbench/console/runs/${encodeURIComponent(selectedRunId)}/traces`, { project_id: projectId }))
      .then((rows) => { if (!cancelled) traces = rows; })
      .catch((err) => { if (!cancelled) showToast(`Trace load failed: ${err.message ?? err}`, 'error'); });
    return () => { cancelled = true; };
  });
</script>

<div class="workbench-console">
  <header class="console-header">
    <div>
      <h2>Workbench Console</h2>
      <p>{projectId}</p>
    </div>
    <ConsoleStatusPane {selectedRun} runCount={runs.length} assetCount={assets.length} />
  </header>

  <ConsoleFilterBar
    kind={filterState.kind}
    status={filterState.status}
    leaseId={filterState.leaseId}
    onChange={(next) => { filterState = next; }}
  />

  {#if loading}
    <div class="state" role="status" aria-live="polite">Loading workbench spine records.</div>
  {:else if error}
    <div class="state error" role="alert">{error}</div>
  {:else}
    <section class="asset-strip" aria-label="Workbench assets">
      {#each assets as asset (asset.asset_id)}
        <AssetCard {asset} />
      {/each}
    </section>

    <RunResultPanel run={selectedRun} trace={selectedTrace} {projectId} />

    <div class="console-grid">
      <RunTable {runs} {selectedRunId} onSelect={(runId) => { selectedRunId = runId; }} />
      <RunDetailPane run={selectedRun}>
        {#if selectedRun}
          {#snippet hardware()}
            <HardwareFitBadge run={selectedRun} />
          {/snippet}
          {#snippet policy()}
            <PolicyBadge run={selectedRun} />
          {/snippet}
          {#snippet provenance()}
            <ProvenanceBadge record={selectedRun} onClick={(record) => { drawerRecord = record; }} />
          {/snippet}
        {/if}
      </RunDetailPane>
      <TraceWaterfall {traces} />
    </div>
  {/if}

  <ArtifactDrawer open={drawerRecord !== null} record={drawerRecord} {projectId} onClose={() => { drawerRecord = null; }} />
</div>

<style>
  .workbench-console { padding: 18px; max-width: 1440px; display: flex; flex-direction: column; gap: 14px; }
  .console-header { display: flex; justify-content: space-between; gap: 14px; align-items: flex-start; flex-wrap: wrap; }
  h2, p { margin: 0; }
  h2 { font-size: 1.25rem; color: var(--text-primary); }
  p { color: var(--text-muted); font-family: var(--font-mono); font-size: 0.82rem; margin-top: 3px; }
  .asset-strip { display: flex; gap: 10px; overflow-x: auto; padding-bottom: 4px; }
  .console-grid { display: grid; grid-template-columns: minmax(440px, 1.4fr) minmax(280px, 0.8fr) minmax(320px, 1fr); gap: 12px; align-items: start; }
  .state { padding: 32px; border: 1px solid var(--border-default); border-radius: 8px; color: var(--text-muted); background: var(--surface-elevated); }
  .error { color: var(--danger); }
  @media (max-width: 1100px) { .console-grid { grid-template-columns: 1fr; } }
</style>
