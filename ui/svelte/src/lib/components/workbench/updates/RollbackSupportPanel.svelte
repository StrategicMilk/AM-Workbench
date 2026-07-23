<script>
  import { ReadinessState } from '$lib/contracts';
  import ProvenanceGate from '$lib/security/ProvenanceGate.svelte';
  import { provenanceDecision } from '$lib/security';

  let { readiness = null, rollbackPlan = null, busy = false, onRollbackPlan = () => {}, onSupportBundle = () => {} } = $props();
  let state = $derived(readiness?.state ?? ReadinessState.BLOCKED);
  let rollbackRefs = $derived([
    ...(Array.isArray(readiness?.evidence_refs) ? readiness.evidence_refs : []),
    readiness?.manifest?.integrity?.manifest_ref,
    readiness?.manifest?.integrity?.signature_ref,
    rollbackPlan?.rollback_plan?.artifact_digest,
    rollbackPlan?.rollback_plan?.approval_ref,
  ].filter(Boolean));
  let rollbackProvenance = $derived(provenanceDecision({
    evidence_refs: rollbackRefs,
    status: state === ReadinessState.READY ? 'verified' : state,
    allowed: state === ReadinessState.READY,
    reasons: readiness?.blocked_reasons,
  }, 'update-rollback'));
</script>

<section class="rollback-panel" aria-label="Rollback and support bundle" data-state={state}>
  <h3>Rollback Support</h3>
  <p>Rollback actions require approval and generate a support bundle before installation changes are applied.</p>
  <ProvenanceGate
    refs={rollbackRefs}
    status={state === ReadinessState.READY ? 'verified' : state}
    allowed={state === ReadinessState.READY}
    reasons={readiness?.blocked_reasons}
    context="update-rollback"
  />
  {#if rollbackPlan?.rollback_plan}
    <dl>
      <div><dt>prior</dt><dd>{rollbackPlan.rollback_plan.prior_version || 'unknown'}</dd></div>
      <div><dt>digest</dt><dd>{rollbackPlan.rollback_plan.artifact_digest || 'missing'}</dd></div>
      <div><dt>approval</dt><dd>{String(rollbackPlan.rollback_plan.requires_user_approval)}</dd></div>
    </dl>
  {:else}
    <p>Rollback remains proposal-only until an Approval Chain decision is supplied.</p>
  {/if}
  <div class="actions">
    <button type="button" onclick={onRollbackPlan} disabled={busy || !rollbackProvenance.trusted} aria-label="Prepare rollback plan">Rollback Plan</button>
    <button type="button" onclick={onSupportBundle} disabled={busy} aria-label="Create support bundle">Support Bundle</button>
  </div>
</section>

<style>
  .rollback-panel {
    border: 1px solid var(--border-default);
    border-radius: 6px;
    padding: 12px;
    background: var(--surface-elevated, #111827);
  }
  h3 {
    margin: 0 0 8px;
    font-size: 15px;
  }
  p, dd {
    color: var(--text-muted);
    overflow-wrap: anywhere;
  }
  dl {
    display: grid;
    gap: 7px;
    margin: 0 0 10px;
  }
  dl div {
    display: grid;
    grid-template-columns: 80px 1fr;
    gap: 8px;
  }
  dt {
    color: var(--text-muted);
  }
  dd {
    margin: 0;
  }
  .actions {
    display: flex;
    gap: 8px;
    flex-wrap: wrap;
  }
  button {
    min-height: 44px;
    border: 1px solid var(--border-default);
    border-radius: 6px;
    background: var(--surface-base, #0b1020);
    color: var(--text-primary);
    padding: 7px 10px;
  }
</style>
