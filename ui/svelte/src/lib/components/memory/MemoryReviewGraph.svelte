<script>
  import { getWorkbenchMemoryReviewGraph } from '$lib/api.js';
  import { showToast } from '$lib/stores/toast.svelte.js';
  import { unitPercent } from '$lib/utils/safe.js';

  let { projectId = 'default' } = $props();

  let graph = $state(null);
  let loading = $state(true);
  let error = $state(null);
  let selectedQueue = $state('all');
  let minConfidence = $state(0.5);
  let includeQuarantined = $state(false);

  const queueLabels = {
    all: 'All',
    stale: 'Stale',
    conflict: 'Conflict',
    quarantine: 'Quarantine',
    export_blocked: 'Export blocked',
  };

  let visibleNodes = $derived(graph?.nodes ?? []);
  let visibleEdges = $derived(graph?.edges ?? []);

  async function loadGraph() {
    loading = true;
    error = null;
    try {
      graph = await getWorkbenchMemoryReviewGraph({
        project_id: projectId,
        queue: selectedQueue,
        min_confidence: minConfidence,
        include_quarantined: includeQuarantined,
      });
    } catch (err) {
      error = err.message ?? String(err);
      graph = null;
      showToast(`Memory review graph unavailable: ${error}`, 'error');
    } finally {
      loading = false;
    }
  }

  $effect(() => {
    projectId;
    selectedQueue;
    minConfidence;
    includeQuarantined;
    loadGraph();
  });
</script>

<section class="memory-review-graph" aria-label="Memory review graph">
  <header class="review-header">
    <div>
      <h2>Memory Review Graph</h2>
      <p>{projectId}</p>
    </div>
    <div class="review-controls" aria-label="Memory review filters">
      <select bind:value={selectedQueue} aria-label="Review queue">
        {#each Object.entries(queueLabels) as [value, label]}
          <option {value}>{label}</option>
        {/each}
      </select>
      <label>
        <span>Confidence</span>
        <input
          type="range"
          min="0"
          max="1"
          step="0.05"
          bind:value={minConfidence}
          aria-label="Minimum memory confidence"
          aria-valuetext={`${Math.round(Number(minConfidence) * 100)} percent minimum confidence`}
        />
      </label>
      <label class="checkbox">
        <input type="checkbox" bind:checked={includeQuarantined} aria-label="Include quarantined memories" />
        <span>Quarantined</span>
      </label>
    </div>
  </header>

  {#if loading}
    <div class="state">Loading verified memory lineage.</div>
  {:else if error}
    <div class="state error">
      <strong>Review graph blocked</strong>
      <span>{error}</span>
    </div>
  {:else if graph}
    <div class="queue-strip" aria-label="Memory review queues">
      {#each Object.entries(graph.queues) as [queue, ids]}
        <button
          class:active={selectedQueue === queue}
          onclick={() => { selectedQueue = queue; }}
          aria-label={`Show ${queueLabels[queue] ?? queue} memory queue with ${ids.length} items`}
        >
          <span>{queueLabels[queue] ?? queue}</span>
          <strong>{ids.length}</strong>
        </button>
      {/each}
    </div>

    <div class="graph-grid">
      <section class="node-list" aria-label="Memory nodes">
        {#each visibleNodes as node (node.memory_id)}
          <article class="memory-node" class:blocked={node.export_boundary.allowed !== true || node.quarantined}>
            <div class="node-title">
              <h3>{node.label}</h3>
              <span>{unitPercent(node.confidence, 0)}%</span>
            </div>
            <p>{node.why_memory_exists}</p>
            <div class="tags">
              <span>{node.authority_tier}</span>
              {#if node.stale}<span>stale</span>{/if}
              {#if node.conflicts.length}<span>conflict</span>{/if}
              {#if node.quarantined}<span>quarantine</span>{/if}
              {#if node.export_boundary.allowed !== true}<span>export blocked</span>{/if}
            </div>
            <div class="node-panels">
              <section>
                <h4>Why recalled</h4>
                {#each node.why_recalled as row}
                  <p>{row.reason}</p>
                {/each}
              </section>
              <section>
                <h4>Where used</h4>
                {#each node.where_used as row}
                  <p>{row.run_id}: {row.outcome}</p>
                {/each}
              </section>
              <section>
                <h4>Actions</h4>
                <p>{node.actions.join(', ')}</p>
              </section>
            </div>
          </article>
        {/each}
      </section>

      <section class="edge-list" aria-label="Memory review edges">
        <h3>Lineage Edges</h3>
        {#each visibleEdges as edge (edge.edge_id)}
          <div class="edge-row">
            <span>{edge.kind}</span>
            <strong>{edge.source}</strong>
            <span>{edge.target}</span>
          </div>
        {/each}
      </section>
    </div>
  {/if}
</section>

<style>
  .memory-review-graph { padding: 18px; display: flex; flex-direction: column; gap: 14px; max-width: 1440px; }
  .review-header { display: flex; justify-content: space-between; gap: 14px; align-items: flex-start; flex-wrap: wrap; }
  h2, h3, h4, p { margin: 0; }
  h2 { font-size: 1.25rem; color: var(--text-primary); }
  h3 { font-size: 1rem; color: var(--text-primary); }
  h4 { font-size: 0.78rem; color: var(--text-muted); text-transform: uppercase; }
  p { color: var(--text-secondary); font-size: 0.86rem; line-height: 1.45; }
  .review-header p { color: var(--text-muted); font-family: var(--font-mono); font-size: 0.82rem; margin-top: 3px; }
  .review-controls { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
  .review-controls select, .review-controls input[type='range'] { min-height: 44px; accent-color: var(--accent, #4f9cf9); }
  .review-controls label { display: flex; align-items: center; gap: 8px; color: var(--text-muted); font-size: 0.82rem; }
  .checkbox { min-height: 44px; padding: 6px 8px; border: 1px solid var(--border-default); border-radius: 6px; }
  .checkbox input { min-height: 20px; width: 20px; }
  .state { padding: 32px; border: 1px solid var(--border-default); border-radius: 8px; color: var(--text-muted); background: var(--surface-elevated); }
  .state.error { color: var(--danger); display: flex; flex-direction: column; gap: 6px; }
  .queue-strip { display: flex; gap: 8px; overflow-x: auto; }
  .queue-strip button { display: flex; gap: 8px; align-items: center; min-height: 44px; border: 1px solid var(--border-default); background: var(--surface-elevated); color: var(--text-primary); border-radius: 6px; padding: 8px 10px; cursor: pointer; }
  .queue-strip button.active { border-color: var(--accent, #4f9cf9); }
  .graph-grid { display: grid; grid-template-columns: minmax(520px, 1.4fr) minmax(280px, 0.7fr); gap: 12px; align-items: start; }
  .node-list, .edge-list { display: flex; flex-direction: column; gap: 10px; }
  .memory-node, .edge-list { border: 1px solid var(--border-default); border-radius: 8px; background: var(--surface-elevated); padding: 12px; }
  .memory-node.blocked { border-color: var(--warning, #f0b429); }
  .node-title { display: flex; justify-content: space-between; gap: 12px; align-items: baseline; margin-bottom: 8px; }
  .node-title span { font-family: var(--font-mono); color: var(--text-muted); font-size: 0.82rem; }
  .tags { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 10px; }
  .tags span { font-size: 0.74rem; color: var(--text-muted); border: 1px solid var(--border-default); border-radius: 999px; padding: 3px 7px; }
  .node-panels { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 10px; margin-top: 12px; }
  .node-panels section { border-top: 1px solid var(--border-default); padding-top: 8px; display: flex; flex-direction: column; gap: 5px; }
  .edge-row { display: grid; grid-template-columns: 120px 1fr 1fr; gap: 8px; padding: 8px 0; border-top: 1px solid var(--border-default); font-size: 0.82rem; color: var(--text-secondary); }
  .edge-row strong { color: var(--text-primary); font-weight: 600; overflow-wrap: anywhere; }
  @media (max-width: 1100px) { .graph-grid, .node-panels { grid-template-columns: 1fr; } }
</style>
