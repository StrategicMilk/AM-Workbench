<script>
  import JourneyLink from './JourneyLink.svelte';

  let { experiment = null, projectId = 'default' } = $props();

  let canPromote = $derived(Boolean(experiment?.experiment_id && experiment?.source_run_id && experiment?.source_trace_id));
</script>

<section class="experiment-winner-panel" aria-label="Experiment handoff">
  <div>
    <h3>Winning experiment</h3>
    <span>{experiment?.experiment_id ?? 'none'}</span>
  </div>
  <JourneyLink
    target="promotionInbox"
    {projectId}
    disabled={!canPromote}
    label="Promote"
    ariaLabel="Open winning experiment in Promotion Inbox"
    params={{
      experimentId: experiment?.experiment_id,
      runId: experiment?.source_run_id,
      traceId: experiment?.source_trace_id,
      assetId: experiment?.asset_id,
      assetRevision: experiment?.asset_revision,
      score: experiment?.score,
    }}
  />
</section>

<style>
  .experiment-winner-panel {
    align-items: center;
    border: 1px solid var(--color-border, #d8dde3);
    border-radius: 8px;
    background: var(--color-surface, #fff);
    display: flex;
    gap: 0.75rem;
    justify-content: space-between;
    padding: 0.85rem 1rem;
  }

  h3,
  span {
    margin: 0;
  }

  h3 {
    font-size: 0.92rem;
  }

  span {
    color: var(--color-text-muted, #68707c);
    font-size: 0.78rem;
    overflow-wrap: anywhere;
  }

  @media (max-width: 620px) {
    .experiment-winner-panel {
      align-items: stretch;
      flex-direction: column;
    }
  }
</style>
