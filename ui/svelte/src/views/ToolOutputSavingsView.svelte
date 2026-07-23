<script>
  import { squashToolOutputPreview } from '$lib/api.js';
  import HelpPopover from '$lib/components/help/HelpPopover.svelte';
  import HelpTooltip from '$lib/components/help/HelpTooltip.svelte';

  let { projectId = 'default' } = $props();
  let rawText = $state('ERROR tests/test_demo.py::test_failure\nTraceback (most recent call last):\nFile "tests/test_demo.py", line 12\nnon-zero exit code: 1');
  let sourceKind = $state('terminal');
  let rawRef = $state('artifacts/redacted/tool-output.log');
  let command = $state('tool-output-preview');
  let exitCode = $state(1);
  let runId = $state('');
  let maxPreviewLines = $state(40);
  let artifactRef = $state('');
  let reproCapsuleRef = $state('');
  let result = $state(null);
  let error = $state('');
  let adminRequired = $state(false);
  let loading = $state(false);
  let status = $derived(result?.status ?? 'idle');
  let metrics = $derived(result?.metrics ?? {});
  let hazards = $derived(result?.hazards ?? []);
  let outcomes = $derived(result?.outcomes ?? []);
  let rawRefs = $derived(result?.raw_refs ?? []);
  let reasons = $derived(result?.reasons ?? []);

  async function preview() {
    loading = true;
    error = '';
    adminRequired = false;
    result = null;
    try {
      result = await squashToolOutputPreview({
        project_id: projectId,
        source_kind: sourceKind,
        raw_text: rawText,
        exit_code: exitCode === '' || exitCode === null ? null : Number(exitCode),
        raw_output_ref: rawRef ? { ref: rawRef, kind: 'artifact', redacted: true, guarded: true } : null,
        artifact_ref: artifactRef || undefined,
        repro_capsule_ref: reproCapsuleRef || undefined,
        max_preview_lines: maxPreviewLines === '' || maxPreviewLines === null ? null : Number(maxPreviewLines),
        command,
        run_id: runId,
      });
    } catch (err) {
      error = err instanceof Error ? err.message : String(err);
      adminRequired = err?.code === 'admin_required';
    } finally {
      loading = false;
    }
  }

  $effect(() => {
    void projectId;
  });
</script>

<section class="tool-output-savings-view" aria-label="Tool output savings">
  <header class="view-header">
    <div>
      <h1>Tool Output Savings</h1>
      <p>Squashed preview status, hazards, outcomes, raw-output-ref, degraded and blocked reasons, and savings metrics.</p>
      <HelpPopover
        title="Tool output savings"
        body="Squash ratio display: compression_ratio shows how much the raw output was reduced (e.g. 0.12 means 88% reduction). estimated_token_savings estimates the tokens saved versus sending the full raw output to the model. Hazard-preserved items note: lines flagged as hazards (stack traces, error codes, file paths, secrets) are always preserved in full regardless of squash ratio — they are never elided. Raw artifact access: the raw_refs field lists the redacted raw-output-ref for the original unsquashed output; use this to retrieve the full content if the squashed preview omits important context."
        severity="info"
      />
    </div>
    <button class="primary-action" onclick={preview} disabled={loading}>{loading ? 'Previewing' : 'Preview'}</button>
  </header>

  <div class="tool-output-layout">
    <section class="input-panel" aria-label="Raw tool output input">
      <label>Source<select bind:value={sourceKind}><option value="terminal">terminal</option><option value="ci">ci</option><option value="watcher">watcher</option><option value="eval">eval</option><option value="automation">automation</option></select></label>
      <label>Command<input bind:value={command} /></label>
      <div class="compact-fields">
        <label>Exit code<input type="number" bind:value={exitCode} /></label>
        <label>Max preview lines<input type="number" min="1" bind:value={maxPreviewLines} /></label>
      </div>
      <label>Run id<input bind:value={runId} /></label>
      <label>Redacted raw-output-ref<input bind:value={rawRef} /></label>
      <label>Artifact ref<input bind:value={artifactRef} /></label>
      <label>Repro capsule ref<input bind:value={reproCapsuleRef} /></label>
      <label>Raw output<textarea bind:value={rawText}></textarea></label>
    </section>

    <section class="preview-panel" aria-label="Squashed output preview">
      {#if loading}
        <div class="status">previewing</div>
      {:else if adminRequired}
        <div class="status blocked">admin access required</div>
      {:else if error}
        <div class="status blocked">blocked {error}</div>
      {:else if result}
        <div class:degraded={status === 'degraded'} class:blocked={status === 'blocked'} class="status">status {status}</div>
      {:else}
        <div class="status">idle</div>
      {/if}
      {#if result}
        <div class="metrics-grid">
          <div><span>raw bytes</span><strong>{metrics.raw_bytes ?? 0}</strong></div>
          <div><span>squashed bytes</span><strong>{metrics.squashed_bytes ?? 0}</strong></div>
          <div><span>estimated token savings</span><strong>{metrics.estimated_token_savings ?? 0}</strong></div>
          <div><span>compression ratio</span><strong>{metrics.compression_ratio ?? 0}</strong></div>
        </div>
        <pre class="preview-text">{result.preview}</pre>
        <div class="detail-columns">
          <section><h2>Hazards</h2>{#each hazards as hazard}<p>{hazard.kind}: {hazard.text} ({hazard.evidence_ref})</p>{:else}<p>none</p>{/each}</section>
          <section><h2>Outcomes</h2>{#each outcomes as outcome}<p>{outcome.kind}: {outcome.text} ({outcome.evidence_ref})</p>{:else}<p>none</p>{/each}</section>
        </div>
        <section>
          <h2>Raw refs and reasons</h2>
          {#each rawRefs as ref}<p>{ref.kind}: {ref.ref} guarded={String(ref.guarded)} redacted={String(ref.redacted)}</p>{:else}<p>no raw-output-ref</p>{/each}
          {#each reasons as reason}<p>{reason}</p>{/each}
        </section>
      {:else if !loading && !error && !adminRequired}
        <pre class="preview-text">No preview yet</pre>
      {/if}
    </section>
  </div>
</section>

<style>
  .tool-output-savings-view { display: flex; flex-direction: column; gap: 16px; padding: 20px; color: var(--text-primary); }
  .view-header { display: flex; justify-content: space-between; gap: 16px; align-items: flex-start; }
  .view-header h1 { margin: 0 0 4px; font-size: 24px; }
  .view-header p { margin: 0; color: var(--text-muted); max-width: 760px; }
  .primary-action { min-width: 104px; padding: 8px 12px; border: 1px solid var(--border-default); border-radius: 6px; background: var(--accent-primary, #4f8cff); color: white; }
  .tool-output-layout { display: grid; grid-template-columns: minmax(280px, 0.9fr) minmax(360px, 1.1fr); gap: 16px; }
  .input-panel, .preview-panel { display: flex; flex-direction: column; gap: 12px; min-width: 0; }
  .compact-fields { display: grid; grid-template-columns: repeat(2, minmax(120px, 1fr)); gap: 8px; }
  label { display: flex; flex-direction: column; gap: 6px; color: var(--text-muted); font-size: 13px; }
  select, input, textarea { width: 100%; border: 1px solid var(--border-default); border-radius: 6px; background: var(--surface-elevated, #111827); color: var(--text-primary); padding: 8px; font: inherit; }
  textarea { min-height: 360px; resize: vertical; font-family: ui-monospace, SFMono-Regular, Consolas, monospace; font-size: 13px; }
  .status { border: 1px solid var(--border-default); border-radius: 6px; padding: 8px 10px; background: var(--surface-elevated, #111827); }
  .status.degraded { border-color: #d6a821; }
  .status.blocked { border-color: #d44d4d; }
  .metrics-grid { display: grid; grid-template-columns: repeat(4, minmax(120px, 1fr)); gap: 8px; }
  .metrics-grid div { border: 1px solid var(--border-default); border-radius: 6px; padding: 8px; }
  .metrics-grid span { display: block; color: var(--text-muted); font-size: 12px; }
  .metrics-grid strong { display: block; margin-top: 4px; font-size: 18px; }
  .preview-text { min-height: 160px; white-space: pre-wrap; overflow: auto; border: 1px solid var(--border-default); border-radius: 6px; padding: 12px; background: var(--surface-elevated, #111827); }
  .detail-columns { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
  h2 { margin: 0 0 6px; font-size: 16px; }
  p { overflow-wrap: anywhere; }
  @media (max-width: 860px) { .tool-output-layout, .detail-columns, .metrics-grid, .compact-fields { grid-template-columns: 1fr; } .view-header { flex-direction: column; } }
</style>
