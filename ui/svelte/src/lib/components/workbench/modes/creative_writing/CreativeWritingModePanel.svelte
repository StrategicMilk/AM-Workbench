<script>
  const { characterCount = 0, sceneBeatCount = 0, voiceScore = 0 } = $props();
</script>

<section class="creative-writing-mode-panel" aria-label="Creative writing mode">
  <div class="mode-title">
    <i class="fas fa-feather-pointed"></i>
    <span>Creative</span>
  </div>
  <dl>
    <div><dt>Cast</dt><dd>{characterCount}</dd></div>
    <div><dt>Beats</dt><dd>{sceneBeatCount}</dd></div>
    <div><dt>Voice</dt><dd>{Math.round(voiceScore * 100)}%</dd></div>
  </dl>
</section>

<style>
  .creative-writing-mode-panel {
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
