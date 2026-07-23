<script>
  let { gates = [] } = $props();
  let failedCount = $derived(gates.filter((gate) => !gate.passed).length);
</script>

<section class="gate-list" data-testid="launcher-health-gates" aria-label="Launcher health gates">
  <header>
    <h2>Health Gates</h2>
    <span role="status" aria-live="polite">{failedCount} blocked</span>
  </header>
  <div class="rows">
    {#each gates as gate}
      <article class="gate" data-testid={`launcher-gate-${gate.name}`}>
        <span class="dot" class:passed={gate.passed} aria-hidden="true"></span>
        <div>
          <h3>{gate.name}</h3>
          <p>{gate.passed ? 'Ready.' : gate.remediation || gate.blockers?.join(', ') || 'Not ready.'}</p>
        </div>
      </article>
    {:else}
      <p class="empty" role="status">No health gates reported.</p>
    {/each}
  </div>
</section>

<style>
  .gate-list {
    display: grid;
    gap: 10px;
  }

  header,
  .gate {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
  }

  h2,
  h3,
  p {
    margin: 0;
  }

  h2 {
    font-size: 1rem;
  }

  .rows {
    display: grid;
    gap: 8px;
  }

  .gate,
  .empty {
    justify-content: flex-start;
    border: 1px solid var(--border-default, #334155);
    border-radius: 8px;
    padding: 10px;
  }

  .dot {
    width: 10px;
    height: 10px;
    border-radius: 50%;
    background: #f59e0b;
  }

  .dot.passed {
    background: #22c55e;
  }

  p {
    color: var(--text-muted, #94a3b8);
    font-size: 0.84rem;
  }
</style>
