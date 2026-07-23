<script>
  import { asArray, nonEmptyString, unitPercent } from '$lib/utils/safe.js';

  let { evidence = [] } = $props();

  function evidenceRows() {
    return asArray(evidence);
  }

  function blockers(item) {
    return asArray(item?.blockers);
  }
</script>

<section class="evidence-list" aria-label="Friction evidence">
  <h2>Evidence</h2>
  <div class="rows">
    {#each evidenceRows() as item}
      <article
        class:blocked={item?.status !== 'accepted' || blockers(item).length > 0}
        aria-label={`Evidence ${nonEmptyString(item?.summary, 'untitled')}: ${nonEmptyString(item?.status, 'unknown status')}${blockers(item).length > 0 ? ', blocked' : ''}`}
      >
        <div>
          <strong>{nonEmptyString(item?.summary, 'Untitled evidence')}</strong>
          <span>{nonEmptyString(item?.kind, 'unknown kind')} - {nonEmptyString(item?.scope?.surface, 'unscoped')}</span>
        </div>
        <div class="meta">
          <span>{unitPercent(item?.confidence, 0)}%</span>
          <span>{nonEmptyString(item?.provenance_ref, 'missing provenance')}</span>
          <span>{nonEmptyString(item?.observed_at_utc, 'missing time')}</span>
        </div>
        {#if blockers(item).length > 0}
          <p>{blockers(item).join(', ')}</p>
        {/if}
      </article>
    {:else}
      <article class="blocked" aria-label="No accepted evidence">
        <div>
          <strong>No accepted evidence</strong>
          <span>blocked</span>
        </div>
      </article>
    {/each}
  </div>
</section>

<style>
  .evidence-list {
    display: grid;
    gap: 10px;
  }

  h2,
  p {
    margin: 0;
    letter-spacing: 0;
  }

  h2 {
    font-size: 15px;
  }

  .rows,
  article,
  .meta {
    display: grid;
    gap: 8px;
  }

  article {
    border: 1px solid var(--border-default, #334155);
    border-radius: 8px;
    padding: 10px;
    background: var(--surface-elevated, #111827);
    min-width: 0;
  }

  article.blocked {
    border-color: #f59e0b;
  }

  strong,
  span,
  p {
    overflow-wrap: anywhere;
  }

  strong {
    font-size: 13px;
  }

  span,
  p {
    color: var(--text-muted, #94a3b8);
    font-size: 12px;
  }

  .meta {
    grid-template-columns: repeat(3, minmax(0, 1fr));
  }

  @media (max-width: 760px) {
    .meta {
      grid-template-columns: 1fr;
    }
  }
</style>
