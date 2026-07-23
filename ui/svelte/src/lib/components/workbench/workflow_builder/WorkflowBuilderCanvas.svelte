<script>
  import { normalizeWorkflowGraph, workflowReadinessEvidence } from './index.js';

  let { graph = {} } = $props();
  let graphState = $derived(normalizeWorkflowGraph(graph));
  let evidence = $derived(workflowReadinessEvidence(graphState));
  let safeGraph = $derived(graphState.graph);
  let steps = $derived(safeGraph.steps);
  let edges = $derived(safeGraph.edges);

  function displayLabel(value, fallback = 'Unknown') {
    if (!value) {
      return fallback;
    }
    return String(value)
      .replace(/[_-]+/g, ' ')
      .replace(/\s+/g, ' ')
      .trim()
      .replace(/\b\w/g, (letter) => letter.toUpperCase());
  }

  function stepKindLabel(kind) {
    return displayLabel(kind, 'Workflow step');
  }

  function safetyModeLabel(mode) {
    return displayLabel(mode, 'Safety mode unavailable');
  }
</script>

<section class="canvas" aria-label="Workflow graph" data-rcg0021-p05-state={graphState.ok ? 'ready' : 'blocked'}>
  {#if !graphState.ok}
    <div class="status-banner" role="alert">
      {graphState.issue}
    </div>
  {/if}
  {#each steps as step (step.step_id)}
    <article aria-label={`${step.label}: ${stepKindLabel(step.kind)}`}>
      <span>{stepKindLabel(step.kind)}</span>
      <h3>{step.label}</h3>
      <p>{step.summary ?? 'Workflow step is ready for review.'}</p>
    </article>
  {/each}
  <footer>
    {edges.length} links - {safetyModeLabel(safeGraph.safety_mode)}
    <span>{evidence.receipt_id}</span>
  </footer>
</section>

<style>
  .canvas {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
    gap: 10px;
  }

  article {
    min-height: 110px;
    border: 1px solid var(--border-default, #334155);
    border-radius: 8px;
    background: var(--surface-elevated, #111827);
    padding: 12px;
  }

  .status-banner {
    grid-column: 1 / -1;
    border: 1px solid #d44d4d;
    border-radius: 8px;
    padding: 10px 12px;
    color: var(--text-primary, #e5e7eb);
  }

  span {
    display: inline-block;
    margin-bottom: 10px;
    color: #93c5fd;
    font-size: 11px;
  }

  h3,
  p,
  footer {
    margin: 0;
    letter-spacing: 0;
  }

  h3 {
    font-size: 14px;
  }

  p,
  footer {
    color: var(--text-muted, #94a3b8);
    font-size: 12px;
  }

  footer {
    grid-column: 1 / -1;
    display: flex;
    gap: 8px;
    flex-wrap: wrap;
  }
</style>
