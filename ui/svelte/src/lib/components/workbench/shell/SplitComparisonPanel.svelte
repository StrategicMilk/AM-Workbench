<script>
  let { comparison = null, objects = [] } = $props();

  function findObject(id) {
    return objects.find((object) => object.object_id === id) ?? null;
  }

  let left = $derived(findObject(comparison?.left_object_id));
  let right = $derived(findObject(comparison?.right_object_id));
</script>

<section class="split-panel" class:degraded={comparison?.degraded} aria-label="Split comparison" data-testid="workbench-split-comparison">
  <header>
    <h2>Compare</h2>
    <span>{comparison?.basis || 'not ready'}</span>
  </header>
  {#if comparison?.degraded}
    <p class="degraded-note">{comparison.degraded_reason}</p>
  {/if}
  <div class="compare-grid">
    <article>
      <span>Left</span>
      <strong>{left?.title || 'No object'}</strong>
      <small>{left?.status || 'missing'}</small>
    </article>
    <article>
      <span>Right</span>
      <strong>{right?.title || 'No object'}</strong>
      <small>{right?.status || 'missing'}</small>
    </article>
  </div>
</section>

<style>
  .split-panel {
    border: 1px solid var(--border-default, #334155);
    border-radius: 8px;
    background: var(--surface-elevated, #111827);
    padding: 12px;
  }

  .split-panel.degraded {
    border-color: #f59e0b;
  }

  header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
  }

  h2,
  p {
    margin: 0;
  }

  h2 {
    font-size: 0.92rem;
  }

  header span,
  article span,
  small,
  .degraded-note {
    color: var(--text-muted, #94a3b8);
    font-size: 0.78rem;
  }

  .degraded-note {
    margin-top: 8px;
  }

  .compare-grid {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 8px;
    margin-top: 12px;
  }

  article {
    display: grid;
    gap: 4px;
    min-width: 0;
    border: 1px solid var(--border-default, #334155);
    border-radius: 6px;
    padding: 10px;
  }

  strong {
    color: var(--text-primary, #e5e7eb);
    min-height: 32px;
    overflow-wrap: anywhere;
  }
</style>
