<script>
  import { onMount } from 'svelte';
  import { getWorkbenchGraphQuerySnapshot } from '$lib/api.js';
  import { GraphNodeKind, GraphQueryView, TaskStatus } from '$lib/contracts';
  import { requireEvidence } from '$lib/evidence/evidenceGuard.js';

  let { projectId = 'default' } = $props();

  let loading = $state(false);
  let error = $state('');
  let snapshot = $state(null);
  let selectedView = $state(GraphQueryView.FULL_CROSS_OBJECT_GRAPH);
  const requiredSavedViews = {
    stale_evidence_blocked_promotions: GraphQueryView.STALE_EVIDENCE_BLOCKED_PROMOTIONS,
    failure_shared_source_revision: GraphQueryView.FAILURE_SHARED_SOURCE_REVISION,
    route_cost_without_quality_gain: GraphQueryView.ROUTE_COST_WITHOUT_QUALITY_GAIN,
    automation_churn_without_adoption: GraphQueryView.AUTOMATION_CHURN_WITHOUT_ADOPTION,
  };

  const visibleNodes = $derived(
    snapshot?.nodes?.filter((node) => {
      if (selectedView === GraphQueryView.FULL_CROSS_OBJECT_GRAPH) return true;
      if (selectedView === requiredSavedViews.stale_evidence_blocked_promotions) {
        return node.kind === GraphNodeKind.PROPOSAL || node.kind === GraphNodeKind.EVAL;
      }
      if (selectedView === requiredSavedViews.failure_shared_source_revision) {
        return node.kind === GraphNodeKind.RUN || node.kind === GraphNodeKind.ASSET;
      }
      if (selectedView === requiredSavedViews.route_cost_without_quality_gain) {
        return node.kind === GraphNodeKind.RUN;
      }
      if (selectedView === requiredSavedViews.automation_churn_without_adoption) {
        return node.kind === GraphNodeKind.AUTOMATION;
      }
      return true;
    }) ?? []
  );

  const visibleNodeIds = $derived(new Set(visibleNodes.map((node) => node.node_id)));
  const visibleEdges = $derived(
    snapshot?.edges?.filter((edge) => visibleNodeIds.has(edge.source_id) && visibleNodeIds.has(edge.target_id)) ?? []
  );

  async function loadSnapshot() {
    loading = true;
    error = '';
    try {
      snapshot = await getWorkbenchGraphQuerySnapshot(projectId);
      if (snapshot?.authority_ref) {
        requireEvidence(snapshot.authority_ref, 'graph_query.authority_ref');
      }
      if (snapshot?.saved_views?.length && !snapshot.saved_views.some((view) => view.view_id === selectedView)) {
        selectedView = snapshot.saved_views[0].view_id;
      }
    } catch (err) {
      error = err instanceof Error ? err.message : String(err);
      snapshot = null;
    } finally {
      loading = false;
    }
  }

  onMount(loadSnapshot);
</script>

<section class="query-surface" aria-labelledby="graph-query-title">
  <header class="query-header">
    <div>
      <p class="eyebrow">Workbench</p>
      <h1 id="graph-query-title">Graph Query</h1>
    </div>
    <button class="refresh-button" type="button" onclick={loadSnapshot} disabled={loading}>
      <i class="fas fa-rotate"></i>
      <span>{loading ? 'Loading' : 'Refresh'}</span>
    </button>
  </header>

  <div class="query-toolbar">
    <label for="saved-view-select">Saved view</label>
    <select id="saved-view-select" bind:value={selectedView} disabled={!snapshot?.saved_views?.length}>
      {#each snapshot?.saved_views ?? [] as view}
        <option value={view.view_id}>{view.name}</option>
      {/each}
    </select>
  </div>

  {#if error}
    <div class="state-banner error" role="alert">{error}</div>
  {:else if loading}
    <div class="state-banner">Loading trusted graph snapshot...</div>
  {:else if snapshot}
    <div class="summary-strip">
      <div>
        <strong>{visibleNodes.length}</strong>
        <span>Objects</span>
      </div>
      <div>
        <strong>{visibleEdges.length}</strong>
        <span>Relationships</span>
      </div>
      <div>
        <strong>{snapshot.authority_ref}</strong>
        <span>Authority</span>
      </div>
    </div>

    {#if snapshot.diagnostics?.length}
      <div class="state-banner warning">
        {snapshot.diagnostics.join(' | ')}
      </div>
    {/if}

    <div class="query-grid">
      <section class="object-list" aria-label="Graph objects">
        {#each visibleNodes as node}
          <article class="object-row">
            <div>
              <span class="kind">{node.kind}</span>
              <h2>{node.label}</h2>
              <p>{node.node_id}</p>
            </div>
            <div class="object-meta">
              <span class:blocked={node.status === TaskStatus.BLOCKED || node.status === TaskStatus.FAILED}>{node.status || TaskStatus.ACTIVE}</span>
              <span>{Math.round((node.confidence ?? 0) * 100)}%</span>
            </div>
          </article>
        {/each}
      </section>

      <section class="edge-list" aria-label="Graph relationships">
        {#each visibleEdges as edge}
          <div class="edge-row">
            <span>{edge.source_id}</span>
            <strong>{edge.relation}</strong>
            <span>{edge.target_id}</span>
          </div>
        {/each}
        {#if visibleEdges.length === 0}
          <div class="state-banner">No relationships for this saved view.</div>
        {/if}
      </section>
    </div>
  {:else}
    <div class="state-banner">No graph snapshot loaded.</div>
  {/if}
</section>

<style>
  .query-surface {
    min-height: 100%;
    padding: 24px;
    color: var(--text-primary);
  }

  .query-header {
    display: flex;
    justify-content: space-between;
    gap: 16px;
    align-items: center;
    margin-bottom: 20px;
  }

  .eyebrow {
    margin: 0 0 4px;
    color: var(--text-muted);
    font-size: 12px;
    text-transform: uppercase;
    letter-spacing: 0;
  }

  h1 {
    margin: 0;
    font-size: 28px;
    font-weight: 650;
  }

  .refresh-button {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    border: 1px solid var(--border-default);
    background: var(--surface-elevated);
    color: var(--text-primary);
    border-radius: 8px;
    padding: 9px 12px;
    cursor: pointer;
  }

  .query-toolbar {
    display: flex;
    gap: 12px;
    align-items: center;
    margin-bottom: 16px;
  }

  .query-toolbar label {
    color: var(--text-muted);
    font-size: 13px;
  }

  .query-toolbar select {
    min-width: 280px;
    max-width: 100%;
    border: 1px solid var(--border-default);
    background: var(--surface-elevated);
    color: var(--text-primary);
    border-radius: 8px;
    padding: 8px 10px;
  }

  .summary-strip {
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    gap: 1px;
    border: 1px solid var(--border-default);
    margin-bottom: 16px;
  }

  .summary-strip div {
    background: var(--surface-elevated);
    padding: 14px;
    min-width: 0;
  }

  .summary-strip strong,
  .summary-strip span {
    display: block;
    overflow-wrap: anywhere;
  }

  .summary-strip span {
    color: var(--text-muted);
    font-size: 12px;
    margin-top: 4px;
  }

  .query-grid {
    display: grid;
    grid-template-columns: minmax(0, 1.1fr) minmax(280px, 0.9fr);
    gap: 16px;
    align-items: start;
  }

  .object-list,
  .edge-list {
    display: grid;
    gap: 8px;
  }

  .object-row,
  .edge-row {
    border: 1px solid var(--border-default);
    background: var(--surface-elevated);
  }

  .object-row {
    display: flex;
    justify-content: space-between;
    gap: 16px;
    padding: 12px;
  }

  .object-row h2 {
    margin: 4px 0;
    font-size: 16px;
  }

  .object-row p {
    margin: 0;
    color: var(--text-muted);
    font-size: 12px;
    overflow-wrap: anywhere;
  }

  .kind {
    color: var(--accent, #61dafb);
    font-size: 12px;
    text-transform: uppercase;
  }

  .object-meta {
    display: grid;
    gap: 6px;
    justify-items: end;
    color: var(--text-muted);
    font-size: 12px;
    min-width: 72px;
  }

  .object-meta .blocked {
    color: var(--danger, #ef4444);
  }

  .edge-row {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto minmax(0, 1fr);
    gap: 8px;
    padding: 10px;
    font-size: 12px;
  }

  .edge-row span {
    overflow-wrap: anywhere;
    color: var(--text-muted);
  }

  .state-banner {
    border: 1px solid var(--border-default);
    background: var(--surface-elevated);
    color: var(--text-muted);
    padding: 12px;
    margin-bottom: 12px;
  }

  .state-banner.error {
    border-color: var(--danger, #ef4444);
    color: var(--danger, #ef4444);
  }

  .state-banner.warning {
    border-color: var(--warning, #f59e0b);
    color: var(--warning, #f59e0b);
  }

  @media (max-width: 860px) {
    .query-surface {
      padding: 16px;
    }

    .query-header,
    .query-toolbar,
    .object-row {
      align-items: stretch;
      flex-direction: column;
    }

    .summary-strip,
    .query-grid {
      grid-template-columns: 1fr;
    }

    .edge-row {
      grid-template-columns: 1fr;
    }

    .object-meta {
      justify-items: start;
    }
  }
</style>
