<script>
  import { asArray, clampUnit, nonEmptyString } from '$lib/utils/safe.js';

  let { trace = null } = $props();

  let candidates = $derived(asArray(trace?.candidates));
  let rejected = $derived(asArray(trace?.rejected_candidates));
</script>

<section class="rag-trace" aria-label="Retrieval trace">
  <div class="section-head">
    <h3>Retrieval Trace</h3>
    <span>{trace?.collection ?? 'No collection'}</span>
  </div>

  <div class="trace-list">
    {#each candidates as candidate}
      <article class="trace-row">
        <div>
          <strong>{nonEmptyString(candidate?.chunk_id, 'unknown chunk')}</strong>
          <span>{nonEmptyString(candidate?.document_id, 'unknown document')}</span>
        </div>
        <meter min="0" max="1" value={clampUnit(candidate?.rerank_score, 0)}></meter>
        <p>{nonEmptyString(candidate?.text, 'No chunk text available.')}</p>
      </article>
    {:else}
      <p class="empty">No retrieved chunks.</p>
    {/each}
  </div>

  <h4>Rejected</h4>
  <div class="rejected-list">
    {#each rejected as candidate}
      <div class="rejected-row">
        <span>{nonEmptyString(candidate?.chunk_id, 'unknown chunk')}</span>
        <span>{nonEmptyString(candidate?.rejection_reason, 'unknown reason')}</span>
      </div>
    {:else}
      <p class="empty">No rejected chunks.</p>
    {/each}
  </div>
</section>

<style>
  .rag-trace,
  .trace-list,
  .rejected-list {
    display: grid;
    gap: 10px;
  }

  .section-head,
  .trace-row > div,
  .rejected-row {
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

  .trace-row {
    display: grid;
    gap: 6px;
    padding: 10px 0;
    border-bottom: 1px solid var(--border-default);
  }

  .trace-row p {
    color: var(--text-secondary);
    font-size: 0.875rem;
  }

  .rejected-row,
  .empty,
  .section-head span,
  .trace-row span {
    color: var(--text-secondary);
    font-size: 0.8125rem;
  }
</style>
