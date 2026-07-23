<script>
  import { showToast } from '$lib/stores/toast.svelte.js';
  import { workbenchKernelRequest } from '$lib/api.js';
  import HelpPopover from '$lib/components/help/HelpPopover.svelte';
  import HelpTooltip from '$lib/components/help/HelpTooltip.svelte';
  import { assertNoPlaceholders } from '$lib/evidence/evidenceGuard.js';

  let { projectId = 'default' } = $props();

  function samplePayload(currentProjectId) {
    return {
      graph_id: 'ui-work-graph',
      priority_policy: 'score-components',
      sources: [
        { source_id: 'roadmap', provenance_ref: 'roadmap:ui', stale: false, readable: true },
        { source_id: 'automation', provenance_ref: 'automation:ui', stale: false, readable: true },
        { source_id: 'evals', provenance_ref: 'eval:ui', stale: false, readable: true },
        { source_id: 'runs', provenance_ref: 'run:ui', stale: false, readable: true },
      ],
      nodes: [
        {
          node_id: 'roadmap:work-graph',
          kind: 'roadmap_item',
          label: 'Work graph priority map',
          source_id: 'roadmap',
          provenance_refs: ['roadmap:ui'],
          stale_evidence_refs: [],
          metadata: { project_id: currentProjectId },
        },
        {
          node_id: 'automation:recipe',
          kind: 'automation_asset',
          label: 'Replay recipe',
          source_id: 'automation',
          provenance_refs: ['automation:ui'],
          stale_evidence_refs: [],
          metadata: {},
        },
        {
          node_id: 'eval:failure',
          kind: 'eval_failure',
          label: 'Regression fixture failure',
          source_id: 'evals',
          provenance_refs: ['eval:ui'],
          stale_evidence_refs: ['stale:prior-run'],
          metadata: {},
        },
        {
          node_id: 'run:kernel',
          kind: 'run_record',
          label: 'Run kernel record',
          source_id: 'runs',
          provenance_refs: ['run:ui'],
          stale_evidence_refs: [],
          metadata: {},
        },
      ],
      edges: [
        {
          edge_id: 'depends:eval-roadmap',
          source_node_id: 'eval:failure',
          target_node_id: 'roadmap:work-graph',
          kind: 'depends-on',
          source_id: 'evals',
          provenance_refs: ['eval:ui'],
        },
        {
          edge_id: 'replay:auto-eval',
          source_node_id: 'automation:recipe',
          target_node_id: 'eval:failure',
          kind: 'replayed-by-automation',
          source_id: 'automation',
          provenance_refs: ['automation:ui'],
        },
        {
          edge_id: 'run:produced',
          source_node_id: 'run:kernel',
          target_node_id: 'automation:recipe',
          kind: 'produced-by-run',
          source_id: 'runs',
          provenance_refs: ['run:ui'],
        },
      ],
    };
  }

  let snapshot = $state(null);
  let status = $state('idle');
  let reasons = $state([]);
  let loading = $state(false);
  let error = $state(null);

  let components = $derived(snapshot?.components ?? []);
  let scores = $derived(snapshot?.priority_scores ?? []);
  let nodesById = $derived(Object.fromEntries((snapshot?.nodes ?? []).map((node) => [node.node_id, node])));
  let blockers = $derived(status === 'blocked' ? reasons : []);

  function parseAcceptedRebuildBody(error) {
    const message = error instanceof Error ? error.message : String(error);
    if (!message.startsWith('409 ') && !message.startsWith('207 ')) {
      throw error;
    }
    const bodyStart = message.indexOf(': ');
    if (bodyStart === -1) {
      throw error;
    }
    return JSON.parse(message.slice(bodyStart + 2));
  }

  function applyGraphResponse(body, fallbackStatus = 'succeeded') {
    const graph = body?.snapshot ?? body?.graph ?? body?.payload?.snapshot ?? body?.payload?.graph ?? null;
    snapshot = graph
      ? {
          ...graph,
          components: graph.components ?? [],
          priority_scores: graph.priority_scores ?? [],
        }
      : null;
    status = body?.status ?? body?.payload?.status ?? (snapshot ? fallbackStatus : 'blocked');
    reasons = body?.reasons ?? body?.payload?.reasons ?? [];
  }

  async function loadGraph() {
    loading = true;
    error = null;
    try {
      const body = await workbenchKernelRequest(
        `/api/workbench/work-graph/snapshot?project_id=${encodeURIComponent(projectId)}`
      );
      applyGraphResponse(body, 'succeeded');
    } catch (err) {
      error = err.message ?? String(err);
      status = 'blocked';
      reasons = [error];
      showToast(`Work graph load failed: ${error}`, 'error');
    } finally {
      loading = false;
    }
  }

  async function rebuildGraph(payload) {
    loading = true;
    error = null;
    try {
      const rebuildPayload = payload ?? samplePayload(projectId);
      let body;
      try {
        assertNoPlaceholders(
          rebuildPayload,
          [
            'sources[].provenance_ref',
            'nodes[].provenance_refs[]',
            'nodes[].stale_evidence_refs[]',
            'edges[].provenance_refs[]',
          ],
          'work_graph.rebuild_payload',
        );
        body = await workbenchKernelRequest('/api/workbench/work-graph/rebuild', {
          method: 'POST',
          body: JSON.stringify(rebuildPayload),
        });
      } catch (err) {
        body = parseAcceptedRebuildBody(err);
      }
      applyGraphResponse(body, 'blocked');
      showToast('Work graph rebuilt.', status === 'blocked' ? 'warning' : 'success');
    } catch (err) {
      error = err.message ?? String(err);
      status = 'blocked';
      reasons = [error];
      showToast(`Work graph failed: ${error}`, 'error');
    } finally {
      loading = false;
    }
  }

  async function showStaleBlocker() {
    await rebuildGraph({
      ...samplePayload(projectId),
      sources: samplePayload(projectId).sources.map((source) =>
        source.source_id === 'evals' ? { ...source, stale: true } : source
      ),
    });
  }

  $effect(() => {
    void loadGraph();
  });
</script>

<main class="work-graph" aria-label="Workbench Work Graph">
  <header class="graph-header">
    <div>
      <h1>Work Graph</h1>
      <p>Priority-scored dependency graph for project {projectId}.</p>
      <HelpPopover
        title="Work graph"
        body="Nodes represent roadmap items, automation assets, eval failures, and run records. Dependency arcs show which nodes block others — a node is blocked when any upstream dependency is unresolved. Cluster view groups tightly connected nodes to reveal structural bottlenecks. Stale-source warning: if a source is marked stale, its dependent nodes may have outdated priority scores — use the Stale Gate button to surface affected nodes. Priority score cap: transitive fan-out, blocked downstream count, and stale evidence count are each capped to prevent outlier nodes from dominating the sort."
        severity="info"
      />
    </div>
    <div class="actions">
      <button type="button" onclick={() => rebuildGraph()} disabled={loading}>
        <i class="fas fa-rotate" aria-hidden="true"></i>
        <span>Rebuild</span>
      </button>
      <button type="button" onclick={showStaleBlocker} disabled={loading}>
        <i class="fas fa-triangle-exclamation" aria-hidden="true"></i>
        <span>Stale Gate</span>
      </button>
    </div>
  </header>

  {#if loading}
    <section class="state" role="status" aria-live="polite">Loading work graph.</section>
  {:else if error}
    <section class="state error" role="alert">{error}</section>
  {:else}
    <section class="status-strip" aria-label="Work graph status">
      <span data-status={status}>{status}</span>
      <span>{snapshot?.nodes?.length ?? 0} nodes</span>
      <span>{snapshot?.edges?.length ?? 0} edges</span>
      <span>{components.length} clusters</span>
    </section>

    {#if blockers.length}
      <section class="blockers" aria-label="Blocked source reasons">
        {#each blockers as reason}
          <span>{reason}</span>
        {/each}
      </section>
    {/if}

    <section class="graph-grid" aria-label="Work graph clusters and scores">
      <article class="clusters">
        <h2>Clusters</h2>
        {#each components as component}
          <div class="cluster">
            <strong>{component.component_id}</strong>
            <ul>
              {#each component.node_ids as nodeId}
                <li>{nodesById[nodeId]?.label ?? nodeId}</li>
              {/each}
            </ul>
          </div>
        {:else}
          <p>No clusters available.</p>
        {/each}
      </article>

      <article class="scores">
        <h2>Score Components</h2>
        <table>
          <thead>
            <tr>
              <th>Node</th>
              <th>Fan-out</th>
              <th>Blocked</th>
              <th>Stale</th>
              <th>Replay/Eval</th>
            </tr>
          </thead>
          <tbody>
            {#each scores as score}
              <tr>
                <td>{nodesById[score.node_id]?.label ?? score.node_id}</td>
                <td>{score.transitive_fanout}</td>
                <td>{score.blocked_downstream_count}</td>
                <td>{score.stale_evidence_count}</td>
                <td>{score.replay_eval_evidence_count}</td>
              </tr>
            {:else}
              <tr><td colspan="5">No score components.</td></tr>
            {/each}
          </tbody>
        </table>
      </article>
    </section>
  {/if}
</main>

<style>
  .work-graph {
    display: grid;
    gap: 16px;
    min-height: 100%;
    padding: 18px;
    background: var(--bg-primary, #0b1120);
    color: var(--text-primary, #e5e7eb);
  }

  .graph-header,
  .actions,
  .status-strip,
  .blockers {
    display: flex;
    flex-wrap: wrap;
    gap: 10px;
    align-items: center;
    justify-content: space-between;
  }

  h1,
  h2,
  p,
  ul {
    margin: 0;
  }

  h1 {
    font-size: 28px;
    letter-spacing: 0;
  }

  h2 {
    font-size: 15px;
    letter-spacing: 0;
  }

  p,
  th {
    color: var(--text-muted, #94a3b8);
  }

  button {
    display: inline-flex;
    gap: 8px;
    align-items: center;
    min-height: 44px;
    border: 1px solid var(--border-default, #334155);
    border-radius: 6px;
    padding: 8px 11px;
    background: var(--surface-elevated, #111827);
    color: inherit;
    cursor: pointer;
  }

  button:disabled {
    cursor: not-allowed;
    opacity: 0.55;
  }

  .state,
  .status-strip,
  .blockers,
  article {
    border: 1px solid var(--border-default, #334155);
    border-radius: 8px;
    background: var(--surface-elevated, #111827);
    padding: 12px;
  }

  .state.error,
  .blockers {
    border-color: #f97316;
    color: #fed7aa;
  }

  .status-strip {
    justify-content: flex-start;
  }

  .status-strip span,
  .blockers span {
    border-radius: 999px;
    background: rgba(148, 163, 184, 0.14);
    padding: 5px 9px;
    font-size: 13px;
  }

  .status-strip [data-status='succeeded'] {
    background: rgba(34, 197, 94, 0.16);
    color: #bbf7d0;
  }

  .graph-grid {
    display: grid;
    grid-template-columns: minmax(260px, 0.8fr) minmax(360px, 1.2fr);
    gap: 14px;
  }

  .clusters,
  .scores {
    display: grid;
    align-content: start;
    gap: 12px;
    min-width: 0;
  }

  .cluster {
    display: grid;
    gap: 7px;
    border-top: 1px solid var(--border-subtle, #1f2937);
    padding-top: 10px;
  }

  ul {
    display: grid;
    gap: 5px;
    padding-left: 18px;
  }

  table {
    width: 100%;
    border-collapse: collapse;
    table-layout: fixed;
  }

  th,
  td {
    border-top: 1px solid var(--border-subtle, #1f2937);
    padding: 8px 6px;
    text-align: left;
    overflow-wrap: anywhere;
    font-size: 13px;
  }

  @media (max-width: 860px) {
    .graph-grid {
      grid-template-columns: 1fr;
    }
  }
</style>
