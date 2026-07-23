<script>
  let {
    experiment = null,
    onReplay = () => {},
    onSave = () => {},
    onPromote = () => {},
    busy = false,
    error = '',
  } = $props();
</script>

<section class="rag-experiment-bar" aria-label="Experiment actions">
  <div>
    <strong>{experiment?.experiment_id ?? 'Unsaved experiment'}</strong>
    <span>{experiment?.revision_id ?? 'No revision'}</span>
  </div>

  <div class="actions">
    <button class="btn btn-secondary" type="button" onclick={onReplay} disabled={busy}>
      Replay
    </button>
    <button class="btn btn-primary" type="button" onclick={onSave} disabled={busy || !experiment}>
      Save
    </button>
    <button class="btn btn-secondary" type="button" onclick={onPromote} disabled={busy || !experiment}>
      Promote
    </button>
  </div>

  {#if error}
    <p role="alert">{error}</p>
  {/if}
</section>

<style>
  .rag-experiment-bar {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
    gap: 12px;
    align-items: center;
    padding: 12px 0;
    border-top: 1px solid var(--border-default);
  }

  .rag-experiment-bar > div:first-child {
    display: grid;
    gap: 4px;
  }

  .rag-experiment-bar span,
  p {
    color: var(--text-secondary);
    font-size: 0.8125rem;
    margin: 0;
  }

  .actions {
    display: flex;
    gap: 8px;
    flex-wrap: wrap;
    justify-content: flex-end;
  }

  p {
    grid-column: 1 / -1;
    color: var(--danger);
  }

  @media (max-width: 760px) {
    .rag-experiment-bar {
      grid-template-columns: 1fr;
    }

    .actions {
      justify-content: flex-start;
    }
  }
</style>
