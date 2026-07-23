<script>
  let { results = [], selectedDomain = 'all', onSelect = () => {} } = $props();
  let visible = $derived(
    selectedDomain === 'all'
      ? results
      : results.filter((result) => result.domain === selectedDomain)
  );
</script>

<section class="health-results" aria-label="Health results">
  <div class="result-grid heading">
    <span>Domain</span>
    <span>State</span>
    <span>Summary</span>
    <span>Action</span>
  </div>
  {#each visible as result}
    <button class="result-grid row" data-state={result.state} onclick={() => onSelect(result)}>
      <span>{result.domain}</span>
      <strong>{result.state}</strong>
      <span>{result.summary}</span>
      <span>{result.settings_target || result.fix_action || 'informational'}</span>
    </button>
  {:else}
    <div class="empty">No health results for this domain.</div>
  {/each}
</section>

<style>
  .health-results {
    border: 1px solid var(--border-default);
    border-radius: 6px;
    background: var(--surface-elevated, #111827);
    overflow: hidden;
  }
  .result-grid {
    display: grid;
    grid-template-columns: minmax(120px, 0.7fr) minmax(120px, 0.5fr) minmax(240px, 1.4fr) minmax(160px, 0.8fr);
    gap: 10px;
    align-items: center;
    width: 100%;
    min-height: 44px;
    padding: 9px 12px;
  }
  .heading {
    color: var(--text-muted);
    font-size: 12px;
    text-transform: uppercase;
    border-bottom: 1px solid var(--border-default);
  }
  .row {
    border: 0;
    border-bottom: 1px solid var(--border-default);
    background: transparent;
    color: var(--text-primary);
    font: inherit;
    text-align: left;
    cursor: pointer;
  }
  .row:hover {
    background: var(--glass-bg, rgba(255, 255, 255, 0.05));
  }
  .row span, .row strong {
    overflow-wrap: anywhere;
  }
  .row[data-state="broken"] strong { color: #ff8a8a; }
  .row[data-state="approval_required"] strong { color: #d7b0ff; }
  .row[data-state="configured"] strong { color: #7bd88f; }
  .row[data-state="degraded"] strong, .row[data-state="stale"] strong, .row[data-state="busy"] strong { color: #ffd166; }
  .empty {
    padding: 16px;
    color: var(--text-muted);
  }
  @media (max-width: 900px) {
    .result-grid { grid-template-columns: 1fr; }
    .heading { display: none; }
  }
</style>
