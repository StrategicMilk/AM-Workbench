<script>
  import SensitiveWorkflowDecisionCard from './SensitiveWorkflowDecisionCard.svelte';
  import * as api from '$lib/api.js';
  import { ReadinessState, RigorRequired } from '$lib/contracts';
  import { requireEvidence } from '$lib/evidence/evidenceGuard.js';

  let { projectId = 'default', onDecision = null, nowProvider = () => new Date() } = $props();

  const domains = [
    'tax',
    'finance',
    'legal',
    'medical',
    'employment',
    'housing',
    'safety',
    'household_planning',
    'document_organization',
    'meeting_prep',
    'purchase_decision',
    'appointment',
    'general_professional',
  ];
  const outcomes = [
    'checklist',
    'document_packet',
    'professional_memo',
    'source_backed_note',
    'reminder',
    'evidence_notebook_entry',
    'questions_for_professional',
    'organize_documents',
    'explain_concept',
  ];

  let activeLensId = $state('professional_assistance');
  let sensitiveDomain = $state('tax');
  let jurisdiction = $state('');
  let taxYear = $state(null);
  let authorityRef = $state('');
  let evidenceText = $state('');
  let documentText = $state('');
  let claimKind = $state('');
  let workflowOutcome = $state('organize_documents');
  let lastDecision = $state(null);
  let submitError = $state('');
  let runtimeBinding = $state(null);
  let runtimeBindingState = $state('loading');

  let evidenceRefs = $derived(evidenceText.split(',').map((item) => item.trim()).filter(Boolean));
  let documentRefs = $derived(documentText.split(',').map((item) => item.trim()).filter(Boolean));
  let canSubmit = $derived(
    Boolean(activeLensId && sensitiveDomain && projectId && runtimeBinding?.available) && runtimeBindingState !== 'loading',
  );
  let requiresJurisdiction = $derived(['tax', 'finance', 'legal', 'medical', 'employment', 'housing', 'safety'].includes(sensitiveDomain));
  let requiresTaxYear = $derived(sensitiveDomain === 'tax');
  let noRuntimeBindingAvailable = $derived(!(runtimeBinding && runtimeBinding.available));
  let jurisdictionHelpId = $derived(requiresJurisdiction ? 'sensitive-workflow-jurisdiction-help' : undefined);
  let taxYearHelpId = $derived(requiresTaxYear ? 'sensitive-workflow-tax-year-help' : undefined);

  function nowIsoString() {
    const value = nowProvider();
    return typeof value === 'string' ? value : value.toISOString();
  }

  function validateDecisionEvidence(missingContext) {
    const refs = [
      ...evidenceRefs,
      ...documentRefs,
      authorityRef.trim(),
      runtimeBinding?.policy_explanation_ref,
      runtimeBinding?.decision_id ?? runtimeBinding?.binding_id,
    ].filter(Boolean);
    if (refs.length === 0) {
      missingContext.push('source_backed_evidence_required');
      return;
    }
    try {
      requireEvidence(refs, `sensitive-workflow:${projectId}:${sensitiveDomain}`);
    } catch (error) {
      missingContext.push(`invalid_evidence_refs:${error.message}`);
    }
  }

  $effect(() => {
    let cancelled = false;
    api.getSensitiveWorkflowBinding(projectId)
      .then((result) => {
        if (cancelled) return;
        runtimeBinding = result;
        runtimeBindingState = result?.available ? 'available' : ReadinessState.BLOCKED;
      })
      .catch((error) => {
        if (!cancelled) {
          runtimeBinding = null;
          runtimeBindingState = `blocked:${error?.message ?? 'runtime_binding_unavailable'}`;
        }
      });
    return () => {
      cancelled = true;
    };
  });

  function buildLocalDecision(kind, missingContext = []) {
    return {
      record_kind: 'decision',
      decision_id: runtimeBinding?.decision_id ?? runtimeBinding?.binding_id ?? '',
      request_correlation_id: `${projectId}-${sensitiveDomain}`,
      allowed: missingContext.length === 0,
      decision_kind: kind,
      reasons: missingContext.length === 0 ? ['preview requirements satisfied'] : [],
      denial_reasons: [],
      missing_context: missingContext,
      degraded: noRuntimeBindingAvailable || missingContext.length > 0,
      evidence_refs: evidenceRefs,
      document_refs: documentRefs,
      authority_ref: authorityRef.trim(),
      jurisdiction: jurisdiction.trim(),
      tax_year: taxYear,
      workflow_outcome: workflowOutcome,
      rigor_required: requiresJurisdiction ? RigorRequired.CHECK_IT_CAREFULLY : RigorRequired.HELP_ME_THINK,
      mode_lens_id: activeLensId,
      policy_explanation_ref: runtimeBinding?.policy_explanation_ref ?? '',
      runtime_binding_state: runtimeBindingState,
      decided_at_utc: nowIsoString(),
    };
  }

  function submitDecision() {
    if (!canSubmit) return;
    submitError = '';
    const missing = [];
    if (noRuntimeBindingAvailable) missing.push('runtime_binding_required');
    if (!runtimeBinding?.policy_explanation_ref) missing.push('policy_explanation_required');
    if (!(runtimeBinding?.decision_id ?? runtimeBinding?.binding_id)) missing.push('decision_binding_required');
    if (requiresJurisdiction && jurisdiction.trim().length === 0) missing.push('jurisdiction_required');
    if (requiresTaxYear && !taxYear) missing.push('tax_year_required');
    if (requiresJurisdiction && authorityRef.trim().length === 0) missing.push('authority_required');
    if (requiresJurisdiction && evidenceRefs.length === 0) missing.push('evidence_required');
    validateDecisionEvidence(missing);
    submitError = missing.length ? `Missing required context: ${missing.join(', ')}` : '';
    lastDecision = buildLocalDecision(missing.length ? 'denied_missing_context' : 'allowed', missing);
    if (onDecision) onDecision(lastDecision);
  }
</script>

<section class="sensitive-workflow-panel" aria-label="Sensitive workflow panel" data-project-id={projectId}>
  <header>
    <div>
      <h2>Professional Life Admin</h2>
      <p>{projectId}</p>
    </div>
    <span class="binding" data-runtime-binding={runtimeBindingState} role="status" aria-live="polite">{runtimeBindingState}</span>
  </header>

  <div class="form-grid">
    <div class="field">
      <label for="sensitive-workflow-mode-lens">Mode lens</label>
      <select id="sensitive-workflow-mode-lens" bind:value={activeLensId}>
        <option value="professional_assistance">Professional assistance</option>
        <option value="life_admin">Life admin</option>
        <option value="research">Research</option>
      </select>
    </div>
    <div class="field">
      <label for="sensitive-workflow-domain">Sensitive domain</label>
      <select id="sensitive-workflow-domain" bind:value={sensitiveDomain}>
        {#each domains as domain}
          <option value={domain}>{domain.replaceAll('_', ' ')}</option>
        {/each}
      </select>
    </div>
    <div class="field">
      <label for="sensitive-workflow-outcome">Workflow outcome</label>
      <select id="sensitive-workflow-outcome" bind:value={workflowOutcome}>
        {#each outcomes as outcome}
          <option value={outcome}>{outcome.replaceAll('_', ' ')}</option>
        {/each}
      </select>
    </div>
    <div class="field">
      <label for="sensitive-workflow-jurisdiction">Jurisdiction</label>
      <input
        id="sensitive-workflow-jurisdiction"
        bind:value={jurisdiction}
        aria-invalid={requiresJurisdiction && jurisdiction.trim().length === 0}
        aria-describedby={jurisdictionHelpId}
      />
      {#if requiresJurisdiction && jurisdiction.trim().length === 0}
        <small id="sensitive-workflow-jurisdiction-help">Jurisdiction is required for this domain.</small>
      {/if}
    </div>
    <div class="field">
      <label for="sensitive-workflow-tax-year">Tax year</label>
      <input
        id="sensitive-workflow-tax-year"
        type="number"
        bind:value={taxYear}
        aria-invalid={requiresTaxYear && !taxYear}
        aria-describedby={taxYearHelpId}
      />
      {#if requiresTaxYear && !taxYear}
        <small id="sensitive-workflow-tax-year-help">Tax year is required for this domain.</small>
      {/if}
    </div>
    <div class="field">
      <label for="sensitive-workflow-authority-ref">Authority ref</label>
      <input id="sensitive-workflow-authority-ref" bind:value={authorityRef} />
    </div>
    <div class="field">
      <label for="sensitive-workflow-evidence-refs">Evidence refs</label>
      <input id="sensitive-workflow-evidence-refs" bind:value={evidenceText} placeholder="source-1, source-2" />
    </div>
    <div class="field">
      <label for="sensitive-workflow-document-refs">Document refs</label>
      <input id="sensitive-workflow-document-refs" bind:value={documentText} placeholder="doc-1, doc-2" />
    </div>
    <div class="field">
      <label for="sensitive-workflow-claim-kind">Claim kind</label>
      <input id="sensitive-workflow-claim-kind" bind:value={claimKind} placeholder="optional" />
    </div>
  </div>

  <button type="button" onclick={submitDecision} disabled={!canSubmit} aria-disabled={!canSubmit}>Submit decision</button>
  {#if submitError}
    <div class="error" role="alert">{submitError}</div>
  {/if}

  <SensitiveWorkflowDecisionCard decision={lastDecision} />
</section>

<style>
  .sensitive-workflow-panel {
    display: grid;
    gap: 14px;
    max-width: 1180px;
    padding: 18px;
    color: var(--text-primary, #111827);
  }

  header {
    display: flex;
    justify-content: space-between;
    gap: 12px;
    align-items: flex-start;
  }

  h2,
  p {
    margin: 0;
  }

  p,
  small {
    color: var(--text-secondary, #4b5563);
  }

  .binding {
    border: 1px solid var(--border-color, #d1d5db);
    border-radius: 6px;
    padding: 5px 8px;
    color: #b7791f;
    background: rgba(245, 158, 11, 0.16);
  }

  .form-grid {
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    gap: 10px;
  }

  .field {
    display: grid;
    gap: 5px;
    font-size: 0.84rem;
    color: var(--text-secondary, #4b5563);
  }

  input,
  select,
  button {
    min-height: 34px;
    border: 1px solid var(--border-color, #d1d5db);
    border-radius: 6px;
    background: var(--surface-primary, #fff);
    color: var(--text-primary, #111827);
    padding: 4px 8px;
  }

  button {
    width: fit-content;
    cursor: pointer;
  }

  button:disabled {
    cursor: not-allowed;
    opacity: 0.55;
  }

  .error {
    color: #d9480f;
  }

  @media (max-width: 860px) {
    header,
    .form-grid {
      grid-template-columns: 1fr;
      display: grid;
    }
  }
</style>
