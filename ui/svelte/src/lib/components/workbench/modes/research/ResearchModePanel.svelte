<script>
  const { claimCount = 0, contradictionCount = 0, freshnessStatus = 'pending' } = $props();
</script>

<section class="research-mode-panel" aria-label="Research mode">
  <div class="mode-title">
    <i class="fas fa-magnifying-glass-chart"></i>
    <span>Research</span>
  </div>
  <dl>
    <div><dt>Claims</dt><dd>{claimCount}</dd></div>
    <div><dt>Conflicts</dt><dd>{contradictionCount}</dd></div>
    <div><dt>Freshness</dt><dd>{freshnessStatus}</dd></div>
  </dl>
</section>

<style>
  .research-mode-panel {
    display: grid;
    gap: 8px;
    padding: 10px;
    border: 1px solid var(--border-default);
    border-radius: 6px;
    background: var(--surface-primary);
  }

  .mode-title {
    display: flex;
    align-items: center;
    gap: 8px;
    color: var(--text-primary);
    font-size: 0.8125rem;
    font-weight: 700;
  }

  dl {
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    gap: 6px;
    margin: 0;
  }

  dt {
    color: var(--text-muted);
    font-size: 0.6875rem;
  }

  dd {
    margin: 2px 0 0;
    color: var(--text-primary);
    font-size: 0.8125rem;
    font-weight: 700;
  }
</style>
