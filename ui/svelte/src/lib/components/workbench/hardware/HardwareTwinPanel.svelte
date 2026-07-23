<script>
  import HardwareBenchmarkMatrix from './HardwareBenchmarkMatrix.svelte';
  import HardwareOptimizationProposalList from './HardwareOptimizationProposalList.svelte';

  let { snapshot = {}, drift = {}, proposals = [] } = $props();

  let observations = $derived(Array.isArray(snapshot.observations) ? snapshot.observations : []);
  let readyCount = $derived(observations.filter((item) => item.status === 'ready').length);
  let driftStatus = $derived(drift.rebenchmark_required ? 'rebenchmark' : drift.status ?? 'stable');
  let evidenceIds = $derived(Array.isArray(snapshot.evidence_ids) ? snapshot.evidence_ids : []);
</script>

<section class="hardware-twin" aria-label="Hardware digital twin">
  <header>
    <div>
      <h2>Hardware Twin</h2>
      <p>{snapshot.status ?? 'unavailable'} · {readyCount}/{observations.length} measured · {driftStatus}</p>
    </div>
    <span class:ready={snapshot.status === 'ready'} class:degraded={snapshot.status !== 'ready'}>
      {snapshot.project_id ?? 'project'}
    </span>
  </header>

  <dl class="headroom">
    {#each observations.slice(0, 4) as item}
      <div>
        <dt>{item.kind.replaceAll('_', ' ')}</dt>
        <dd>{item.value ?? item.status} {item.unit ?? ''}</dd>
        <dd class="evidence">{item.evidence_id}</dd>
      </div>
    {/each}
  </dl>

  <HardwareBenchmarkMatrix observations={observations} />
  <HardwareOptimizationProposalList {proposals} />

  <footer>
    {#each evidenceIds as evidenceId}
      <span>{evidenceId}</span>
    {/each}
  </footer>
</section>

<style>
  .hardware-twin {
    display: grid;
    gap: 14px;
    color: var(--text-default, #e5e7eb);
  }

  header {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 12px;
  }

  h2,
  p {
    margin: 0;
    letter-spacing: 0;
  }

  h2 {
    font-size: 18px;
  }

  p {
    color: var(--text-muted, #94a3b8);
    font-size: 12px;
  }

  header span,
  footer span {
    border: 1px solid var(--border-default, #334155);
    border-radius: 8px;
    padding: 5px 8px;
    font-size: 12px;
  }

  .ready {
    color: #86efac;
  }

  .degraded {
    color: #fbbf24;
  }

  .headroom {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
    gap: 10px;
    margin: 0;
  }

  .headroom div {
    min-height: 76px;
    border: 1px solid var(--border-default, #334155);
    border-radius: 8px;
    background: var(--surface-elevated, #111827);
    padding: 10px;
  }

  dt {
    margin-bottom: 5px;
    color: var(--text-muted, #94a3b8);
    font-size: 11px;
    text-transform: capitalize;
  }

  dd {
    margin: 0;
    font-size: 13px;
  }

  .evidence {
    margin-top: 4px;
    color: var(--text-muted, #94a3b8);
    overflow-wrap: anywhere;
    font-size: 11px;
  }

  footer {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
  }
</style>
