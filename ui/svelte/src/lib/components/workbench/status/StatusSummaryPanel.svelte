<script>
  let { snapshot = null } = $props();
  const states = ['configured', 'degraded', 'broken', 'busy', 'stale', 'approval_required'];
  let counts = $derived(snapshot && typeof snapshot.state_counts === 'object' && snapshot.state_counts ? snapshot.state_counts : {});
  let overall = $derived(snapshot?.overall_state ?? 'broken');
</script>

<section class="summary-panel" aria-label="Workbench status summary" data-state={overall} role={overall === 'configured' ? 'status' : 'alert'} aria-live="polite">
  <div>
    <span class="eyebrow">Overall</span>
    <strong>{overall}</strong>
  </div>
  <div class="state-counts">
    {#each states as state}
      <div class="state-count" data-state={state}>
        <span>{state}</span>
        <strong>{counts[state] ?? 0}</strong>
      </div>
    {/each}
  </div>
</section>

<style>
  .summary-panel {
    display: grid;
    grid-template-columns: minmax(160px, 0.4fr) minmax(420px, 1fr);
    gap: 12px;
    align-items: stretch;
    border: 1px solid var(--border-default);
    border-radius: 6px;
    padding: 12px;
    background: var(--surface-elevated, #111827);
  }
  .eyebrow {
    display: block;
    color: var(--text-muted);
    font-size: 12px;
    text-transform: uppercase;
  }
  strong {
    display: block;
    margin-top: 4px;
    font-size: 24px;
  }
  .state-counts {
    display: grid;
    grid-template-columns: repeat(6, minmax(92px, 1fr));
    gap: 8px;
  }
  .state-count {
    min-height: 64px;
    border: 1px solid var(--border-default);
    border-radius: 6px;
    padding: 8px;
  }
  .state-count span {
    display: block;
    color: var(--text-muted);
    font-size: 11px;
    overflow-wrap: anywhere;
  }
  .state-count strong {
    font-size: 20px;
  }
  [data-state="configured"] { border-color: #31a66a; }
  [data-state="degraded"], [data-state="stale"], [data-state="busy"] { border-color: #d6a821; }
  [data-state="approval_required"] { border-color: #b779ff; }
  [data-state="broken"] { border-color: #d44d4d; }
  @media (max-width: 980px) {
    .summary-panel { grid-template-columns: 1fr; }
    .state-counts { grid-template-columns: repeat(2, minmax(120px, 1fr)); }
  }
</style>
