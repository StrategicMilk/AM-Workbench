<script>
  import { asArray, clampUnit, nonEmptyString } from '$lib/utils/safe.js';

  let { breakdown = [] } = $props();

  let safeRows = $derived(
    asArray(breakdown).map((row) => {
      const pre = clampUnit(row?.pre_rerank_score, null);
      const post = clampUnit(row?.post_rerank_score, null);
      const reportedDelta = Number(row?.delta);
      const delta = Number.isFinite(reportedDelta) && pre !== null && post !== null
        ? Math.max(-1, Math.min(1, reportedDelta))
        : pre !== null && post !== null
          ? Number((post - pre).toFixed(3))
          : null;

      return {
        candidateId: nonEmptyString(row?.candidate_id, 'unknown chunk'),
        pre,
        post,
        delta,
        inconsistent:
          pre !== null &&
          post !== null &&
          delta !== null &&
          Math.abs(Number((post - pre).toFixed(3)) - delta) > 0.001,
      };
    }),
  );

  function scoreLabel(value) {
    return value === null ? 'missing' : value.toFixed(3);
  }
</script>

<section class="rag-rerank" aria-label="Rerank breakdown">
  <h3>Rerank</h3>
  <div class="breakdown-table" role="table" aria-label="Rerank score changes">
    <div class="header" role="row">
      <span role="columnheader">Chunk</span>
      <span role="columnheader">Before</span>
      <span role="columnheader">After</span>
      <span role="columnheader">Delta</span>
    </div>
    {#each safeRows as row}
      <div class="breakdown-row" role="row">
        <span role="cell">{row.candidateId}</span>
        <span role="cell" class:missing={row.pre === null}>{scoreLabel(row.pre)}</span>
        <span role="cell" class:missing={row.post === null}>{scoreLabel(row.post)}</span>
        <span role="cell" class:missing={row.delta === null} class:inconsistent={row.inconsistent}>
          {scoreLabel(row.delta)}
        </span>
      </div>
    {:else}
      <p>No rerank data.</p>
    {/each}
  </div>
</section>

<style>
  .rag-rerank {
    display: grid;
    gap: 10px;
  }

  h3,
  p {
    margin: 0;
  }

  h3 {
    font-size: 1rem;
  }

  .breakdown-table {
    display: grid;
    gap: 4px;
  }

  .header,
  .breakdown-row {
    display: grid;
    grid-template-columns: minmax(0, 1fr) 72px 72px 72px;
    gap: 8px;
    min-height: 32px;
    align-items: center;
    border-bottom: 1px solid var(--border-default);
    font-size: 0.8125rem;
  }

  .header {
    color: var(--text-secondary);
    font-weight: 600;
  }

  p {
    color: var(--text-secondary);
    font-size: 0.875rem;
  }

  .missing,
  .inconsistent {
    color: var(--warning);
  }
</style>
