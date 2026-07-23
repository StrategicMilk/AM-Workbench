<script>
  import * as api from '$lib/api.js';
  import { MigrationRisk, ReadinessState } from '$lib/contracts';
  import { requireEvidence } from '$lib/evidence/evidenceGuard.js';

  let { projectId = 'default' } = $props();

  let queueSource = $state('loading');
  let queueError = $state('');

  const dimensions = [
    'Source accuracy',
    'Tone',
    'Policy compliance',
    'Citation sufficiency',
    'Risk rating',
    'Usefulness',
    'Corrected output',
  ];

  const defaultQueues = [
    { label: 'Ready', count: 12 },
    { label: 'Gold', count: 3 },
    { label: 'Drift', count: 2 },
    { label: 'Blocked', count: 4 },
  ];
  let queues = $state(defaultQueues.map((queue) => ({ ...queue, count: 0, degraded: true })));
  let workItems = $state([]);

  const artifacts = [
    'Eval case',
    'Preference draft',
    'Method card',
    'Source/tool card',
    'Diagnosis label',
    'Training candidate',
  ];

  function normalizeWorkItem(item) {
    const evidenceRefs = Array.isArray(item?.evidence_refs) ? item.evidence_refs : [];
    const provenanceRefs = Array.isArray(item?.provenance_refs) ? item.provenance_refs : [];
    try {
      if (evidenceRefs.length === 0) {
        throw new Error('missing_evidence_refs');
      }
      requireEvidence([...evidenceRefs, ...provenanceRefs], `domain-review:${item?.id ?? item?.title ?? 'work-item'}`);
      return { ...item, evidenceBlocked: false, evidenceBlockReason: '' };
    } catch (error) {
      return { ...item, risk: 'blocked', evidenceBlocked: true, evidenceBlockReason: error.message };
    }
  }

  $effect(() => {
    let cancelled = false;
    api.getDomainReviewQueues(projectId)
      .then((result) => {
        if (cancelled) return;
        const nextQueues = Array.isArray(result?.queues) ? result.queues : [];
        if (nextQueues.length === 0) {
          queueSource = ReadinessState.BLOCKED;
          queueError = 'domain_review_queues_empty';
          queues = defaultQueues.map((queue) => ({ ...queue, count: 0, degraded: true }));
          return;
        }
        queues = nextQueues.map((queue) => ({
          label: String(queue.label ?? queue.name ?? queue.id ?? 'Unknown'),
          count: Number(queue.count ?? queue.items_count ?? 0),
          degraded: false,
        }));
        workItems = Array.isArray(result?.work_items ?? result?.workItems)
          ? (result.work_items ?? result.workItems).map(normalizeWorkItem)
          : [];
        queueSource = 'api';
        queueError = '';
      })
      .catch((error) => {
        if (!cancelled) {
          queueSource = ReadinessState.BLOCKED;
          queueError = `domain_review_queues_unavailable:${error?.message ?? 'unknown'}`;
          queues = defaultQueues.map((queue) => ({ ...queue, count: 0, degraded: true }));
          workItems = [];
        }
      });
    return () => {
      cancelled = true;
    };
  });
</script>

<section class="domain-review-shell" aria-labelledby="domain-review-title">
  <header class="domain-review-header">
    <div>
      <p class="eyebrow">Workbench</p>
      <h1 id="domain-review-title">Domain Review</h1>
    </div>
    <span class="project-pill">{projectId}</span>
    <span class="source-pill">{queueSource}</span>
  </header>
  {#if queueError}
    <p class="queue-error">{queueError}</p>
  {/if}

  <div class="queue-strip" aria-label="Review queues">
    {#each queues as queue}
      <div class="queue-tile" data-degraded={queue.degraded} aria-label={`${queue.label} review queue has ${queue.count} items`}>
        <span>{queue.label}</span>
        <strong>{queue.count}</strong>
      </div>
    {/each}
  </div>

  <div class="review-layout">
    <section class="review-worklist" aria-label="Review worklist">
      {#if workItems.length > 0}
        {#each workItems as item, index}
          <div class="work-row" class:selected={index === 0}>
            <div>
              <strong>{item.title ?? item.label ?? item.id ?? 'Review item'}</strong>
              <span>{item.evidenceBlockReason || item.summary || item.description || 'API review item awaiting scoring.'}</span>
            </div>
            <span class:risk={true} class:high={(item.risk ?? '').toLowerCase() === MigrationRisk.HIGH} class:medium={(item.risk ?? '').toLowerCase() !== MigrationRisk.HIGH}>{item.risk ?? 'unknown'}</span>
          </div>
        {/each}
      {:else}
        <div class="work-row selected">
          <div>
            <strong>No API work items available</strong>
            <span>Review worklist is blocked until the domain review route returns work_items.</span>
          </div>
          <span class="risk medium">blocked</span>
        </div>
      {/if}
    </section>

    <section class="review-panel" aria-label="Review task">
      <div class="context-block">
        <span class="context-label">Minimum Context</span>
        <p>Output excerpt, source excerpt, provenance, citations, confidence, safety state, and policy refs.</p>
      </div>

      <div class="dimension-grid" aria-label="Rubric dimensions">
        {#each dimensions as dimension}
          <label>
            <span>{dimension}</span>
            <input type="number" min="1" max="5" value="3" aria-label={`${dimension} score`} />
          </label>
        {/each}
      </div>

      <textarea aria-label="Corrected output" rows="5">Corrected output drafted by reviewer.</textarea>

      <div class="artifact-row" aria-label="Consent-gated correction artifacts">
        {#each artifacts as artifact}
          <label>
            <input type="checkbox" aria-label={`Attach ${artifact}`} />
            <span>{artifact}</span>
          </label>
        {/each}
      </div>
    </section>
  </div>
</section>

<style>
  .domain-review-shell {
    display: flex;
    flex-direction: column;
    gap: 18px;
    padding: 24px;
    color: var(--text-primary);
  }

  .domain-review-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 16px;
  }

  .eyebrow {
    margin: 0 0 4px;
    color: var(--text-muted);
    font-size: 12px;
    text-transform: uppercase;
  }

  h1 {
    margin: 0;
    font-size: 28px;
    font-weight: 700;
  }

  .project-pill,
  .source-pill {
    border: 1px solid var(--border-default);
    border-radius: 999px;
    padding: 6px 10px;
    color: var(--text-secondary);
    font-size: 13px;
  }

  .queue-strip {
    display: grid;
    grid-template-columns: repeat(4, minmax(0, 1fr));
    gap: 10px;
  }

  .queue-tile {
    border: 1px solid var(--border-default);
    border-radius: 8px;
    padding: 12px;
    background: var(--surface-elevated);
  }

  .queue-tile[data-degraded='true'],
  .queue-error {
    border-color: var(--warning);
    color: var(--warning);
  }

  .queue-tile span {
    display: block;
    color: var(--text-muted);
    font-size: 13px;
  }

  .queue-tile strong {
    display: block;
    margin-top: 6px;
    font-size: 24px;
  }

  .review-layout {
    display: grid;
    grid-template-columns: minmax(260px, 0.8fr) minmax(420px, 1.4fr);
    gap: 14px;
    align-items: start;
  }

  .review-worklist,
  .review-panel {
    border: 1px solid var(--border-default);
    border-radius: 8px;
    background: var(--surface-elevated);
  }

  .work-row {
    display: flex;
    justify-content: space-between;
    gap: 12px;
    padding: 14px;
    border-bottom: 1px solid var(--border-default);
  }

  .work-row:last-child {
    border-bottom: 0;
  }

  .work-row.selected {
    box-shadow: inset 3px 0 0 var(--accent-primary);
  }

  .work-row span {
    display: block;
    margin-top: 5px;
    color: var(--text-muted);
    font-size: 13px;
    line-height: 1.35;
  }

  .risk {
    align-self: start;
    border-radius: 999px;
    padding: 3px 8px;
    font-size: 12px;
  }

  .risk.high {
    background: var(--danger-muted);
    color: var(--danger);
  }

  .risk.medium {
    background: var(--warning-muted);
    color: var(--warning);
  }

  .review-panel {
    display: flex;
    flex-direction: column;
    gap: 14px;
    padding: 16px;
  }

  .context-block {
    border: 1px solid var(--border-default);
    border-radius: 8px;
    padding: 12px;
  }

  .context-label {
    color: var(--text-muted);
    font-size: 12px;
    text-transform: uppercase;
  }

  .context-block p {
    margin: 6px 0 0;
    line-height: 1.45;
  }

  .dimension-grid {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 10px;
  }

  .dimension-grid label,
  .artifact-row label {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    font-size: 13px;
  }

  .dimension-grid input {
    min-height: 44px;
    width: 54px;
  }

  .artifact-row input {
    min-height: 20px;
    width: 20px;
  }

  textarea,
  input {
    border: 1px solid var(--border-default);
    border-radius: 6px;
    background: var(--surface-default);
    color: var(--text-primary);
  }

  textarea {
    width: 100%;
    min-height: 120px;
    padding: 10px;
    resize: vertical;
  }

  .artifact-row {
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    gap: 8px 12px;
  }

  @media (max-width: 900px) {
    .queue-strip,
    .review-layout,
    .dimension-grid,
    .artifact-row {
      grid-template-columns: 1fr;
    }
  }
</style>
