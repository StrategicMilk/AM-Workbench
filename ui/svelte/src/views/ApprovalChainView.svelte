<script>
  import { resolveWorkbenchApprovalChain } from '$lib/api.js';
  import HelpPopover from '$lib/components/help/HelpPopover.svelte';
  import HelpTooltip from '$lib/components/help/HelpTooltip.svelte';
  import { assertNoPlaceholders, requireEvidence } from '$lib/evidence/evidenceGuard.js';

  let { projectId = 'default' } = $props();

  let form = $state({
    session_id: 'local-session',
    channel: 'desktop',
    action_id: 'preview-action',
    action_type: 'tool_invocation',
    actor_id: 'operator',
    run_id: 'approval-preview',
    risk_domain: 'tool_invocation',
    summary: 'Preview a non-destructive Workbench action',
    target_paths: '',
    action_fingerprint: 'preview-action-fingerprint',
    approval_sources: '',
    governance_mode: 'observe',
    governance_available: true,
  });
  let loading = $state(false);
  let error = $state('');
  let decision = $state(null);
  const previewFailClosedReceipt = 'rcg-0021-p05:approval-chain:preview-fail-closed';

  let orderedTrace = $derived(decision?.ordered_trace ?? []);
  let receipt = $derived(decision?.receipt_payload ?? null);
  let outcome = $derived(decision?.outcome ?? 'pending');
  let matchedStep = $derived(decision?.matched_step ?? 'pending');
  let fallbackRule = $derived(decision?.fallback_rule ?? 'pending');
  let approvalPreviewReceipt = $derived(receipt?.decision_id ?? previewFailClosedReceipt);
  function approvalEvidenceRefs(approvalSources) {
    return approvalSources
      .map((source) => source.includes(':') ? source : '')
      .filter(Boolean);
  }

  function readinessSignals(approvalSources) {
    const kinds = [
      'setup', 'identity', 'config', 'provider', 'capability_pack', 'policy',
      'scheduler', 'memory', 'connector', 'tool_pin', 'run_kernel', 'verified_history', 'probe',
    ];
    const signals = {};
    const evidenceRefs = approvalEvidenceRefs(approvalSources).map((ref) => ({ ref, kind: 'artifact' }));
    const refValues = evidenceRefs.map((entry) => entry.ref);
    const hasRefPrefix = (prefix) => refValues.some((ref) => ref.toLowerCase().startsWith(prefix));
    const fieldReady = (value) => typeof value === 'string' && value.trim().length > 0;
    const traceReady = orderedTrace.length > 0;
    const checks = {
      setup: { ready: form.governance_available, pending: [], blockers: form.governance_available ? [] : ['governance_unavailable'] },
      identity: { ready: fieldReady(form.actor_id), pending: fieldReady(form.actor_id) ? [] : ['missing_actor_id'], blockers: [] },
      config: { ready: fieldReady(form.governance_mode), pending: fieldReady(form.governance_mode) ? [] : ['missing_governance_mode'], blockers: [] },
      provider: { ready: fieldReady(form.channel), pending: fieldReady(form.channel) ? [] : ['missing_channel'], blockers: [] },
      capability_pack: { ready: fieldReady(form.risk_domain), pending: fieldReady(form.risk_domain) ? [] : ['missing_risk_domain'], blockers: [] },
      policy: { ready: form.governance_available && fieldReady(form.governance_mode), pending: [], blockers: form.governance_available ? [] : ['governance_unavailable'] },
      scheduler: { ready: fieldReady(form.session_id), pending: fieldReady(form.session_id) ? [] : ['missing_session_id'], blockers: [] },
      memory: { ready: approvalSources.length > 0, pending: approvalSources.length > 0 ? [] : ['missing_approval_sources'], blockers: [] },
      connector: { ready: evidenceRefs.length > 0, pending: evidenceRefs.length > 0 ? [] : ['missing_source_evidence_refs'], blockers: [] },
      tool_pin: { ready: fieldReady(form.action_fingerprint), pending: fieldReady(form.action_fingerprint) ? [] : ['missing_action_fingerprint'], blockers: [] },
      run_kernel: { ready: traceReady || hasRefPrefix('kernel:'), pending: traceReady || hasRefPrefix('kernel:') ? [] : ['awaiting_run_kernel_check'], blockers: [] },
      verified_history: { ready: traceReady || hasRefPrefix('history:'), pending: traceReady || hasRefPrefix('history:') ? [] : ['awaiting_verified_history'], blockers: [] },
      probe: { ready: hasRefPrefix('probe:'), pending: hasRefPrefix('probe:') ? [] : ['missing_probe_evidence_ref'], blockers: [] },
    };
    for (const kind of kinds) {
      const check = checks[kind];
      const blockers = check.blockers;
      const pending = check.pending;
      signals[kind] = {
        status: blockers.length > 0 ? 'blocked' : pending.length > 0 ? 'pending' : 'passing',
        summary: blockers.length > 0 ? blockers.join(', ') : pending.length > 0 ? pending.join(', ') : `${kind} verified`,
        evidence_refs: evidenceRefs,
      };
    }
    return signals;
  }

  function payload() {
    if (!form.session_id.trim() || !form.action_id.trim() || !form.actor_id.trim()) {
      throw new Error('Approval preview requires session, action, and actor identifiers.');
    }
    if (!form.governance_available) {
      throw new Error('Approval preview cannot resolve while governance state is unavailable.');
    }
    const approvalSources = form.approval_sources.split(',').map((row) => row.trim()).filter(Boolean);
    if (approvalSources.length === 0) {
      throw new Error('Approval preview requires at least one approval source.');
    }
    const evidenceRefs = requireEvidence(approvalEvidenceRefs(approvalSources), 'approval_chain.evidence_refs');
    if (evidenceRefs.length === 0) {
      throw new Error('Approval preview requires approval sources with evidence refs such as evidence:run-id.');
    }

    const requestPayload = {
      project_id: projectId,
      session_id: form.session_id,
      channel: form.channel,
      action_id: form.action_id,
      action_type: form.action_type,
      actor_id: form.actor_id,
      run_id: form.run_id,
      risk_domain: form.risk_domain,
      summary: form.summary,
      action_fingerprint: form.action_fingerprint,
      target_paths: form.target_paths.split('\n').map((row) => row.trim()).filter(Boolean),
      approval_sources: approvalSources,
      governance_mode: form.governance_mode,
      governance_available: form.governance_available,
      readiness_signals: readinessSignals(approvalSources),
      evidence_links: evidenceRefs.map((ref) => ({
        evidence_id: ref,
        kind: 'external',
        ref,
        summary: `approval evidence supplied by operator: ${ref}`,
      })),
      authority_refs: ['operator-preview'],
    };
    assertNoPlaceholders(requestPayload, ['readiness_signals.*.evidence_refs[].ref', 'evidence_links[].ref'], 'approval_chain.payload');
    return requestPayload;
  }

  async function resolveDecision() {
    loading = true;
    error = '';
    try {
      const requestPayload = payload();
      decision = await resolveWorkbenchApprovalChain(requestPayload);
    } catch (err) {
      error = err instanceof Error ? err.message : String(err);
      decision = null;
    } finally {
      loading = false;
    }
  }

  $effect(() => {
    void projectId;
    error = '';
  });
</script>

<section class="approval-chain" aria-label="Workbench approval chain" data-rcg0021-p05-receipt={approvalPreviewReceipt}>
  <header class="view-header">
    <div>
      <h1>Approval Chain</h1>
      <p>Review and resolve the approval chain for actions in project {projectId}.</p>
      <HelpPopover
        title="Approval chain"
        body="First-match semantics: the chain evaluates steps in order and stops at the first matching rule — only that step's outcome applies, even if later steps would decide differently. Fail-closed behavior: if no step matches and no fallback rule is configured, the decision defaults to deny — actions are not allowed by default. Outcome colors: allow (green) means the action may proceed; require_human_approval (amber) means a human must explicitly approve before the action runs; deny (red) means the action is blocked. Receipt explanation: the receipt payload records the exact matched step, outcome, and evidence references — use this for audit and escalation."
        severity="info"
      />
    </div>
    <button class="primary-action" type="button" onclick={resolveDecision} disabled={loading}>
      <i class="fas fa-shield-halved" aria-hidden="true"></i>
      <span>{loading ? 'Resolving' : 'Resolve'}</span>
    </button>
  </header>

  <div class="layout">
    <form class="decision-form" onsubmit={(event) => { event.preventDefault(); resolveDecision(); }}>
      <label>
        <span>Session</span>
        <input bind:value={form.session_id} autocomplete="off" />
      </label>
      <label>
        <span>Channel</span>
        <select bind:value={form.channel}>
          <option value="desktop">desktop</option>
          <option value="mobile">mobile</option>
          <option value="automation">automation</option>
          <option value="notification">notification</option>
          <option value="receipt">receipt</option>
          <option value="cli">cli</option>
        </select>
      </label>
      <label>
        <span>Action</span>
        <input bind:value={form.action_id} autocomplete="off" />
      </label>
      <label>
        <span>Type</span>
        <input bind:value={form.action_type} autocomplete="off" />
      </label>
      <label>
        <span>Risk</span>
        <select bind:value={form.risk_domain}>
          <option value="tool_invocation">tool_invocation</option>
          <option value="file_system">file_system</option>
          <option value="network">network</option>
          <option value="approval">approval</option>
          <option value="permission">permission</option>
        </select>
      </label>
      <label>
        <span>Summary</span>
        <textarea bind:value={form.summary}></textarea>
      </label>
      <label>
        <span>Target paths</span>
        <textarea bind:value={form.target_paths}></textarea>
      </label>
      <label>
        <span>Approval sources</span>
        <input bind:value={form.approval_sources} autocomplete="off" />
      </label>
      <label class="check-row">
        <input type="checkbox" bind:checked={form.governance_available} />
        <span>governance available</span>
      </label>
      <button class="full-action" type="submit" disabled={loading}>Resolve</button>
    </form>

    <div class="result-stack">
      {#if error}
        <div class="status-banner denied" role="alert">{error}</div>
      {/if}

      <section class="summary-strip" data-outcome={outcome}>
        <div><span>Outcome</span><strong>{outcome}</strong></div>
        <div><span>Matched</span><strong>{matchedStep}</strong></div>
        <div><span>Fallback</span><strong>{fallbackRule}</strong></div>
      </section>

      {#if decision}
        <section class="result-panel" aria-label="Rendered explanation">
          <h2>Explanation</h2>
          <pre>{decision.rendered_explanation}</pre>
        </section>

        <section class="result-panel" aria-label="Receipt payload">
          <h2>Receipt Payload</h2>
          <dl>
            <div><dt>Decision</dt><dd>{receipt?.decision_id}</dd></div>
            <div><dt>Outcome</dt><dd>{receipt?.outcome}</dd></div>
            <div><dt>Matched</dt><dd>{receipt?.matched_step}</dd></div>
            <div><dt>Fallback</dt><dd>{receipt?.fallback_rule}</dd></div>
          </dl>
        </section>

        <section class="trace-panel" aria-label="Ordered trace">
          <h2>Ordered Trace</h2>
          <div class="trace-table">
            <div class="table-heading">step</div>
            <div class="table-heading">status</div>
            <div class="table-heading">reason</div>
            <div class="table-heading">outcome</div>
            {#each orderedTrace as step}
              <div>{step.name}</div>
              <div>{step.status}</div>
              <div>{step.reason}</div>
              <div>{step.outcome ?? 'none'}</div>
            {/each}
          </div>
        </section>
      {:else if !error}
        <div class="empty-state">No approval-chain decision loaded.</div>
      {/if}
    </div>
  </div>
</section>

<style>
  .approval-chain { display: flex; flex-direction: column; gap: 20px; padding: 24px; color: var(--text-primary); }
  .view-header { display: flex; align-items: center; justify-content: space-between; gap: 16px; }
  h1, h2, p { margin: 0; }
  .view-header p, .empty-state { color: var(--text-muted); margin-top: 4px; }
  .layout { display: grid; grid-template-columns: minmax(280px, 360px) 1fr; gap: 20px; align-items: start; }
  .decision-form, .result-panel, .trace-panel, .summary-strip {
    border: 1px solid var(--border-default);
    background: var(--surface-elevated);
    border-radius: 8px;
    padding: 16px;
  }
  .decision-form, .result-stack { display: flex; flex-direction: column; gap: 12px; }
  label { display: flex; flex-direction: column; gap: 6px; color: var(--text-muted); font-size: 13px; }
  .check-row { flex-direction: row; align-items: center; color: var(--text-primary); }
  input, select, textarea {
    min-height: 44px;
    border: 1px solid var(--border-default);
    border-radius: 6px;
    background: var(--surface-primary);
    color: var(--text-primary);
    padding: 0 10px;
    font: inherit;
  }
  textarea { min-height: 72px; padding: 8px 10px; resize: vertical; }
  .primary-action, .full-action {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    gap: 8px;
    min-height: 44px;
    border: none;
    border-radius: 6px;
    background: var(--accent-primary);
    color: white;
    cursor: pointer;
    padding: 0 14px;
  }
  .full-action { width: 100%; }
  .status-banner { border-radius: 8px; padding: 12px 14px; font-weight: 600; }
  .status-banner.denied, .summary-strip[data-outcome="deny"] { border-color: #d44d4d; }
  .summary-strip[data-outcome="require_human_approval"] { border-color: #d6a821; }
  .summary-strip[data-outcome="allow"] { border-color: #31a66a; }
  .summary-strip { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; }
  .summary-strip div { display: flex; flex-direction: column; gap: 4px; min-width: 0; }
  .summary-strip span, dt, .table-heading { color: var(--text-muted); font-size: 12px; text-transform: uppercase; }
  .summary-strip strong, dd, pre, .trace-table div { overflow-wrap: anywhere; }
  dl { display: grid; gap: 8px; margin: 12px 0 0; }
  dl div { display: grid; grid-template-columns: minmax(80px, 120px) minmax(0, 1fr); gap: 8px; }
  dd { margin: 0; }
  pre {
    white-space: pre-wrap;
    margin: 12px 0 0;
    border: 1px solid var(--border-default);
    border-radius: 6px;
    padding: 10px;
    background: var(--surface-primary);
  }
  .trace-table { display: grid; grid-template-columns: minmax(120px, 1fr) 90px minmax(150px, 1.2fr) 120px; gap: 8px; }
  @media (max-width: 980px) {
    .layout, .summary-strip, .trace-table { grid-template-columns: 1fr; }
  }
</style>
