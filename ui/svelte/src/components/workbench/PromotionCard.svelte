<script>
  import PromotionDecisionForm from './PromotionDecisionForm.svelte';
  import PromotionGateBadge from './PromotionGateBadge.svelte';
  import EvidenceTraceLink from './promotion_inbox/EvidenceTraceLink.svelte';

  // Props use $props(); do not replace with legacy export let in Svelte 5.
  let { proposal, projectId = 'default', disabled = false, onDecide = null } = $props();
  const MAX_FEEDBACK_REASONS = 8;
  const MAX_ASSETS = 12;
  const MAX_BLOCKERS = 20;

  function boundedArray(values, maxItems) {
    if (!Array.isArray(values)) return [];
    return values.slice(0, maxItems);
  }

  let evidence = $derived(proposal?.gate_evidence ?? {});
  let feedbackReasons = $derived(boundedArray(evidence.plan_feedback_reasons, MAX_FEEDBACK_REASONS));
  let evidenceRunId = $derived(evidence.source_run_id ?? evidence.run_id ?? evidence.evidence_run_id ?? '');
  let evidenceTraceId = $derived(evidence.source_trace_id ?? evidence.trace_id ?? evidence.evidence_trace_id ?? '');
  let evidenceAssetId = $derived(evidence.asset_id ?? proposal?.affected_assets?.[0] ?? '');
  let canOpenEvidenceTrace = $derived(Boolean(evidenceRunId && evidenceTraceId && evidenceAssetId && proposal?.proposal_id));
  let modelEvidence = $derived(
    evidence.model_id ?? evidence.model ?? evidence.selected_model ?? proposal?.model_id ?? ''
  );
  let evalMatched = $derived(evidence.eval_count_matched);
  let evalFailing = $derived(evidence.eval_count_failing);
  let memoryEvidence = $derived(
    evidence.memory_asset_id ??
      evidence.memory_trace_id ??
      evidence.memory_id ??
      evidence.recalled_memory_ids?.[0] ??
      ''
  );
  let requiredPromotionSignals = $derived([
    {
      id: 'model',
      label: 'Model',
      value: modelEvidence,
      present: hasEvidenceValue(modelEvidence),
      detail: modelEvidence || 'missing model evidence',
    },
    {
      id: 'eval',
      label: 'Eval',
      value: evalMatched,
      present: isFiniteCount(evalMatched) && Number(evalMatched) > 0 && Number(evalFailing ?? 0) === 0,
      detail: isFiniteCount(evalMatched)
        ? `${evalMatched} matched / ${evalFailing ?? 0} failing`
        : 'missing eval evidence',
    },
    {
      id: 'memory',
      label: 'Memory',
      value: memoryEvidence,
      present: hasEvidenceValue(memoryEvidence),
      detail: memoryEvidence || 'missing memory evidence',
    },
  ]);
  let missingPromotionSignals = $derived(requiredPromotionSignals.filter((signal) => !signal.present));
  let effectiveGateBlockers = $derived([
    ...boundedArray(proposal?.gate_blockers, MAX_BLOCKERS),
    ...missingPromotionSignals.map((signal) => `promotion_signal_missing:${signal.id}`),
  ]);
  let effectiveGatePassed = $derived(Boolean(proposal?.gate_passed) && missingPromotionSignals.length === 0);

  function hasEvidenceValue(value) {
    if (Array.isArray(value)) {
      return value.length > 0;
    }
    return value !== null && value !== undefined && String(value).trim().length > 0;
  }

  function isFiniteCount(value) {
    return value !== null && value !== undefined && Number.isFinite(Number(value));
  }

  function decide(event) {
    onDecide?.({
      proposal_id: proposal.proposal_id,
      accepted: event.accepted,
      decided_by: event.decided_by,
      rationale: event.rationale,
    });
  }
</script>

<article class:blocked={!effectiveGatePassed} class="promotion-card">
  <header>
    <div>
      <h3>{proposal.proposal_id}</h3>
      <p>{proposal.kind} · {proposal.status}</p>
    </div>
    <PromotionGateBadge passed={effectiveGatePassed} blockers={effectiveGateBlockers} />
  </header>

  {#if effectiveGateBlockers.includes('plan_feedback_refused')}
    <div class="feedback-reasons" aria-label="Plan feedback reasons">
      {#each feedbackReasons as reason}
        <span>{reason}</span>
      {/each}
    </div>
  {/if}

  <dl class="signal-grid" aria-label="Promotion evidence signals">
    {#each requiredPromotionSignals as signal}
      <div data-signal={signal.id} data-state={signal.present ? 'present' : 'missing'}>
        <dt>{signal.label}</dt>
        <dd>{signal.detail}</dd>
      </div>
    {/each}
  </dl>

  <dl class="evidence-grid">
    <div>
      <dt>Assets</dt>
      <dd>{boundedArray(proposal.affected_assets, MAX_ASSETS).join(', ')}</dd>
    </div>
    <div>
      <dt>Evals</dt>
      <dd>{evidence.eval_count_matched ?? 0} matched · {evidence.eval_count_failing ?? 0} failing</dd>
    </div>
    <div>
      <dt>Taints</dt>
      <dd>{evidence.taint_count ?? 0}</dd>
    </div>
    <div>
      <dt>Plan Feedback</dt>
      <dd>{evidence.plan_feedback_match_count ?? 0}</dd>
    </div>
  </dl>

  <footer>
    {#if canOpenEvidenceTrace}
      <EvidenceTraceLink
        {projectId}
        runId={evidenceRunId}
        traceId={evidenceTraceId}
        assetId={evidenceAssetId}
        proposalId={proposal.proposal_id}
      />
    {:else}
      <span class="no-evidence">No evidence trace target</span>
    {/if}
    <PromotionDecisionForm
      proposalId={proposal.proposal_id}
      gatePassed={effectiveGatePassed}
      {disabled}
      onDecision={decide}
    />
  </footer>
</article>

<style>
  .promotion-card {
    border: 1px solid #c7ced7;
    border-left: 4px solid #256f46;
    border-radius: 8px;
    display: grid;
    gap: 1rem;
    padding: 1rem;
  }

  .promotion-card.blocked {
    border-left-color: #d64533;
  }

  header {
    align-items: start;
    display: flex;
    gap: 1rem;
    justify-content: space-between;
  }

  h3,
  p,
  dl {
    margin: 0;
  }

  h3 {
    font-size: 1rem;
  }

  p {
    color: #5d6876;
    font-size: 0.85rem;
    margin-top: 0.2rem;
  }

  .evidence-grid {
    display: grid;
    gap: 0.75rem;
    grid-template-columns: repeat(auto-fit, minmax(9rem, 1fr));
  }

  .signal-grid {
    display: grid;
    gap: 0.5rem;
    grid-template-columns: repeat(auto-fit, minmax(10rem, 1fr));
  }

  .signal-grid > div {
    border: 1px solid #c7ced7;
    border-radius: 6px;
    padding: 0.55rem 0.65rem;
  }

  .signal-grid > div[data-state='missing'] {
    border-color: #d64533;
    background: #fff4f1;
  }

  dt {
    color: #5d6876;
    font-size: 0.75rem;
    font-weight: 700;
    text-transform: uppercase;
  }

  dd {
    margin: 0.2rem 0 0;
  }

  .feedback-reasons {
    display: flex;
    flex-wrap: wrap;
    gap: 0.4rem;
  }

  .feedback-reasons span {
    background: #edf2f7;
    border-radius: 6px;
    color: #354052;
    font-size: 0.78rem;
    font-weight: 700;
    padding: 0.25rem 0.45rem;
  }

  footer {
    display: grid;
    gap: 0.75rem;
  }

  .no-evidence {
    color: #68707c;
    font-size: 0.82rem;
    font-weight: 700;
  }
</style>
