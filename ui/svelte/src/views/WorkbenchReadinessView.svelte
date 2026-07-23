<script>
  import { fetchWorkbenchReadinessSnapshot, previewWorkbenchReadinessAdmission } from '$lib/api.js';
  import HelpPopover from '$lib/components/help/HelpPopover.svelte';
  import HelpTooltip from '$lib/components/help/HelpTooltip.svelte';

  let { projectId = 'default' } = $props();
  let loading = $state(false);
  let previewing = $state(false);
  let adminRequired = $state(false);
  let error = $state('');
  let snapshot = $state(null);
  let selectedFeature = $state('automation_admission');
  let preview = $state(null);

  let mode = $derived(snapshot?.mode ?? 'blocked');
  let reasons = $derived(snapshot?.reasons ?? []);
  let actions = $derived(snapshot?.recommended_actions ?? []);
  let signals = $derived(snapshot?.signals ?? []);
  let gates = $derived(Object.entries(snapshot?.feature_gates ?? {}));
  let evidenceRefs = $derived(snapshot?.evidence_refs ?? []);
  let admission = $derived(preview?.admission_preview ?? null);

  async function loadSnapshot() {
    loading = true;
    error = '';
    adminRequired = false;
    try {
      snapshot = await fetchWorkbenchReadinessSnapshot(projectId);
    } catch (err) {
      error = err instanceof Error ? err.message : String(err);
      adminRequired = err?.code === 'admin_required';
    } finally {
      loading = false;
    }
  }

  async function runAdmissionPreview() {
    previewing = true;
    error = '';
    adminRequired = false;
    try {
      const signalMap = {};
      for (const signal of signals) {
        signalMap[signal.kind] = signal;
      }
      preview = await previewWorkbenchReadinessAdmission({ feature: selectedFeature, signals: signalMap });
      snapshot = preview;
    } catch (err) {
      error = err instanceof Error ? err.message : String(err);
      adminRequired = err?.code === 'admin_required';
    } finally {
      previewing = false;
    }
  }

  $effect(() => {
    void projectId;
    loadSnapshot();
  });
</script>

<section class="readiness-view" aria-label="Workbench readiness">
  <header class="view-header">
    <div>
      <h1>Workbench Readiness</h1>
      <p>Earned operator readiness across setup, identity, config, provider, policy, tool-pin, connector, and run-kernel state.</p>
      <HelpPopover
        title="Readiness modes"
        body="full — all required signals verified; workbench operates without restriction. minimal — core signals met; some features gated. restricted — one or more required signals degraded; high-risk operations blocked. blocked — a required signal is missing or failed; workbench cannot accept new runs. Reason codes describe which signal caused the mode. Recommended actions guide recovery — resolve each listed item and refresh."
        severity="info"
      />
    </div>
    <div class="actions">
      <select bind:value={selectedFeature} aria-label="Admission feature">
        <option value="mission_control">mission_control</option>
        <option value="launcher_first_run_setup">launcher_first_run_setup</option>
        <option value="automation_admission">automation_admission</option>
        <option value="provider_use">provider_use</option>
        <option value="capability_pack_use">capability_pack_use</option>
        <option value="connector_use">connector_use</option>
        <option value="run_kernel_operations">run_kernel_operations</option>
      </select>
      <button class="primary-action" onclick={runAdmissionPreview} disabled={previewing || !snapshot}>
        {previewing ? 'Previewing' : 'Preview Admission'}
      </button>
      <button onclick={loadSnapshot} disabled={loading}>{loading ? 'Loading' : 'Refresh'}</button>
    </div>
  </header>

  {#if adminRequired}
    <div class="status blocked" role="alert">admin access required</div>
  {:else if error}
    <div class="status blocked" role="alert">blocked {error}</div>
  {:else if loading && !snapshot}
    <div class="status" role="status" aria-live="polite">loading</div>
  {:else}
    <div class="mode-strip" data-mode={mode} role="status" aria-live="polite">
      <strong>{mode}</strong>
      <span>full</span>
      <span>minimal</span>
      <span>restricted</span>
      <span>blocked</span>
    </div>
  {/if}

  {#if snapshot}
    <div class="readiness-grid">
      <section aria-label="Reasons">
        <h2>Reasons</h2>
        {#each reasons as reason}
          <p>{reason}</p>
        {:else}
          <p>all-required-readiness-signals-verified</p>
        {/each}
      </section>

      <section aria-label="Recommended actions">
        <h2>Recommended Actions</h2>
        {#each actions as action}
          <p>{action}</p>
        {:else}
          <p>continue-normal-workbench-operations</p>
        {/each}
      </section>

      <section aria-label="Feature gates">
        <h2>Feature Gates</h2>
        <div class="gate-list">
          {#each gates as [gate, decision]}
            <div class="gate-row" data-decision={decision}><span>{gate}</span><strong>{decision}</strong></div>
          {/each}
        </div>
      </section>

      <section aria-label="Admission preview">
        <h2>Admission Preview</h2>
        {#if admission}
          <p>{admission.feature}: {admission.decision}</p>
          <p>allowed={String(admission.allowed)} gate={admission.gate} mode={admission.mode}</p>
        {:else}
          <p>select a feature and preview admission</p>
        {/if}
      </section>
    </div>

    <section aria-label="Dependency signals">
      <h2>Dependency Signals</h2>
      <div class="signals-table">
        <div class="table-heading">signal</div>
        <div class="table-heading">status</div>
        <div class="table-heading">critical</div>
        <div class="table-heading">summary</div>
        {#each signals as signal}
          <div>{signal.kind}</div>
          <div>{signal.status}</div>
          <div>{String(signal.critical)}</div>
          <div>{signal.summary}</div>
        {/each}
      </div>
    </section>

    <section aria-label="Evidence references">
      <h2>Verified History And Probes</h2>
      {#each evidenceRefs as ref}
        <p>{ref.kind}: {ref.ref} {ref.detail ?? ''}</p>
      {:else}
        <p>no verified history or passing probe evidence supplied</p>
      {/each}
    </section>
  {/if}
</section>

<style>
  .readiness-view { display: flex; flex-direction: column; gap: 16px; padding: 20px; color: var(--text-primary); }
  .view-header { display: flex; justify-content: space-between; gap: 16px; align-items: flex-start; }
  .view-header h1 { margin: 0 0 4px; font-size: 24px; }
  .view-header p { margin: 0; color: var(--text-muted); max-width: 760px; }
  .actions { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
  button, select { min-height: 44px; border: 1px solid var(--border-default); border-radius: 6px; background: var(--surface-elevated, #111827); color: var(--text-primary); padding: 8px 10px; font: inherit; }
  .primary-action { background: var(--accent-primary, #4f8cff); color: white; }
  .status, .mode-strip { border: 1px solid var(--border-default); border-radius: 6px; padding: 10px 12px; background: var(--surface-elevated, #111827); }
  .status.blocked, .mode-strip[data-mode="blocked"] { border-color: #d44d4d; }
  .mode-strip[data-mode="restricted"] { border-color: #d6a821; }
  .mode-strip[data-mode="minimal"] { border-color: #4f8cff; }
  .mode-strip[data-mode="full"] { border-color: #31a66a; }
  .mode-strip { display: flex; gap: 16px; align-items: center; text-transform: uppercase; }
  .mode-strip strong { font-size: 22px; }
  .mode-strip span { color: var(--text-muted); font-size: 12px; }
  .readiness-grid { display: grid; grid-template-columns: repeat(4, minmax(180px, 1fr)); gap: 12px; }
  .readiness-grid section, .signals-table, section[aria-label="Evidence references"] { min-width: 0; border: 1px solid var(--border-default); border-radius: 6px; padding: 12px; background: var(--surface-elevated, #111827); }
  h2 { margin: 0 0 8px; font-size: 16px; }
  p { overflow-wrap: anywhere; }
  .gate-list { display: flex; flex-direction: column; gap: 6px; }
  .gate-row { display: flex; justify-content: space-between; gap: 8px; border-bottom: 1px solid var(--border-default); padding-bottom: 6px; }
  .gate-row[data-decision="blocked"] strong { color: #ff8a8a; }
  .gate-row[data-decision="restricted"] strong, .gate-row[data-decision="confirmation_required"] strong { color: #ffd166; }
  .gate-row[data-decision="open"] strong { color: #7bd88f; }
  .signals-table { display: grid; grid-template-columns: minmax(100px, 0.8fr) minmax(90px, 0.6fr) minmax(70px, 0.4fr) minmax(180px, 1.4fr); gap: 8px; }
  .table-heading { color: var(--text-muted); font-size: 12px; text-transform: uppercase; }
  @media (max-width: 980px) {
    .view-header { flex-direction: column; }
    .readiness-grid { grid-template-columns: 1fr; }
    .signals-table { grid-template-columns: 1fr; }
  }
</style>
