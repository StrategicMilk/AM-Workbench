<script>
  import { appState } from '$lib/stores/app.svelte.js';
  import { workbenchKernelRequest } from '$lib/api.js';
  import { requireEvidence } from '$lib/evidence/evidenceGuard.js';

  let { projectId = appState.currentProjectId || 'default' } = $props();

  let form = $state({
    action_kind: 'tool',
    subject_id: 'web-scrape',
    policy_profile_id: 'default',
    tool_card_id: '',
    source_card_id: '',
    capability_pack_id: '',
    trace_id: '',
    run_id: '',
    budget_scope: 'daily_usd',
    requested_by: 'operator',
  });
  let loading = $state(false);
  let errorMessage = $state('');
  let explanation = $state(null);

  let activeProjectId = $derived(projectId || appState.currentProjectId || 'default');
  let isDenied = $derived(explanation?.allowed === false);
  let isDegraded = $derived(Boolean(explanation?.degraded));

  $effect(() => {
    void activeProjectId;
    errorMessage = '';
  });

  function payload() {
    requireEvidence(
      [form.tool_card_id, form.source_card_id, form.capability_pack_id, form.trace_id, form.run_id].filter(Boolean),
      'policy_explainability.optional_refs',
    );
    const body = {
      project_id: activeProjectId,
      action_kind: form.action_kind,
      subject_id: form.subject_id,
      policy_profile_id: form.policy_profile_id,
      budget_scope: form.budget_scope || undefined,
      requested_by: form.requested_by || undefined,
    };
    for (const key of ['tool_card_id', 'source_card_id', 'capability_pack_id', 'trace_id', 'run_id']) {
      if (form[key]) body[key] = form[key];
    }
    return body;
  }

  async function explain() {
    loading = true;
    errorMessage = '';
    try {
      const data = await workbenchKernelRequest('/api/workbench/policy-explainability/explain', {
        method: 'POST',
        body: JSON.stringify(payload()),
      });
      explanation = data.explanation;
    } catch (err) {
      errorMessage = err.message;
      explanation = null;
    } finally {
      loading = false;
    }
  }
</script>

<svelte:head>
  <title>Policy Explainability</title>
</svelte:head>

<section class="policy-explainability" aria-labelledby="policy-explainability-title">
  <header class="view-header">
    <div>
      <h1 id="policy-explainability-title">Policy Explainability</h1>
      <p>Project {activeProjectId}</p>
    </div>
    <button class="primary-action" type="button" onclick={explain} disabled={loading}>
      <i class="fas fa-shield-alt" aria-hidden="true"></i>
      <span>{loading ? 'Checking' : 'Explain'}</span>
    </button>
  </header>

  <div class="layout">
    <form class="explain-form" onsubmit={(event) => { event.preventDefault(); explain(); }}>
      <label>
        <span>Action</span>
        <select bind:value={form.action_kind}>
          <option value="tool">Tool</option>
          <option value="dataset">Dataset</option>
          <option value="model">Model</option>
          <option value="mcp_server">MCP server</option>
          <option value="runtime">Runtime</option>
          <option value="export">Export</option>
        </select>
      </label>
      <label>
        <span>Subject</span>
        <input bind:value={form.subject_id} autocomplete="off" />
      </label>
      <label>
        <span>Policy</span>
        <input bind:value={form.policy_profile_id} autocomplete="off" />
      </label>
      <label>
        <span>Capability</span>
        <input bind:value={form.capability_pack_id} autocomplete="off" />
      </label>
      <label>
        <span>Source</span>
        <input bind:value={form.source_card_id} autocomplete="off" />
      </label>
      <label>
        <span>Tool card</span>
        <input bind:value={form.tool_card_id} autocomplete="off" />
      </label>
      <label>
        <span>Budget</span>
        <input bind:value={form.budget_scope} autocomplete="off" />
      </label>
      <button class="full-action" type="submit" disabled={loading}>Explain</button>
    </form>

    <div class="result-stack">
      {#if errorMessage}
        <div class="status-banner denied" role="alert">{errorMessage}</div>
      {/if}

      {#if isDenied}
        <div class="status-banner denied" role="status" aria-live="polite">Denied before use</div>
      {/if}

      {#if isDegraded}
        <div class="status-banner degraded" role="status" aria-live="polite">Policy state degraded or incomplete</div>
      {/if}

      {#if explanation}
        <section class="result-panel" aria-labelledby="policy-basis-heading">
          <h2 id="policy-basis-heading">Policy</h2>
          <dl>
            <div><dt>Allowed</dt><dd>{String(explanation.allowed)}</dd></div>
            <div><dt>Policy</dt><dd>{explanation.policy_id}</dd></div>
            <div><dt>Source</dt><dd>{explanation.policy_source}</dd></div>
            <div><dt>Decision</dt><dd>{explanation.decision_kind}</dd></div>
          </dl>
          <ul>
            {#each [...(explanation.reasons || []), ...(explanation.denial_reasons || [])] as reason}
              <li>{reason}</li>
            {/each}
          </ul>
        </section>

        <section class="result-grid" aria-label="Policy explanation details">
          <article>
            <h2>Exposure</h2>
            <dl>
              <div><dt>Secrets</dt><dd>{explanation.exposures?.secret_exposure}</dd></div>
              <div><dt>Files</dt><dd>{explanation.exposures?.file_exposure}</dd></div>
              <div><dt>Network</dt><dd>{explanation.exposures?.network_exposure}</dd></div>
              <div><dt>Credentials</dt><dd>{explanation.exposures?.credential_posture}</dd></div>
              <div><dt>Locality</dt><dd>{explanation.exposures?.locality}</dd></div>
            </dl>
          </article>
          <article>
            <h2>Budget</h2>
            <dl>
              <div><dt>Scope</dt><dd>{explanation.budget?.scope}</dd></div>
              <div><dt>Policy</dt><dd>{explanation.budget?.policy_name}</dd></div>
              <div><dt>Limit</dt><dd>{explanation.budget?.limit}</dd></div>
              <div><dt>Remaining</dt><dd>{explanation.budget?.remaining}</dd></div>
            </dl>
          </article>
          <article>
            <h2>Trace</h2>
            <dl>
              <div><dt>Trace</dt><dd>{explanation.trace?.trace_id || 'pending'}</dd></div>
              <div><dt>Receipt</dt><dd>{explanation.trace?.receipt_kind}</dd></div>
              <div><dt>Record</dt><dd>{String(explanation.trace?.will_record)}</dd></div>
              <div><dt>Retention</dt><dd>{explanation.trace?.retention_note}</dd></div>
            </dl>
          </article>
          <article>
            <h2>Failure</h2>
            <p>{explanation.failure_behavior}</p>
            <p>{explanation.budget?.failure_behavior}</p>
          </article>
        </section>
      {:else if !errorMessage}
        <div class="empty-state">No explanation loaded.</div>
      {/if}
    </div>
  </div>
</section>

<style>
  .policy-explainability {
    display: flex;
    flex-direction: column;
    gap: 20px;
    padding: 24px;
  }

  .view-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 16px;
  }

  h1,
  h2,
  p {
    margin: 0;
  }

  .view-header p,
  .empty-state {
    color: var(--text-muted);
    margin-top: 4px;
  }

  .layout {
    display: grid;
    grid-template-columns: minmax(260px, 340px) 1fr;
    gap: 20px;
    align-items: start;
  }

  .explain-form,
  .result-panel,
  .result-grid article {
    border: 1px solid var(--border-default);
    background: var(--surface-elevated);
    border-radius: 8px;
    padding: 16px;
  }

  .explain-form,
  .result-stack {
    display: flex;
    flex-direction: column;
    gap: 12px;
  }

  label {
    display: flex;
    flex-direction: column;
    gap: 6px;
    color: var(--text-muted);
    font-size: 13px;
  }

  input,
  select {
    min-height: 44px;
    border: 1px solid var(--border-default);
    border-radius: 6px;
    background: var(--surface-primary);
    color: var(--text-primary);
    padding: 0 10px;
  }

  .primary-action,
  .full-action {
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

  .full-action {
    width: 100%;
  }

  .status-banner {
    border-radius: 8px;
    padding: 12px 14px;
    font-weight: 600;
  }

  .status-banner.denied {
    background: rgba(239, 68, 68, 0.16);
    color: #fecaca;
  }

  .status-banner.degraded {
    background: rgba(245, 158, 11, 0.16);
    color: #fde68a;
  }

  .result-grid {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 12px;
  }

  dl {
    display: grid;
    gap: 8px;
    margin: 12px 0 0;
  }

  dl div {
    display: grid;
    grid-template-columns: minmax(80px, 120px) minmax(0, 1fr);
    gap: 8px;
  }

  dt {
    color: var(--text-muted);
  }

  dd {
    margin: 0;
    overflow-wrap: anywhere;
  }

  ul {
    margin: 12px 0 0;
    padding-left: 18px;
  }

  @media (max-width: 900px) {
    .layout,
    .result-grid {
      grid-template-columns: 1fr;
    }
  }
</style>
