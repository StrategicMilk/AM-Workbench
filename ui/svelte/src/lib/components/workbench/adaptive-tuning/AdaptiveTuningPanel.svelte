<script>
  import AdaptiveTuningControls from './AdaptiveTuningControls.svelte';
  import FrictionEvidenceList from './FrictionEvidenceList.svelte';
  import { CardStatus } from '$lib/contracts/enums.js';
  import { asArray, nonEmptyString, unitPercent } from '$lib/utils/safe.js';

  let { projectId = 'default', snapshot = {}, onAction = () => {} } = $props();

  function hypotheses() {
    return asArray(snapshot?.hypotheses);
  }

  function controls() {
    return asArray(snapshot?.controls);
  }

  function activeCount() {
    return hypotheses().filter((item) => item?.status === CardStatus.ACTIVE).length;
  }

  function blockedCount() {
    return hypotheses().filter((item) => [
      CardStatus.BLOCKED,
      CardStatus.REJECTED,
      CardStatus.FORGOTTEN,
      CardStatus.DECAYED,
      CardStatus.REVOKED,
    ].includes(item?.status)).length;
  }
</script>

<main class="adaptive-tuning" aria-label="Workbench adaptive tuning" data-testid="workbench-adaptive-tuning">
  <header class="surface-header">
    <div>
      <h1>Adaptive Tuning</h1>
      <p>{projectId}</p>
    </div>
    <span class="policy-pill">reviewable</span>
  </header>

  <section class="metric-strip" aria-label="Adaptive tuning summary">
    <div><span>Hypotheses</span><strong>{hypotheses().length}</strong></div>
    <div><span>Active</span><strong>{activeCount()}</strong></div>
    <div><span>Blocked</span><strong>{blockedCount()}</strong></div>
    <div><span>Controls</span><strong>{controls().length}</strong></div>
  </section>

  <section class="hypothesis-grid" aria-label="Adaptive hypotheses">
    {#each hypotheses() as hypothesis}
      <article class="hypothesis" data-state={nonEmptyString(hypothesis?.status, 'unknown')}>
        <div class="hypothesis-head">
          <div>
            <h2>{nonEmptyString(hypothesis?.title, 'Untitled hypothesis')}</h2>
            <p>{nonEmptyString(hypothesis?.scope?.surface, 'unscoped')} · {unitPercent(hypothesis?.confidence, 0)}%</p>
          </div>
          <span class="status">{nonEmptyString(hypothesis?.status, 'unknown')}</span>
        </div>

        {#if hypothesis?.proposal}
          <div class="proposal">
            <strong>{hypothesis.proposal.title}</strong>
            <span>{hypothesis.proposal.risk_tier} · {hypothesis.proposal.target}</span>
            <span>{hypothesis.proposal.rollback?.required ? 'rollback required' : 'local only'}</span>
          </div>
        {:else}
          <div class="proposal blocked">
            <strong>No proposal</strong>
            <span>fail closed</span>
          </div>
        {/if}

        <FrictionEvidenceList evidence={hypothesis?.evidence ?? []} />
        <AdaptiveTuningControls
          hypothesisId={hypothesis?.hypothesis_id}
          state={hypothesis?.status}
          {onAction}
        />
      </article>
    {:else}
      <article class="hypothesis" data-state="blocked">
        <div class="hypothesis-head">
          <div>
            <h2>No active adaptive tuning</h2>
            <p>default</p>
          </div>
          <span class="status">blocked</span>
        </div>
      </article>
    {/each}
  </section>
</main>

<style>
  .adaptive-tuning {
    display: grid;
    gap: 14px;
    max-width: 1480px;
    padding: 16px;
    color: var(--text-primary, #e5e7eb);
  }

  .surface-header,
  .hypothesis-head {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 12px;
  }

  h1,
  h2,
  p {
    margin: 0;
    letter-spacing: 0;
  }

  h1 {
    font-size: 24px;
  }

  h2 {
    font-size: 15px;
  }

  p,
  span {
    color: var(--text-muted, #94a3b8);
    font-size: 12px;
  }

  .policy-pill,
  .status {
    border: 1px solid var(--border-default, #334155);
    border-radius: 8px;
    padding: 5px 8px;
    color: var(--text-primary, #e5e7eb);
    text-transform: capitalize;
  }

  .metric-strip,
  .hypothesis-grid {
    display: grid;
    gap: 10px;
  }

  .metric-strip {
    grid-template-columns: repeat(4, minmax(0, 1fr));
  }

  .metric-strip > div,
  .hypothesis,
  .proposal {
    border: 1px solid var(--border-default, #334155);
    border-radius: 8px;
    background: var(--surface-elevated, #111827);
    padding: 12px;
    min-width: 0;
  }

  .metric-strip > div,
  .hypothesis,
  .proposal {
    display: grid;
    gap: 8px;
  }

  .metric-strip strong {
    font-size: 22px;
  }

  .proposal.blocked,
  .hypothesis[data-state='blocked'],
  .hypothesis[data-state='rejected'],
  .hypothesis[data-state='forgotten'],
  .hypothesis[data-state='revoked'],
  .hypothesis[data-state='decayed'] {
    border-color: #f59e0b;
  }

  strong,
  span,
  p {
    overflow-wrap: anywhere;
  }

  @media (max-width: 760px) {
    .surface-header,
    .hypothesis-head {
      display: grid;
    }

    .metric-strip {
      grid-template-columns: 1fr;
    }
  }
</style>
