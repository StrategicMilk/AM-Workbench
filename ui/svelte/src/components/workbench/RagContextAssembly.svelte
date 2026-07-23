<script>
  let { context = null, verdict = null } = $props();

  let coverageRows = $derived(Object.entries(context?.source_coverage ?? {}));
  let hasContext = $derived(Boolean(
    context?.context_text
      || context?.token_count
      || (context?.included_chunk_ids ?? []).length
      || coverageRows.length
  ));
  let hasVerdict = $derived(verdict?.passed !== undefined || verdict?.faithfulness_score !== undefined);
  let verdictState = $derived(!hasVerdict && !hasContext ? 'empty' : verdict?.passed ? 'passed' : 'failed');
  let verdictLabel = $derived(verdictState === 'empty' ? 'No grounding verdict' : verdict?.passed ? 'Grounded' : 'Not grounded');
  let faithfulnessScore = $derived(Number(verdict?.faithfulness_score ?? 0));
</script>

<section class="rag-context" aria-label="Context assembly">
  <div class="section-head">
    <h3>Context Assembly</h3>
    <span>{context?.token_count ?? 0} tokens</span>
  </div>

  <pre>{context?.context_text ?? 'No assembled context.'}</pre>

  <div class="context-grid">
    <div>
      <h4>Included</h4>
      {#each context?.included_chunk_ids ?? [] as chunkId}
        <span class="pill">{chunkId}</span>
      {:else}
        <p>No chunks included.</p>
      {/each}
    </div>
    <div>
      <h4>Coverage</h4>
      {#each coverageRows as row}
        <span class="coverage-row">{row[0]}: {row[1]}</span>
      {:else}
        <p>No source coverage.</p>
      {/each}
    </div>
  </div>

  <div class:passed={verdictState === 'passed'} class:empty={verdictState === 'empty'} class="verdict">
    <strong>{verdictLabel}</strong>
    <span>faithfulness {faithfulnessScore.toFixed(2)}</span>
  </div>
</section>

<style>
  .rag-context {
    display: grid;
    gap: 12px;
  }

  .section-head,
  .verdict {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
    gap: 12px;
    align-items: center;
  }

  h3,
  h4,
  p {
    margin: 0;
  }

  h3 {
    font-size: 1rem;
  }

  h4 {
    font-size: 0.875rem;
  }

  pre {
    margin: 0;
    max-height: 260px;
    overflow: auto;
    white-space: pre-wrap;
    border: 1px solid var(--border-default);
    border-radius: 6px;
    padding: 12px;
    color: var(--text-secondary);
    background: var(--surface-secondary);
  }

  .context-grid {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 14px;
  }

  .pill,
  .coverage-row {
    display: block;
    margin-top: 6px;
    color: var(--text-secondary);
    font-size: 0.8125rem;
  }

  .verdict {
    color: var(--danger);
  }

  .verdict.passed {
    color: var(--success);
  }

  .verdict.empty {
    color: var(--text-muted);
  }

  @media (max-width: 760px) {
    .context-grid {
      grid-template-columns: 1fr;
    }
  }
</style>
