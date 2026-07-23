<script>
  import { asArray, nonEmptyString } from '$lib/utils/safe.js';

  const {
    reviews = [],
    harnessResults = [],
    evalDatasets = [],
    selectedRisk = 'all',
    onInspect = undefined,
  } = $props();

  let activeRisk = $state(selectedRisk);
  const safeHarnessResults = $derived(asArray(harnessResults));
  const safeEvalDatasets = $derived(asArray(evalDatasets));

  const normalizedReviews = $derived(
    asArray(reviews).map((review) => ({
      ...review,
      review_id: nonEmptyString(review?.review_id, review?.media_asset_id ?? 'unknown-review'),
      risk_flags: asArray(review?.risk_flags),
      annotation_refs: asArray(review?.annotation_refs),
      redaction_refs: asArray(review?.redaction_refs),
      scene_refs: asArray(review?.scene_refs),
    })),
  );

  const visibleReviews = $derived(
    activeRisk === 'all'
      ? normalizedReviews
      : normalizedReviews.filter((review) => review.risk_flags.includes(activeRisk)),
  );

  const riskCounts = $derived(
    (() => {
      const riskMap = new Map();
      for (const review of normalizedReviews) {
        for (const risk of review.risk_flags) {
          riskMap.set(risk, (riskMap.get(risk) ?? 0) + 1);
        }
      }
      return [...riskMap.entries()].map(([risk, count]) => ({ risk, count }));
    })(),
  );

  function inspect(kind, payload) {
    if (typeof onInspect === 'function') {
      onInspect({ kind, payload });
    }
  }

  function selectRisk(risk) {
    activeRisk = risk;
  }
</script>

<section class="multimodal-workbench" aria-label="Multimodal media workbench">
  <div class="workbench-toolbar" role="tablist" aria-label="Media review risk filter">
    <button type="button" role="tab" aria-selected={activeRisk === 'all'} tabindex={activeRisk === 'all' ? 0 : -1} class:active={activeRisk === 'all'} onclick={() => selectRisk('all')}>All</button>
    {#each riskCounts as item (item.risk)}
      <button type="button" role="tab" aria-selected={activeRisk === item.risk} tabindex={activeRisk === item.risk ? 0 : -1} class:active={activeRisk === item.risk} onclick={() => selectRisk(item.risk)}>
        {item.risk} <span>{item.count}</span>
      </button>
    {/each}
  </div>

  <dl class="summary-strip" aria-label="Workbench summary">
    <div>
      <dt>Reviews</dt>
      <dd>{normalizedReviews.length}</dd>
    </div>
    <div>
      <dt>Harnesses</dt>
      <dd>{safeHarnessResults.length}</dd>
    </div>
    <div>
      <dt>Datasets</dt>
      <dd>{safeEvalDatasets.length}</dd>
    </div>
  </dl>

  {#if visibleReviews.length === 0}
    <p class="empty-state" role="status">No media reviews match this filter.</p>
  {:else}
    <div class="review-grid">
      {#each visibleReviews as review (review.review_id ?? review.media_asset_id)}
        <article class:risky={review.risk_flags.length > 0} class="review-row">
          <div class="review-main">
            <h3>{review.review_id}</h3>
            <p>{review.media_asset_id ?? review.media_asset?.asset_id}</p>
          </div>
          <dl class="review-facts">
            <div>
              <dt>Annotations</dt>
              <dd>{review.annotation_refs.length}</dd>
            </div>
            <div>
              <dt>Redactions</dt>
              <dd>{review.redaction_refs.length}</dd>
            </div>
            <div>
              <dt>Scenes</dt>
              <dd>{review.scene_refs.length}</dd>
            </div>
          </dl>
          <button type="button" class="inspect-button" onclick={() => inspect('review', review)}>Inspect</button>
        </article>
      {/each}
    </div>
  {/if}

  <div class="lower-grid">
    <section aria-label="Voice harness results">
      <h3>Voice Harness</h3>
      {#each safeHarnessResults as result (result.harness_id)}
        <button type="button" class="metric-row" onclick={() => inspect('harness', result)}>
          <span>{result.harness_id}</span>
          <strong>{result.metrics?.max_agent_latency_ms ?? 'n/a'} ms</strong>
        </button>
      {/each}
    </section>

    <section aria-label="Multimodal eval datasets">
      <h3>Eval Datasets</h3>
      {#each safeEvalDatasets as dataset (dataset.dataset_id)}
        <button type="button" class="metric-row" onclick={() => inspect('dataset', dataset)}>
          <span>{dataset.dataset_id}</span>
          <strong>{dataset.cases?.length ?? 0} cases</strong>
        </button>
      {/each}
    </section>
  </div>
</section>

<style>
  .multimodal-workbench {
    width: 100%;
  }

  .workbench-toolbar {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    margin-bottom: 10px;
  }

  .workbench-toolbar button,
  .inspect-button,
  .metric-row {
    border: 1px solid var(--border-default, #d6d9de);
    border-radius: 6px;
    background: var(--surface-secondary, #f8fafc);
    color: var(--text-primary, #111827);
    font: inherit;
    cursor: pointer;
  }

  .workbench-toolbar button {
    min-height: 32px;
    font-size: 0.75rem;
    font-weight: 700;
  }

  .workbench-toolbar button.active {
    border-color: var(--primary, #2563eb);
    background: var(--primary-soft, rgba(37, 99, 235, 0.12));
  }

  .workbench-toolbar span {
    margin-left: 4px;
    color: var(--text-muted, #6b7280);
  }

  .summary-strip {
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    gap: 8px;
    margin-bottom: 10px;
  }

  .summary-strip div,
  .lower-grid section {
    padding: 10px;
    border: 1px solid var(--border-default, #d6d9de);
    border-radius: 6px;
    background: var(--surface-primary, #fff);
  }

  .summary-strip dt {
    color: var(--text-muted, #6b7280);
    font-size: 0.6875rem;
  }

  .summary-strip dd {
    margin: 2px 0 0;
    color: var(--text-primary, #111827);
    font-size: 1rem;
    font-weight: 700;
  }

  .review-grid {
    display: grid;
    gap: 8px;
  }

  .review-row {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto 88px;
    align-items: center;
    gap: 12px;
    min-height: 78px;
    padding: 12px;
    border: 1px solid var(--border-default, #d6d9de);
    border-radius: 6px;
    background: var(--surface-primary, #fff);
  }

  .review-row.risky {
    border-color: var(--warning, #b45309);
    box-shadow: inset 3px 0 0 var(--warning, #b45309);
  }

  .review-main {
    min-width: 0;
  }

  .review-main h3,
  .lower-grid h3 {
    margin: 0 0 4px;
    color: var(--text-primary, #111827);
    font-size: 0.9375rem;
    font-weight: 700;
  }

  .review-main p {
    margin: 0;
    color: var(--text-muted, #4b5563);
    font-size: 0.8125rem;
    overflow-wrap: anywhere;
  }

  .review-facts {
    display: grid;
    grid-template-columns: repeat(3, minmax(60px, auto));
    gap: 8px;
    margin: 0;
  }

  .review-facts dt {
    color: var(--text-muted, #6b7280);
    font-size: 0.6875rem;
  }

  .review-facts dd {
    margin: 2px 0 0;
    color: var(--text-primary, #111827);
    font-size: 0.8125rem;
    font-weight: 700;
  }

  .inspect-button {
    width: 88px;
    min-height: 36px;
    font-size: 0.8125rem;
    font-weight: 700;
  }

  .lower-grid {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 8px;
    margin-top: 10px;
  }

  .metric-row {
    display: flex;
    justify-content: space-between;
    width: 100%;
    min-height: 34px;
    margin-top: 6px;
    padding: 8px;
    font-size: 0.8125rem;
    text-align: left;
  }

  .metric-row span {
    overflow-wrap: anywhere;
  }

  .empty-state {
    margin: 0;
    padding: 12px;
    border: 1px solid var(--border-default, #d6d9de);
    border-radius: 6px;
    color: var(--text-muted, #4b5563);
    font-size: 0.875rem;
  }

  .workbench-toolbar button:focus-visible,
  .inspect-button:focus-visible,
  .metric-row:focus-visible {
    border-color: var(--primary, #2563eb);
    outline: 2px solid var(--primary-soft, rgba(37, 99, 235, 0.22));
    outline-offset: 2px;
  }

  @media (max-width: 720px) {
    .review-row,
    .lower-grid,
    .summary-strip {
      grid-template-columns: 1fr;
    }

    .review-facts {
      grid-template-columns: repeat(3, minmax(0, 1fr));
    }

    .inspect-button {
      width: 100%;
    }
  }
</style>
